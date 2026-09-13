"""Correlate Telegram-to-terminal input with transcript user messages.

Also the one place that knows an injection came from Telegram rather than
from the keyboard, so the opt-in desktop notification for a delivered message
(``notify_injection``) is raised from here through the backend-neutral
``multiplexer.notify`` seam.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time

import structlog

from ..multiplexer.window_ops import send_followup_to_window, send_to_window

logger = structlog.get_logger()

_PENDING_INJECTION_TTL_S = 30.0
# A toast is glanceable, not readable: one line of the message is enough to
# tell which phone message just landed in the pane.
_NOTIFY_BODY_MAX_CHARS = 200
_AGENT_EXITED_MESSAGE = (
    "Agent exited or shell access is not confirmed; recover the session or run "
    "/agent shell before sending input."
)


@dataclass(frozen=True, slots=True, eq=False)
class _PendingInjection:
    text: str
    created_at: float


_pending_injections: dict[
    tuple[int, int | None, str, int], deque[_PendingInjection]
] = {}


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _key(user_id: int, window_id: str, thread_id: int, chat_id: int | None):
    return user_id, chat_id, window_id, thread_id


def remember_telegram_injection(
    user_id: int,
    window_id: str,
    thread_id: int,
    text: str,
    chat_id: int | None = None,
) -> _PendingInjection:
    now = time.monotonic()
    _discard_all_expired(now)
    pending = _pending_injections.setdefault(
        _key(user_id, window_id, thread_id, chat_id), deque()
    )
    injection = _PendingInjection(_normalize(text), now)
    pending.append(injection)
    return injection


def forget_telegram_injection(
    user_id: int,
    window_id: str,
    thread_id: int,
    injection: _PendingInjection,
    chat_id: int | None = None,
) -> None:
    pending = _pending_injections.get(_key(user_id, window_id, thread_id, chat_id))
    if pending is None:
        return
    try:
        pending.remove(injection)
    except ValueError:
        return
    if not pending:
        _pending_injections.pop(_key(user_id, window_id, thread_id, chat_id), None)


async def agent_origin_returned_to_shell(
    window_id: str, window: object | None = None
) -> bool:
    """Return whether Telegram input would reach a shell after an agent exit."""
    # Lazy: telegram_origin is a leaf used by provider/session initialization.
    from .. import window_query

    # Lazy: same provider/session initialization cycle.
    from ..multiplexer import multiplexer

    # Lazy: same provider/session initialization cycle.
    from ..providers import detect_provider_from_pane

    # Lazy: same provider/session initialization cycle.
    from ..window_state_ports import identity_state

    current_provider = window_query.get_window_provider(window_id)
    if not isinstance(current_provider, str) or not current_provider:
        return False
    initial_provider = identity_state.get_initial_provider_name(window_id)
    if current_provider == "shell":
        return not initial_provider
    if initial_provider == "shell":
        return False
    resolved_window = window or await multiplexer.find_window_by_id(window_id)
    if resolved_window is None:
        return False
    pane_command = getattr(resolved_window, "pane_current_command", "") or ""
    detected = await detect_provider_from_pane(pane_command, window_id=window_id)
    return detected == "shell"


async def notify_injection(window_id: str, text: str) -> None:
    """Tell the desktop that a Telegram message just landed in *window_id*.

    Opt-in (``CCGRAM_HERDR_NOTIFY_ON_INJECT``) and capability-gated: a backend
    without a UI of its own declares ``supports_notifications=False`` and is
    never called. The send has already succeeded by the time this runs, so a
    refused or broken notification is a debug line and nothing more — it must
    never turn a delivered message into a failed one.
    """
    # Lazy: config singleton resolved at call time so tests can swap it.
    from ..config import config

    if not config.herdr_notify_on_inject:
        return
    # Lazy: telegram_origin is a leaf used by provider/session initialization.
    from ..multiplexer import multiplexer

    # Lazy: same provider/session initialization cycle.
    from .. import window_query

    try:
        if not multiplexer.capabilities.supports_notifications:
            return
        view = window_query.view_window(window_id)
        label = (view.window_name if view else "") or window_id
        body = " ".join(text.split())
        if len(body) > _NOTIFY_BODY_MAX_CHARS:
            body = body[:_NOTIFY_BODY_MAX_CHARS] + "…"
        await multiplexer.notify(f"Telegram → {label}", body)
    except Exception as exc:  # noqa: BLE001 — a toast must never fail the send
        logger.debug(
            "injection notification failed", window_id=window_id, error=str(exc)
        )


async def send_telegram_to_window(
    user_id: int,
    window_id: str,
    thread_id: int | None,
    text: str,
    chat_id: int | None = None,
    *,
    raw: bool = False,
) -> tuple[bool, str]:
    if await agent_origin_returned_to_shell(window_id):
        return False, _AGENT_EXITED_MESSAGE
    if thread_id is None:
        success, message = await send_to_window(window_id, text, raw=raw)
        if success:
            await notify_injection(window_id, text)
        return success, message
    injection = remember_telegram_injection(
        user_id, window_id, thread_id, text, chat_id
    )
    success = False
    try:
        success, message = await send_to_window(window_id, text, raw=raw)
        if success:
            await notify_injection(window_id, text)
        return success, message
    finally:
        if not success:
            forget_telegram_injection(user_id, window_id, thread_id, injection, chat_id)


async def send_telegram_followup_to_window(
    user_id: int,
    window_id: str,
    thread_id: int | None,
    text: str,
    chat_id: int | None = None,
) -> tuple[bool, str]:
    if await agent_origin_returned_to_shell(window_id):
        return False, _AGENT_EXITED_MESSAGE
    if thread_id is None:
        success, message = await send_followup_to_window(window_id, text)
        if success:
            await notify_injection(window_id, text)
        return success, message
    injection = remember_telegram_injection(
        user_id, window_id, thread_id, text, chat_id
    )
    success = False
    try:
        success, message = await send_followup_to_window(window_id, text)
        if success:
            await notify_injection(window_id, text)
        return success, message
    finally:
        if not success:
            forget_telegram_injection(user_id, window_id, thread_id, injection, chat_id)


def consume_telegram_injection(
    user_id: int,
    window_id: str,
    thread_id: int,
    text: str,
    chat_id: int | None = None,
) -> bool:
    key = _key(user_id, window_id, thread_id, chat_id)
    pending = _pending_injections.get(key)
    if pending is None:
        return False
    _discard_expired(pending, time.monotonic())
    if not pending:
        _pending_injections.pop(key, None)
        return False
    normalized = _normalize(text)
    for index, injection in enumerate(pending):
        if injection.text == normalized:
            del pending[index]
            if not pending:
                _pending_injections.pop(key, None)
            return True
    return False


def clear_pending_telegram_injections() -> None:
    _pending_injections.clear()


def _discard_expired(pending: deque[_PendingInjection], now: float) -> None:
    while pending and now - pending[0].created_at >= _PENDING_INJECTION_TTL_S:
        pending.popleft()


def _discard_all_expired(now: float) -> None:
    for key, pending in list(_pending_injections.items()):
        _discard_expired(pending, now)
        if not pending:
            _pending_injections.pop(key, None)
