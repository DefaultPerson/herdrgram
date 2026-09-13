"""Persisted record of the title each Telegram topic currently carries.

``handlers/status/topic_emoji`` keeps the last known topic name in memory
only, so in a fresh process the first status update of every bound topic
looked like a name change: ``editForumTopic`` was called with the title the
topic already had. Telegram posts a "topic renamed" service message even for
that no-op, so every bot restart renamed every bound topic once.

This record survives the restart. It holds the exact title last written to a
topic — by a rename, by the ``createForumTopic`` call that made it, or as
observed from a ``forum_topic_edited`` update when somebody renamed it by
hand — keyed ``"<chat_id>:<thread_id>"``, so the first update in a new
process can tell "unchanged" from "unknown". A topic with no record keeps the
old behaviour (send once, then it has a record).

Stored as ``$CCGRAM_DIR/topic_titles.json``, written atomically. A missing or
corrupt file reads as an empty record: the cost of losing it is one redundant
rename per topic, never a wrong one.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import structlog

from .config import config
from .utils import atomic_write_json

logger = structlog.get_logger()

_STORE_NAME = "topic_titles.json"

# Entries are dropped when a topic is unbound, but a topic retired while the
# bot was down leaves one behind, so the record is capped. Insertion order is
# the eviction order: a refreshed title is re-inserted at the end, so the key
# dropped first is the one untouched for longest.
_MAX_TITLES = 1000

_titles: dict[str, str] = {}
_loaded_store_path: Path | None = None


def _key(chat_id: int, thread_id: int) -> str:
    return f"{chat_id}:{thread_id}"


def _store_path() -> Path:
    """Resolve the state file now, so a test can repoint ``config_dir``."""
    return config.config_dir / _STORE_NAME


def _ensure_loaded() -> None:
    """Read the record once per store path.

    Anything unreadable — no file yet, truncated JSON, a file that is not a
    JSON object, a non-UTF-8 byte — leaves the record empty rather than
    raising: a rename is not worth failing a status update over.
    """
    global _loaded_store_path
    path = _store_path()
    if _loaded_store_path == path:
        return
    _loaded_store_path = path
    _titles.clear()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return
    if not isinstance(raw, dict):
        return
    for key, title in raw.items():
        if isinstance(key, str) and isinstance(title, str) and title:
            _titles[key] = title
    _trim()


def _trim() -> None:
    while len(_titles) > _MAX_TITLES:
        del _titles[next(iter(_titles))]


def _persist() -> None:
    """Write the record without letting a disk error cost the rename."""
    path = _store_path()
    try:
        if _titles:
            atomic_write_json(path, _titles)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not persist topic titles", path=str(path))


def get_title(chat_id: int, thread_id: int) -> str | None:
    """Title this topic currently carries, or None when it is not recorded."""
    _ensure_loaded()
    return _titles.get(_key(chat_id, thread_id))


def remember_title(chat_id: int, thread_id: int, title: str) -> None:
    """Record the title a topic now carries (idempotent, no write when equal)."""
    if not title:
        return
    _ensure_loaded()
    key = _key(chat_id, thread_id)
    if _titles.get(key) == title:
        return
    _titles.pop(key, None)
    _titles[key] = title
    _trim()
    _persist()


def forget_title(chat_id: int, thread_id: int) -> None:
    """Drop the record of a topic that is no longer bound (idempotent)."""
    _ensure_loaded()
    if _titles.pop(_key(chat_id, thread_id), None) is None:
        return
    _persist()


def reset_for_testing() -> None:
    """Clear the record in memory and on disk (tests share one CCGRAM_DIR)."""
    global _loaded_store_path
    if _loaded_store_path is not None:
        with contextlib.suppress(OSError):
            _loaded_store_path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        _store_path().unlink(missing_ok=True)
    _loaded_store_path = None
    _titles.clear()
