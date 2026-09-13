"""Replay the tail of a session into a topic that has just been bound.

A topic bound to a session that has been running for a while starts empty: the
monitor delivers what happens next, and everything said before the binding is
only reachable through ``/history``. That is fine for a window ccgram just
created and wrong for every other case — an adopted session, a topic recreated
after a delete, a window picked out of ``/new``.

``CCGRAM_TOPIC_BACKFILL_MESSAGES`` (default 0, the upstream behaviour) turns the
replay on. ``backfill_new_topic`` seals the transcript as delivered at its
current end, reads the last N messages bounded by that same offset, and sends
them behind a header saying how many were skipped. Sealing first is what makes
the two halves disjoint: nothing replayed here can also arrive as live output,
and the queue's backlog prompt never fires for the catch-up.

Call it only where a topic is newly bound to a session it has never delivered
for. Resuming a session into a topic that was already showing the previous one
(the dead-window recovery banner, ``/resume``) must not replay: that topic has
history of its own on screen and would show some of it twice.
"""

from __future__ import annotations

from typing import Any

import structlog

from ... import session_query
from ...config import config
from ...expandable_quote import EXPANDABLE_QUOTE_END, EXPANDABLE_QUOTE_START
from ...session_monitor import get_active_monitor
from ...telegram_client import TelegramClient
from ...window_state_ports import identity_state
from ..messaging_pipeline.message_sender import rate_limit_send_message

logger = structlog.get_logger()

BACKFILL_HEADER_PARTIAL = (
    "⏮ В сессии {total} сообщений, пропущено {skipped} — ниже последние {shown}"
)
BACKFILL_HEADER_FULL = "⏮ Загружена вся история: {total} сообщений"


def backfill_enabled() -> bool:
    """Whether a newly bound topic replays anything at all.

    Asked rather than read at the call sites, so the flag resolves against this
    module's configuration — the bind paths are the ones that must stay
    byte-identical to upstream while it is off.
    """
    return config.topic_backfill_messages > 0


def _format_backfill_message(message: dict[str, Any]) -> str:
    """Render one replayed transcript message the way ``/history`` does."""
    text = str(message.get("text", ""))
    text = text.replace(EXPANDABLE_QUOTE_START, "").replace(EXPANDABLE_QUOTE_END, "")
    if message.get("role") == "user":
        return f"👤 {text}"
    if message.get("content_type") == "thinking":
        return f"\U0001f9e0 Thinking…\n{text}"
    return text


async def mark_live_from_eof(window_id: str) -> int | None:
    """Declare the transcript delivered up to here, returning that offset.

    The monitor may already be tracking this session — it polls every entry in
    ``session_map.json``, bound or not — with a watermark from before the topic
    existed. Without this the first live tick would replay everything since,
    duplicating the backfill and tripping the queue's backlog prompt.

    ``None`` means the session is not tracked yet (no identity, no transcript
    path, or no running monitor). That is safe rather than unbounded: the
    monitor starts a session it has never seen at the end of its file, so a
    replay sent now is not repeated when tracking begins.
    """
    identity = identity_state.get_identity(window_id)
    if identity is None or not identity.session_id or identity.transcript_path is None:
        return None
    monitor = get_active_monitor()
    if monitor is None:
        return None
    return await monitor.mark_delivered_to_eof(
        identity.session_id, identity.transcript_path
    )


async def backfill_new_topic(
    client: TelegramClient, chat_id: int, thread_id: int, window_id: str
) -> int:
    """Seal the transcript and replay its tail, returning how many were sent.

    Zero when the flag is off, when nothing is bound to a transcript yet, or
    when the session has not said anything — and in the first of those cases
    nothing is sealed either, so the upstream path is untouched.
    """
    limit = config.topic_backfill_messages
    if limit <= 0:
        return 0
    # Sealed before the replay is read, and the replay stops at the seal, so
    # the two cannot overlap. Callers must reach here with no await between
    # the bind and this call, or a poll tick could deliver live in between.
    upto = await mark_live_from_eof(window_id)
    messages, total = await session_query.get_recent_messages(window_id, end_byte=upto)
    if total == 0:
        # A session that has said nothing yet needs no header about how much of
        # nothing was skipped; live delivery starts on its first line.
        return 0
    tail = messages[-limit:]
    shown = len(tail)
    skipped = total - shown
    header = (
        BACKFILL_HEADER_FULL.format(total=total)
        if skipped <= 0
        else BACKFILL_HEADER_PARTIAL.format(total=total, skipped=skipped, shown=shown)
    )
    # Paced like every other automated outbound. Ten messages at once is
    # exactly the burst Telegram's per-chat limit exists to refuse.
    await rate_limit_send_message(client, chat_id, header, message_thread_id=thread_id)
    for message in tail:
        await rate_limit_send_message(
            client,
            chat_id,
            _format_backfill_message(message),
            message_thread_id=thread_id,
        )
    logger.info(
        "Replayed %d of %d transcript message(s) into thread %d for window %s",
        shown,
        total,
        thread_id,
        window_id,
    )
    return shown
