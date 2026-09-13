"""Random icons for the forum topics CCGram creates (``CCGRAM_TOPIC_RANDOM_ICON``).

Upstream creates every topic with Telegram's default icon, so a chat full of
agent topics reads as one undifferentiated column. With the flag on, each topic
CCGram creates gets a random ``icon_custom_emoji_id`` drawn from
``getForumTopicIconStickers`` — the only custom emoji a bot may use without a
premium account — plus a random ``icon_color`` from the six values Telegram
accepts.

The sticker list is the same for every chat and does not change mid-run, so it
is fetched lazily and cached for the process. No lock guards the fetch: the
worst a concurrent burst of adoptions can do is spend one redundant API call,
which is cheaper than a module-level ``asyncio.Lock`` bound to whichever event
loop touched it first.

Each chat remembers which icons it has already spent and prefers an unspent one,
so two topics in the same chat rarely look alike; once every icon is taken the
memory is cleared and repeats resume.

Nothing here raises or blocks a topic: an icon is decoration, and a topic that
cannot be decorated is still worth creating. Every failure path degrades to the
colour alone, which already tells two topics apart.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any

import structlog
from telegram.error import TelegramError

from ...config import config
from ...telegram_client import TelegramClient

logger = structlog.get_logger()

# The six values createForumTopic accepts for icon_color; anything else is
# rejected outright, so the set is fixed rather than configurable.
TOPIC_ICON_COLORS: tuple[int, ...] = (
    7322096,
    16766590,
    13338331,
    9367192,
    16749490,
    16478047,
)

ICON_EMOJI_KEY = "icon_custom_emoji_id"

# Process-wide sticker cache: None until the first successful fetch.
_icon_ids: tuple[str, ...] | None = None
# chat_id -> custom emoji ids already handed to a topic in that chat.
_used_icons: dict[int, set[str]] = {}
# Chats that refused a custom icon once; they get the colour only from then on.
_icon_refused_chats: set[int] = set()


async def pick_topic_icon(client: TelegramClient, chat_id: int) -> dict[str, Any]:
    """Return the icon kwargs for one ``create_forum_topic`` call.

    Empty when the flag is off, so the call stays byte-identical to upstream.
    With the flag on it always carries ``icon_color`` and carries
    ``icon_custom_emoji_id`` too whenever an icon could be resolved for this
    chat.
    """
    if not config.topic_random_icon:
        return {}
    kwargs: dict[str, Any] = {"icon_color": random.choice(TOPIC_ICON_COLORS)}
    if chat_id in _icon_refused_chats:
        return kwargs
    icon_id = _choose_icon(await _fetch_icon_ids(client), chat_id)
    if icon_id is not None:
        kwargs[ICON_EMOJI_KEY] = icon_id
    return kwargs


def note_icon_refused(chat_id: int) -> None:
    """Remember that this chat rejected a custom topic icon.

    A private-chat forum accepts ``icon_color`` but not every bot may spend a
    custom emoji there. One rejection is enough to stop paying for a doomed
    parameter on every later topic in that chat; the colour still varies.
    """
    _icon_refused_chats.add(chat_id)


async def _fetch_icon_ids(client: TelegramClient) -> tuple[str, ...]:
    """Custom emoji ids usable as topic icons, fetched once per process."""
    global _icon_ids
    if _icon_ids is not None:
        return _icon_ids
    try:
        stickers = await client.get_forum_topic_icon_stickers()
    except TelegramError as exc:
        # Not cached: a transient failure should not cost every later topic its
        # icon for the rest of the run.
        logger.warning(
            "Could not fetch forum topic icons, creating topics without one",
            error=str(exc),
        )
        return ()
    ids = tuple(
        sticker.custom_emoji_id for sticker in stickers if sticker.custom_emoji_id
    )
    if not ids:
        logger.warning("Telegram published no forum topic icons")
    _icon_ids = ids
    return ids


def _choose_icon(icon_ids: Sequence[str], chat_id: int) -> str | None:
    """Pick an icon for this chat, preferring one it has not used yet."""
    if not icon_ids:
        return None
    used = _used_icons.setdefault(chat_id, set())
    unused = [icon_id for icon_id in icon_ids if icon_id not in used]
    if not unused:
        # Every icon is already on a topic here — a repeat beats no icon.
        used.clear()
        unused = list(icon_ids)
    chosen = random.choice(unused)
    used.add(chosen)
    return chosen


def _reset_icon_state_for_testing() -> None:
    """Drop the sticker cache and every per-chat memory."""
    global _icon_ids
    _icon_ids = None
    _used_icons.clear()
    _icon_refused_chats.clear()
