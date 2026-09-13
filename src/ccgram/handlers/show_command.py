"""/show — raise this topic's session in the terminal multiplexer UI.

Topic-scoped like ``/last`` and ``/split``: the thread binding resolves to a
window and the backend is asked to bring it forward, so a Telegram tap puts
the desk in front of the pane that is talking. Backend-neutral — it rides
``multiplexer.focus_window`` and is gated on ``capabilities.supports_focus``
(herdr raises the resolved pane; tmux and agterm have no UI to raise and say
so instead of reporting a silent no-op as success).

``focus_bound_window`` is the shared entry point: the ``show`` toolbar button
dispatches to it too, so both surfaces answer with the same text.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

from ..multiplexer import multiplexer as tmux_manager
from ..thread_router import thread_router

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

FOCUS_OK = "\U0001f441 Focused in the terminal UI"
FOCUS_UNSUPPORTED = "❌ Focus is not supported by this multiplexer backend."
FOCUS_FAILED = "❌ Could not focus: session not found."


async def focus_bound_window(window_id: str) -> tuple[bool, str]:
    """Focus *window_id* in the multiplexer UI; return ``(ok, reply text)``."""
    if not tmux_manager.capabilities.supports_focus:
        return False, FOCUS_UNSUPPORTED
    if not await tmux_manager.focus_window(window_id):
        logger.debug("focus request refused by the backend", window_id=window_id)
        return False, FOCUS_FAILED
    return True, FOCUS_OK


async def show_command(update: "Update", _context: "ContextTypes.DEFAULT_TYPE") -> None:
    """Handle /show — focus this topic's session in the multiplexer UI."""
    # Lazy: config singleton resolved at call time so tests can swap it
    from ..config import config

    # Lazy: messaging_pipeline ↔ handler cycle through status_bubble
    from .messaging_pipeline.message_sender import safe_reply

    # Lazy: callback_helpers only used when we have a real update
    from .callback_helpers import get_thread_id

    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ Use this command inside a topic.")
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(update.message, "❌ This topic is not bound to any session.")
        return

    _ok, text = await focus_bound_window(window_id)
    await safe_reply(update.message, text)
