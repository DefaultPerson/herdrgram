"""Unified cleanup API for topic state.

Orchestrates topic teardown: dispatches registered cleanups via
TopicStateRegistry, then handles infrastructure and bot-specific async
cleanup that cannot be registered (log throttle, status messages,
interactive UI, user_data).

Functions:
  - clear_topic_state: Clean up all memory state for a specific topic
  - unbind_command: release a topic's binding, leaving the topic in place
  - close_command: release the binding and remove the topic from Telegram
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from telegram.error import BadRequest, TelegramError

from ..telegram_client import PTBTelegramClient, TelegramClient

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

from ..config import config
from ..session_map import session_map_prefix
from ..thread_router import thread_router
from ..window_state_ports import legacy_state
from ..topic_state_registry import topic_state
from ..utils import handle_general_topic_message, is_general_topic, log_throttle_reset
from .callback_helpers import get_thread_id
from .callback_tokens import revoke_window_tokens
from .interactive import clear_interactive_msg
from .messaging_pipeline.message_queue import enqueue_status_update
from .messaging_pipeline.message_sender import is_thread_gone, safe_reply, safe_send
from .status.status_bubble import clear_status_msg_info
from .user_state import PENDING_THREAD_ID, PENDING_THREAD_TEXT, VOICE_PENDING

logger = structlog.get_logger()


async def clear_topic_state(
    user_id: int,
    thread_id: int,
    client: TelegramClient | None = None,
    user_data: dict[str, Any] | None = None,
    window_id: str | None = None,
    chat_id: int | None = None,
    *,
    window_dead: bool = True,
) -> None:
    """Clear all memory state associated with a topic.

    Dispatches registered cleanups via TopicStateRegistry, then handles
    bot-specific async cleanup and infrastructure I/O that cannot be
    registered as simple callbacks.

    Args:
        window_dead: When False, skip qualified-scope cleanup because
            the tmux window is still alive (e.g. topic close, /unbind).
            Window-scope callbacks (toolbar labels, screen buffer, etc.) always
            run.  Shell prompt orchestrator state is cleared separately, only
            when the window is truly dead, to preserve skip/offer state for
            live sessions.
    """
    chat_id = chat_id or thread_router.resolve_chat_id(user_id, thread_id)

    qualified_id: str | None = None
    if window_id and window_dead:
        qualified_id = f"{session_map_prefix()}{window_id}"

    # Enqueue status-message delete BEFORE registry clears the message ID
    if client is not None:
        await enqueue_status_update(
            client,
            user_id,
            window_id or "",
            None,
            thread_id=thread_id,
        )
    else:
        clear_status_msg_info(user_id, thread_id)

    if window_id:
        revoke_window_tokens(window_id)

    # Registry dispatch — all module-specific per-topic/window/chat state.
    # Always pass window_id so window-scope callbacks (toolbar, screen buffer,
    # monitor state, etc.) run even when the window is still alive.
    # Shell prompt orchestrator state is excluded from the registry and handled
    # below so it only clears on true window death.
    topic_state.clear_all(
        user_id,
        thread_id,
        window_id=window_id,
        qualified_id=qualified_id,
        chat_id=chat_id,
    )
    if window_id and window_dead:
        # Lazy: cleanup → shell.shell_prompt_orchestrator → shell/__init__ →
        # polling → window_tick → apply → cleanup forms a cycle. Keep lazy.
        from .shell.shell_prompt_orchestrator import clear_state as _clear_shell_prompt

        _clear_shell_prompt(window_id)

    # Infrastructure cleanup (formatted keys, file I/O — not registerable)
    log_throttle_reset(f"status-update:{user_id}:{thread_id}")
    if window_id:
        log_throttle_reset(f"topic-probe:{window_id}")

    await clear_interactive_msg(user_id, client, thread_id, chat_id=chat_id)

    # user_data cleanup
    if user_data is not None and user_data.get(PENDING_THREAD_ID) == thread_id:
        user_data.pop(PENDING_THREAD_ID, None)
        user_data.pop(PENDING_THREAD_TEXT, None)

    if user_data is not None:
        voice_store: dict[tuple[int, int], str] = user_data.get(VOICE_PENDING, {})
        stale = [k for k in voice_store if k[0] == chat_id]
        for k in stale:
            voice_store.pop(k, None)


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disconnect a topic from its tmux window without killing the session."""
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        if (
            update.message
            and update.effective_chat
            and is_general_topic(update.message)
        ):
            await handle_general_topic_message(
                update.get_bot(), update.message, update.effective_chat.id
            )
        else:
            await safe_reply(update.message, "❌ Use this command inside a topic.")
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    window_id = (
        thread_router.get_window_for_thread(user.id, thread_id, chat_id)
        if isinstance(chat_id, int)
        else thread_router.get_window_for_thread(user.id, thread_id)
    )
    if not window_id:
        await safe_reply(update.message, "❌ This topic is not bound to any session.")
        return

    display = thread_router.get_display_name(window_id)
    is_legacy_herdr = legacy_state.is_legacy_herdr(window_id)
    if is_legacy_herdr:
        legacy_state.archive_legacy_herdr(window_id, user.id, thread_id)
    client = PTBTelegramClient(context.bot)
    await enqueue_status_update(client, user.id, window_id, None, thread_id)
    clear_kwargs: dict = {"window_id": window_id, "window_dead": False}
    if isinstance(chat_id, int):
        clear_kwargs["chat_id"] = chat_id
    await clear_topic_state(
        user.id, thread_id, client, context.user_data, **clear_kwargs
    )
    thread_router.unbind_thread(
        user.id, thread_id, chat_id=chat_id if isinstance(chat_id, int) else None
    )
    if is_legacy_herdr:
        await safe_reply(
            update.message,
            f"📦 Archived legacy Herdr binding `{display}` without closing its session.\n"
            "It remains blocked if restored; use /rollback in this topic to restore "
            "this exact binding, or send a message to explicitly bind a listed session target.",
        )
        return
    await safe_reply(
        update.message,
        f"✂ Unbound from window `{display}`. The session is still running.\n"
        "Send a message in this topic to rebind or create a new session.",
    )


# ── /close ────────────────────────────────────────────────────────────────

CLOSE_NOT_IN_TOPIC = "❌ Use this command inside a topic."
CLOSE_CLOSED = "🔒 Topic closed: {name}"
CLOSE_DELETED = "🗑 Topic deleted: {name}"
CLOSE_SESSION_ALIVE = "The session keeps running and is offered again in General."
CLOSE_NO_BINDING = "No session was bound to it."
CLOSE_FAILED = (
    "✂ Unbound from `{name}`, but Telegram would not remove this topic: {error}\n"
    "Delete it by hand if you want it gone — the session is unaffected."
)


def _close_unsupported(exc: TelegramError) -> bool:
    """Whether Telegram refused the close because this chat has no forum.

    ``closeForumTopic`` is a supergroup-forum method. A private chat with
    topics enabled answers "the chat is not a supergroup forum" for both close
    and reopen, and offers deletion as its only removal — so that one refusal
    is a signal to delete, not a failure.
    """
    if not isinstance(exc, BadRequest):
        return False
    message = exc.message.lower()
    return "not a supergroup forum" in message or "not a forum" in message


async def _remove_topic(
    client: TelegramClient, chat_id: int, thread_id: int
) -> tuple[bool, bool, str | None]:
    """Close *thread_id*, deleting it where Telegram cannot close.

    Returns ``(removed, deleted, error)``. A topic that is already gone counts
    as removed: the user asked for it not to be there.
    """
    try:
        closed = await client.close_forum_topic(
            chat_id=chat_id, message_thread_id=thread_id
        )
    except TelegramError as exc:
        if is_thread_gone(exc):
            return True, True, None
        if not _close_unsupported(exc):
            return False, False, str(exc)
        logger.debug(
            "close_forum_topic unsupported, deleting instead",
            chat_id=chat_id,
            thread_id=thread_id,
        )
    else:
        if closed is not False:
            return True, False, None
        logger.debug(
            "close_forum_topic refused, deleting instead",
            chat_id=chat_id,
            thread_id=thread_id,
        )

    try:
        deleted = await client.delete_forum_topic(
            chat_id=chat_id, message_thread_id=thread_id
        )
    except TelegramError as exc:
        if is_thread_gone(exc):
            return True, True, None
        return False, False, str(exc)
    if deleted is False:
        return False, False, "delete_forum_topic was refused"
    return True, True, None


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /close — retire this topic in Telegram without ending the session.

    The mirror image of closing a topic by hand under
    ``CCGRAM_KILL_ON_TOPIC_CLOSE``: there the topic is the session and closing
    it ends both, here only the Telegram side goes away. The binding is dropped
    first, so the ``forum_topic_closed`` update this triggers finds nothing to
    kill, then the topic is closed — or deleted, in a private chat where
    Telegram supports no other removal. Discovery sees an unbound live window
    within a poll cycle and offers it again in General.
    """
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        if (
            update.message
            and update.effective_chat
            and is_general_topic(update.message)
        ):
            await handle_general_topic_message(
                update.get_bot(), update.message, update.effective_chat.id
            )
        else:
            await safe_reply(update.message, CLOSE_NOT_IN_TOPIC)
        return

    raw_chat_id = update.effective_chat.id if update.effective_chat else None
    chat_id = raw_chat_id if isinstance(raw_chat_id, int) else None
    window_id = (
        thread_router.get_window_for_thread(user.id, thread_id, chat_id)
        if chat_id is not None
        else thread_router.get_window_for_thread(user.id, thread_id)
    )
    client = PTBTelegramClient(context.bot)
    display = thread_router.get_display_name(window_id) if window_id else ""
    name = display or window_id or str(thread_id)

    if window_id:
        await enqueue_status_update(client, user.id, window_id, None, thread_id)
        clear_kwargs: dict = {"window_id": window_id, "window_dead": False}
        if chat_id is not None:
            clear_kwargs["chat_id"] = chat_id
        await clear_topic_state(
            user.id, thread_id, client, context.user_data, **clear_kwargs
        )
        thread_router.unbind_thread(user.id, thread_id, chat_id=chat_id)

    target_chat_id = (
        chat_id
        if chat_id is not None
        else thread_router.resolve_chat_id(user.id, thread_id)
    )
    removed, deleted, error = await _remove_topic(client, target_chat_id, thread_id)
    if not removed:
        await safe_reply(
            update.message, CLOSE_FAILED.format(name=name, error=error or "unknown")
        )
        return

    # The note goes to General, the chat's control lane: a deleted topic takes
    # any reply in it with it, and a closed one may refuse further posts.
    headline = (CLOSE_DELETED if deleted else CLOSE_CLOSED).format(name=name)
    tail = CLOSE_SESSION_ALIVE if window_id else CLOSE_NO_BINDING
    await safe_send(client, target_chat_id, f"{headline}\n{tail}")
    logger.info(
        "topic_retired_by_command",
        user_id=user.id,
        thread_id=thread_id,
        window_id=window_id or "",
        deleted=deleted,
    )


async def rollback_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restore this topic's archived legacy binding while keeping it blocked."""
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message, "❌ Use this command inside the archived topic."
        )
        return
    if (
        thread_router.get_window_for_thread(
            user.id,
            thread_id,
            update.effective_chat.id if update.effective_chat else None,
        )
        is not None
    ):
        await safe_reply(update.message, "❌ This topic is already bound to a session.")
        return

    window_id = legacy_state.get_archived_legacy_herdr_binding(user.id, thread_id)
    if window_id is None:
        await safe_reply(
            update.message,
            "❌ No archived legacy Herdr binding belongs to this topic.",
        )
        return

    # Lazy: topic_lifecycle imports this module for clear_topic_state.
    from .topics.topic_lifecycle import rollback_legacy_herdr_binding

    if not rollback_legacy_herdr_binding(user.id, thread_id, window_id):
        await safe_reply(
            update.message, "❌ The archived binding is no longer available."
        )
        return
    await safe_reply(
        update.message,
        "📦 Restored this legacy Herdr binding. It remains blocked; send a message "
        "to explicitly bind a listed session target before taking actions.",
    )
