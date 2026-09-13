"""On-demand topics: offer a topic for a new session, open it when accepted.

Upstream creates a Telegram topic the moment discovery finds an unbound
eligible window. ``CCGRAM_TOPIC_ON_DEMAND=true`` posts one offer to the General
topic of every target chat instead:

    🆕 Новая сессия: <name>
    📁 <cwd>
    🤖 <provider>
    [🧵 Открыть топик] [🙈 Скрыть]

Accepting it runs the ordinary creation path (``create_topic_in_chat`` through
``handle_new_window``), so icons, naming and binding are identical to automatic
mode, and then replays the tail of the transcript into the fresh topic. A topic
opened minutes after its session started would otherwise begin mid-conversation.

Three rules keep the offer from becoming noise:

  - one offer per window, remembered across a restart, so a bot that comes back
    up does not re-announce a window whose offer is still on screen;
  - ``🙈 Скрыть`` is durable — a dismissed window is never offered again, though
    it remains bindable through ``/sessions`` and the ``/new`` window picker;
  - a window that dies while its offer is unanswered has the offer retired
    (``⛔ Сессия завершена``) and forgotten.

Callback payloads carry a short minted token, never the window id: a herdr
session target is 81 bytes and Telegram allows 64. The token map is part of the
persisted record, so a button pressed after a restart still resolves. Authority
to press it is the bot-wide allow-list checked in ``callback_registry.dispatch``
— an offer names a window nobody owns yet, so the per-window ownership check
that guards ``callback_tokens`` cannot apply here.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError

from ... import window_query
from ...config import config
from ...multiplexer import multiplexer as tmux_manager
from ...multiplexer.base import canonical_window_id
from ...session_monitor import NewWindowEvent
from ...telegram_client import PTBTelegramClient, TelegramClient
from ...thread_router import thread_router
from ...utils import atomic_write_json
from ..callback_data import CB_ANNOUNCE_HIDE, CB_ANNOUNCE_OPEN
from ..callback_registry import register
from ..messaging_pipeline.message_sender import (
    edit_with_fallback,
    rate_limit_send_message,
)
from .topic_orchestration import collect_target_chats, handle_new_window

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

    from ...multiplexer.base import WindowRef as TmuxWindow

logger = structlog.get_logger()

_STORE_NAME = "topic_announcements.json"

# A dismissal is a per-window decision, and herdr never reissues a target, so
# the set only grows. Bound so a long-lived install cannot accumulate state
# forever; the oldest dismissals fall off first and a window that outlives its
# entry can at worst be offered once more.
_MAX_DISMISSED = 512

# Announcement texts. Kept here as one block: they are the whole user-facing
# surface of this feature and the tests pin them.
ANNOUNCE_HEADER = "🆕 Новая сессия: {name}"
ANNOUNCE_CWD = "📁 {cwd}"
ANNOUNCE_PROVIDER = "🤖 {provider}"
ANNOUNCE_OPENED = "🧵 Топик открыт: {name}"
ANNOUNCE_DIED = "⛔ Сессия завершена: {name}"
ANNOUNCE_HIDDEN = "🙈 Скрыто: {name}"
ANNOUNCE_GONE = "⚠ Сессия больше не доступна"
ANNOUNCE_FAILED = "⚠ Не удалось открыть топик — попробуйте ещё раз"
BUTTON_OPEN = "🧵 Открыть топик"
BUTTON_HIDE = "🙈 Скрыть"


@dataclass(slots=True)
class Announcement:
    """One live offer of a topic, and where its message is."""

    window_id: str
    token: str
    name: str
    announced_at: float
    # chat_id → message_id. One window can be offered in several chats.
    targets: dict[int, int] = field(default_factory=dict)


_announcements: dict[str, Announcement] = {}
_tokens: dict[str, str] = {}
_dismissed: dict[str, float] = {}
_loaded_store_path: Path | None = None


def _store_path() -> Path:
    """Resolve the state file now, so a test can repoint ``config_dir``."""
    return config.config_dir / _STORE_NAME


def _ensure_loaded() -> None:
    """Read persisted offers once per store path.

    A restart must not re-offer a window whose offer is still on screen, and
    must not re-offer one that was dismissed, so both survive on disk.
    """
    global _loaded_store_path
    path = _store_path()
    if _loaded_store_path == path:
        return
    _loaded_store_path = path
    _announcements.clear()
    _tokens.clear()
    _dismissed.clear()
    try:
        raw = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return
    if not isinstance(raw, dict):
        return
    for window_id, value in (raw.get("announced") or {}).items():
        entry = _parse_announcement(window_id, value)
        if entry is None:
            continue
        _announcements[window_id] = entry
        _tokens[entry.token] = window_id
    for window_id, dismissed_at in (raw.get("dismissed") or {}).items():
        if isinstance(window_id, str) and isinstance(dismissed_at, (int, float)):
            _dismissed[window_id] = float(dismissed_at)
    _trim_dismissed()


def _parse_announcement(window_id: object, value: object) -> Announcement | None:
    """Rebuild one record, skipping anything that cannot be acted on."""
    if not isinstance(window_id, str) or not isinstance(value, dict):
        return None
    token = value.get("token")
    if not isinstance(token, str) or not token:
        return None
    targets: dict[int, int] = {}
    for chat_id, message_id in (value.get("targets") or {}).items():
        try:
            targets[int(chat_id)] = int(message_id)
        except TypeError, ValueError:
            continue
    if not targets:
        return None
    announced_at = value.get("announced_at")
    name = value.get("name")
    return Announcement(
        window_id=window_id,
        token=token,
        name=name if isinstance(name, str) and name else window_id,
        announced_at=float(announced_at)
        if isinstance(announced_at, (int, float))
        else time.time(),
        targets=targets,
    )


def _trim_dismissed() -> None:
    while len(_dismissed) > _MAX_DISMISSED:
        oldest = min(_dismissed, key=lambda key: _dismissed[key])
        del _dismissed[oldest]


def _persist() -> None:
    """Write the record without letting a disk error cost the offer."""
    data = {
        "announced": {
            window_id: {
                "token": entry.token,
                "name": entry.name,
                "announced_at": entry.announced_at,
                "targets": {
                    str(chat): message for chat, message in entry.targets.items()
                },
            }
            for window_id, entry in _announcements.items()
        },
        "dismissed": _dismissed,
    }
    path = _store_path()
    try:
        if _announcements or _dismissed:
            atomic_write_json(path, data)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not persist topic announcements", path=str(path))


# ── Record queries (public for tests and for the orchestration seam) ───────


def has_announcement(window_id: str) -> bool:
    """Whether this window already has a live offer somewhere."""
    _ensure_loaded()
    return window_id in _announcements


def is_dismissed(window_id: str) -> bool:
    """Whether the user told us not to offer this window again."""
    _ensure_loaded()
    return window_id in _dismissed


def resolve_token(token: str) -> str | None:
    """Window behind a callback token, or None once the offer is gone."""
    _ensure_loaded()
    return _tokens.get(token)


def forget_announcement(window_id: str) -> None:
    """Drop an offer's record (idempotent). Does not touch the message."""
    _ensure_loaded()
    entry = _announcements.pop(window_id, None)
    if entry is None:
        return
    _tokens.pop(entry.token, None)
    _persist()


def reset_for_testing() -> None:
    """Clear in-memory state so a test can point ``config_dir`` elsewhere."""
    global _loaded_store_path
    _loaded_store_path = None
    _announcements.clear()
    _tokens.clear()
    _dismissed.clear()


def on_demand_enabled() -> bool:
    """Whether discovery offers a topic instead of creating one.

    Asked from ``topic_orchestration`` rather than read there, so the flag is
    resolved against this module's configuration — the creation path is the one
    place that must stay byte-identical to upstream while the flag is off.
    """
    return config.topic_on_demand


def _mint_token(window_id: str) -> str:
    while True:
        token = secrets.token_urlsafe(9)
        if token not in _tokens:
            _tokens[token] = window_id
            return token


# ── Offering a topic ───────────────────────────────────────────────────────


def _announcement_text(name: str, cwd: str, provider: str) -> str:
    lines = [ANNOUNCE_HEADER.format(name=name)]
    if cwd:
        lines.append(ANNOUNCE_CWD.format(cwd=cwd))
    if provider:
        lines.append(ANNOUNCE_PROVIDER.format(provider=provider))
    return "\n".join(lines)


def _announcement_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BUTTON_OPEN, callback_data=f"{CB_ANNOUNCE_OPEN}{token}"
                ),
                InlineKeyboardButton(
                    BUTTON_HIDE, callback_data=f"{CB_ANNOUNCE_HIDE}{token}"
                ),
            ]
        ]
    )


async def announce_new_window(
    client: TelegramClient, event: NewWindowEvent, topic_name: str
) -> bool:
    """Offer a topic for a newly discovered window, returning whether we did.

    Sent to the General topic of each target chat, which is the chat's control
    lane: a message with no ``message_thread_id`` lands there in a forum and in
    a private chat with topics alike.
    """
    _ensure_loaded()
    window_id = event.window_id
    if window_id in _dismissed or window_id in _announcements:
        return False

    chats = collect_target_chats(window_id)
    if not chats:
        return False

    provider = window_query.get_window_provider(window_id) or ""
    text = _announcement_text(topic_name, event.cwd, provider)
    token = _mint_token(window_id)
    keyboard = _announcement_keyboard(token)

    targets: dict[int, int] = {}
    for chat_id in sorted(chats):
        # Automated outbound, so it goes through the same per-chat pacing the
        # queue uses; several agents starting at once would otherwise send a
        # burst of offers straight into flood control.
        message = await rate_limit_send_message(
            client, chat_id, text, reply_markup=keyboard
        )
        if message is not None:
            targets[chat_id] = message.message_id

    if not targets:
        _tokens.pop(token, None)
        logger.warning("Could not offer a topic for window %s in any chat", window_id)
        return False

    _announcements[window_id] = Announcement(
        window_id=window_id,
        token=token,
        name=topic_name,
        announced_at=time.time(),
        targets=targets,
    )
    _persist()
    logger.info("Offered a topic for window %s in %d chat(s)", window_id, len(targets))
    return True


async def _retire_announcement(
    client: TelegramClient,
    entry: Announcement,
    text: str,
    *,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Rewrite an offer as a closed record, dropping its keyboard by default.

    Best effort by design: an offer is a convenience, and a Telegram error
    while retiring one must not stop the caller (a topic that was just created,
    a window that has just died). ``keyboard`` puts the buttons back for the
    one case that is not a retirement — a failed open worth retrying.
    """
    extra = {"reply_markup": keyboard} if keyboard is not None else {}
    for chat_id, message_id in entry.targets.items():
        try:
            await edit_with_fallback(client, chat_id, message_id, text, **extra)
        except TelegramError:
            logger.debug(
                "Could not retire the topic offer for window %s in chat %d",
                entry.window_id,
                chat_id,
            )


async def reconcile_announcements(
    client: TelegramClient, live_windows: "list[TmuxWindow]"
) -> None:
    """Retire offers whose window is gone, and forget dismissals for the same.

    Runs on the poll loop's complete listing, which the caller only has when it
    is confirmed — an unavailable listing skips the tick rather than reporting
    every window as dead.
    """
    _ensure_loaded()
    if not _announcements and not _dismissed:
        return
    live = {canonical_window_id(w.window_id) for w in live_windows}
    changed = False
    for window_id, entry in list(_announcements.items()):
        if canonical_window_id(window_id) in live:
            continue
        await _retire_announcement(client, entry, ANNOUNCE_DIED.format(name=entry.name))
        _announcements.pop(window_id, None)
        _tokens.pop(entry.token, None)
        changed = True
    for window_id in list(_dismissed):
        if canonical_window_id(window_id) not in live:
            del _dismissed[window_id]
            changed = True
    if changed:
        _persist()


# ── Opening a topic from an offer ──────────────────────────────────────────


async def _live_window(window_id: str) -> "TmuxWindow | None":
    """Re-read the window at the moment of the click, or None if it is gone."""
    # Lazy: importing the reconciliation seam at module load forms a cycle
    # (same reason as topic_orchestration.still_adoptable).
    from ...multiplexer.reconciliation import list_windows_for_reconciliation

    windows = await list_windows_for_reconciliation(tmux_manager)
    if windows is None:
        return None
    wanted = canonical_window_id(window_id)
    return next(
        (
            w
            for w in windows
            if canonical_window_id(w.window_id) == wanted and w.topic_eligible
        ),
        None,
    )


async def open_topic_from_announcement(
    client: TelegramClient, window_id: str, user_id: int, chat_id: int
) -> bool:
    """Create the topic an offer promised, then replay the session into it."""
    _ensure_loaded()
    entry = _announcements.get(window_id)
    if thread_router.has_window(window_id):
        # Two people pressing the same offer, or one pressing twice before the
        # first round-trip returned. ``handle_new_window`` would honour the
        # second as an explicit bind and hand this window a second topic —
        # which is the duplicate-topic failure this whole feature exists to
        # avoid. Whoever was first has the topic; retire the offer and stop.
        if entry is not None:
            await _retire_announcement(
                client, entry, ANNOUNCE_OPENED.format(name=entry.name)
            )
            forget_announcement(window_id)
        return False

    window = await _live_window(window_id)
    if window is None:
        if entry is not None:
            await _retire_announcement(
                client, entry, ANNOUNCE_DIED.format(name=entry.name)
            )
            forget_announcement(window_id)
        return False

    name = window.window_name or (entry.name if entry else window_id)
    event = NewWindowEvent(
        window_id=window_id,
        session_id="",
        window_name=name,
        cwd=window.cwd,
    )
    created = await handle_new_window(
        event, client, target_user_id=user_id, target_chat_id=chat_id
    )
    if not created:
        logger.warning(
            "Offer accepted but no topic was created for window %s", window_id
        )
        if entry is not None:
            # Keep the buttons: the offer is still good and the usual causes
            # (flood control, a transient Telegram error) clear on their own.
            await _retire_announcement(
                client,
                entry,
                f"{_announcement_text(name, window.cwd, '')}\n{ANNOUNCE_FAILED}",
                keyboard=_announcement_keyboard(entry.token),
            )
        return False

    # The replay is not driven here: ``create_topic_in_chat`` runs it for every
    # topic it binds, so an accepted offer and an auto-created topic get the
    # same catch-up from the same place.
    if entry is not None:
        await _retire_announcement(client, entry, ANNOUNCE_OPENED.format(name=name))
        forget_announcement(window_id)
    return True


async def hide_announcement(client: TelegramClient, window_id: str) -> None:
    """Dismiss an offer: delete its message and never offer this window again."""
    _ensure_loaded()
    entry = _announcements.get(window_id)
    _dismissed[window_id] = time.time()
    _trim_dismissed()
    if entry is None:
        _persist()
        return
    undeleted: dict[int, int] = {}
    for chat_id, message_id in entry.targets.items():
        try:
            deleted = await client.delete_message(
                chat_id=chat_id, message_id=message_id
            )
        except TelegramError:
            deleted = False
        if not deleted:
            undeleted[chat_id] = message_id
    if undeleted:
        # A message older than Telegram's delete window cannot be removed;
        # stripping it back to a one-line note is the honest fallback.
        await _retire_announcement(
            client,
            Announcement(
                window_id=entry.window_id,
                token=entry.token,
                name=entry.name,
                announced_at=entry.announced_at,
                targets=undeleted,
            ),
            ANNOUNCE_HIDDEN.format(name=entry.name),
        )
    _announcements.pop(window_id, None)
    _tokens.pop(entry.token, None)
    _persist()


# ── Registry dispatch entry point ──────────────────────────────────────────


@register(CB_ANNOUNCE_OPEN, CB_ANNOUNCE_HIDE)
async def handle_announce_callback(
    update: Update, context: "ContextTypes.DEFAULT_TYPE"
) -> None:
    """Route the two offer buttons.

    The query is answered before any of the work: creating a topic is several
    round-trips and Telegram expires an unanswered callback in seconds.
    Authorization already happened in ``callback_registry.dispatch``.
    """
    query = update.callback_query
    user = update.effective_user
    if query is None or query.data is None or user is None:
        return
    await query.answer()

    client: TelegramClient = PTBTelegramClient(context.bot)
    data = query.data
    hiding = data.startswith(CB_ANNOUNCE_HIDE)
    prefix = CB_ANNOUNCE_HIDE if hiding else CB_ANNOUNCE_OPEN
    window_id = resolve_token(data[len(prefix) :])
    if window_id is None:
        message = query.message
        if message is not None:
            await edit_with_fallback(
                client, message.chat.id, message.message_id, ANNOUNCE_GONE
            )
        return

    if hiding:
        await hide_announcement(client, window_id)
        return

    message = query.message
    chat_id = message.chat.id if message is not None else user.id
    await open_topic_from_announcement(client, window_id, user.id, chat_id)
