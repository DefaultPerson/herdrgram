"""Reopen a session ended with /kill: resume it and give it a topic again.

``/close`` keeps the session alive, so on-demand discovery offers it again in
General on its own. ``/kill`` ends it, which leaves nothing to discover; this
module posts the same kind of offer for the ended session instead:

    💀 Session ended: <name>
    📁 <cwd>
    🤖 <provider>
    [🔄 Reopen]

Pressing it resumes the session (the provider's own resume, in the same
directory, provider and approval mode), waits for it to register, and opens a
topic for it through the ordinary creation path, so naming, binding and the
transcript replay are those of an accepted "Open topic".

Callback payloads carry a short minted token (herdr targets are 81 bytes,
Telegram allows 64). Records persist to ``reopen_offers.json``, so a button
pressed after a restart still works; the oldest are dropped past a cap.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError

from ... import window_query
from ...config import config
from ...multiplexer import multiplexer as tmux_manager
from ...session import session_manager
from ...session_map import session_map_sync
from ...session_monitor import NewWindowEvent
from ...telegram_client import PTBTelegramClient, TelegramClient
from ...thread_router import thread_router
from ...utils import atomic_write_json
from ...window_state_store import CCGRAM_CREATED_WINDOW_ORIGIN
from ..callback_data import CB_REOPEN
from ..callback_registry import register
from ..messaging_pipeline.message_sender import (
    edit_with_fallback,
    rate_limit_send_message,
)
from . import topic_orchestration

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_STORE_NAME = "reopen_offers.json"
_MAX_OFFERS = 128
# A resumed Claude reloads its whole transcript before SessionStart fires.
_REGISTER_TIMEOUT_S = 30.0

REOPEN_HEADER = "💀 Session ended: {name}"
REOPEN_CWD = "📁 {cwd}"
REOPEN_PROVIDER = "🤖 {provider}"
REOPEN_WORKING = "🔄 Reopening {name}…"
REOPEN_DONE = "🧵 Topic reopened: {name}"
REOPEN_ALREADY = "🧵 {name} is already open in a topic"
REOPEN_FAILED = "⚠ Could not reopen {name}: {error}"
REOPEN_GONE = "⚠ This session can no longer be reopened"
BUTTON_REOPEN = "🔄 Reopen"


@dataclass(slots=True)
class ReopenOffer:
    """What it takes to bring one ended session back, and where its offer is."""

    name: str
    session_id: str
    cwd: str
    provider: str
    approval_mode: str
    chat_id: int
    message_id: int
    offered_at: float


_offers: dict[str, ReopenOffer] = {}
_in_progress: set[str] = set()
_loaded_store_path: Path | None = None


def _store_path() -> Path:
    return config.config_dir / _STORE_NAME


def _ensure_loaded() -> None:
    global _loaded_store_path
    path = _store_path()
    if _loaded_store_path == path:
        return
    _loaded_store_path = path
    _offers.clear()
    try:
        raw = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return
    if not isinstance(raw, dict):
        return
    for token, value in raw.items():
        if not isinstance(token, str) or not isinstance(value, dict):
            continue
        try:
            _offers[token] = ReopenOffer(**value)
        except TypeError:
            continue


def _persist() -> None:
    while len(_offers) > _MAX_OFFERS:
        oldest = min(_offers, key=lambda token: _offers[token].offered_at)
        del _offers[oldest]
    path = _store_path()
    try:
        if _offers:
            atomic_write_json(
                path, {token: asdict(offer) for token, offer in _offers.items()}
            )
        else:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not persist reopen offers", path=str(path))


def reset_for_testing() -> None:
    global _loaded_store_path
    _loaded_store_path = None
    _offers.clear()
    _in_progress.clear()


def _offer_text(name: str, cwd: str, provider: str) -> str:
    lines = [REOPEN_HEADER.format(name=name)]
    if cwd:
        lines.append(REOPEN_CWD.format(cwd=cwd))
    if provider:
        lines.append(REOPEN_PROVIDER.format(provider=provider))
    return "\n".join(lines)


def _keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BUTTON_REOPEN, callback_data=f"{CB_REOPEN}{token}")]]
    )


async def offer_reopen(
    client: TelegramClient,
    chat_id: int,
    *,
    name: str,
    session_id: str,
    cwd: str,
    provider: str,
    approval_mode: str,
) -> bool:
    """Post the ended-session note to General, with a Reopen button if possible.

    A session with no id or directory cannot be resumed, so it gets the bare
    note. Returns whether a message was sent.
    """
    _ensure_loaded()
    text = _offer_text(name, cwd, provider)
    if not session_id or not cwd or not provider:
        return await rate_limit_send_message(client, chat_id, text) is not None
    token = secrets.token_urlsafe(9)
    while token in _offers:
        token = secrets.token_urlsafe(9)
    message = await rate_limit_send_message(
        client, chat_id, text, reply_markup=_keyboard(token)
    )
    if message is None:
        return False
    _offers[token] = ReopenOffer(
        name=name,
        session_id=session_id,
        cwd=cwd,
        provider=provider,
        approval_mode=approval_mode or "normal",
        chat_id=chat_id,
        message_id=message.message_id,
        offered_at=time.time(),
    )
    _persist()
    return True


async def _bound_window_running(session_id: str) -> str | None:
    """A live window already running *session_id*, if any.

    Resuming a session that is still open would start a second agent on one
    transcript, and herdr cannot address two panes reporting one identity.
    """
    for window in await tmux_manager.list_windows():
        if (
            window.native_session_id == session_id
            or window_query.get_session_id_for_window(window.window_id) == session_id
        ):
            return window.window_id
    return None


async def _resume_window(offer: ReopenOffer) -> tuple[str | None, str]:
    """Start the resumed agent; return (window_id, error)."""
    # Lazy: providers package heavy bootstrap
    from ...providers import registry as provider_registry, resolve_launch_command

    try:
        provider = provider_registry.get(offer.provider)
        launch_args = provider.make_launch_args(resume_id=offer.session_id)
    except Exception as exc:  # noqa: BLE001 - unknown provider or invalid id
        return None, str(exc)
    launch_command = resolve_launch_command(
        offer.provider, approval_mode=offer.approval_mode
    )
    with topic_orchestration.pending_creation_transaction():
        success, message, _name, window_id = await tmux_manager.create_window(
            offer.cwd, agent_args=launch_args, launch_command=launch_command
        )
        if success:
            topic_orchestration.register_pending_creation(
                window_id, ttl_s=_REGISTER_TIMEOUT_S * 2
            )
    if not success:
        return None, message
    caps = provider.capabilities
    if caps.supports_hook and caps.names_session_on_first_prompt is not True:
        await session_map_sync.wait_for_session_map_entry(
            window_id,
            timeout=_REGISTER_TIMEOUT_S,
            resolve_window_id=window_query.resolve_window_alias,
        )
    current = window_query.resolve_window_alias(window_id)
    if current != window_id:
        topic_orchestration.register_pending_creation(current)
        topic_orchestration.clear_pending_creation(window_id)
    session_manager.set_window_origin(current, CCGRAM_CREATED_WINDOW_ORIGIN)
    session_manager.set_window_provider(current, offer.provider)
    session_manager.set_window_approval_mode(current, offer.approval_mode)
    session_manager.set_window_cwd(current, offer.cwd)
    return current, ""


async def reopen(client: TelegramClient, token: str, user_id: int) -> bool:
    """Resume the session behind *token* and open a topic for it."""
    _ensure_loaded()
    offer = _offers.get(token)
    if offer is None:
        return False

    async def note(text: str, *, keep_button: bool = False) -> None:
        extra = {"reply_markup": _keyboard(token)} if keep_button else {}
        try:
            await edit_with_fallback(
                client, offer.chat_id, offer.message_id, text, **extra
            )
        except TelegramError:
            logger.debug("Could not update the reopen offer", token=token)

    running = await _bound_window_running(offer.session_id)
    if running is not None and thread_router.has_window(running):
        await note(REOPEN_ALREADY.format(name=offer.name))
        _offers.pop(token, None)
        _persist()
        return False

    await note(REOPEN_WORKING.format(name=offer.name))
    if running is not None:
        window_id, error = running, ""
    else:
        window_id, error = await _resume_window(offer)
    if window_id is None:
        await note(
            f"{_offer_text(offer.name, offer.cwd, offer.provider)}\n"
            + REOPEN_FAILED.format(name=offer.name, error=error or "unknown"),
            keep_button=True,
        )
        return False

    # The guard kept discovery off the new pane while it registered; the
    # explicit bind below is what it was waiting for.
    topic_orchestration.clear_pending_creation(window_id)
    event = NewWindowEvent(
        window_id=window_id,
        session_id=offer.session_id,
        window_name=offer.name,
        cwd=offer.cwd,
    )
    created = await topic_orchestration.handle_new_window(
        event, client, target_user_id=user_id, target_chat_id=offer.chat_id
    )
    if not created:
        await note(
            f"{_offer_text(offer.name, offer.cwd, offer.provider)}\n"
            + REOPEN_FAILED.format(
                name=offer.name, error="the session runs, but no topic was created"
            ),
            keep_button=True,
        )
        return False

    await note(REOPEN_DONE.format(name=offer.name))
    _offers.pop(token, None)
    _persist()
    logger.info(
        "session_reopened",
        user_id=user_id,
        window_id=window_id,
        session_id=offer.session_id,
    )
    return True


@register(CB_REOPEN)
async def handle_reopen_callback(
    update: Update, context: "ContextTypes.DEFAULT_TYPE"
) -> None:
    """Route the Reopen button; authorization happened in the dispatcher.

    Answered first: a resume plus a topic is several round-trips, and Telegram
    expires an unanswered callback in seconds. A second press while the first
    is still working is ignored rather than starting a second agent.
    """
    query = update.callback_query
    user = update.effective_user
    if query is None or query.data is None or user is None:
        return
    await query.answer()
    client: TelegramClient = PTBTelegramClient(context.bot)
    token = query.data[len(CB_REOPEN) :]
    _ensure_loaded()
    if token not in _offers:
        if query.message is not None:
            await edit_with_fallback(
                client, query.message.chat.id, query.message.message_id, REOPEN_GONE
            )
        return
    if token in _in_progress:
        return
    _in_progress.add(token)
    try:
        await reopen(client, token, user.id)
    finally:
        _in_progress.discard(token)
