from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from telegram.error import TimedOut

from ccgram.config import config
from ccgram.handlers.topics import topic_icons
from ccgram.handlers.topics.topic_icons import (
    ICON_EMOJI_KEY,
    TOPIC_ICON_COLORS,
    note_icon_refused,
    pick_topic_icon,
)
from ccgram.telegram_client import FakeTelegramClient

_FETCH = "get_forum_topic_icon_stickers"


@pytest.fixture(autouse=True)
def _reset_icon_state() -> Iterator[None]:
    topic_icons._reset_icon_state_for_testing()
    yield
    topic_icons._reset_icon_state_for_testing()


@pytest.fixture
def icons_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "topic_random_icon", True)


def _stickers(*ids: str | None) -> tuple[MagicMock, ...]:
    return tuple(MagicMock(custom_emoji_id=icon_id) for icon_id in ids)


def _client(*ids: str | None) -> FakeTelegramClient:
    client = FakeTelegramClient()
    client.returns[_FETCH] = _stickers(*ids)
    return client


class TestFlagOff:
    async def test_returns_no_kwargs(self) -> None:
        client = _client("icon-1")

        assert await pick_topic_icon(client, -100500) == {}

    async def test_never_asks_telegram_for_icons(self) -> None:
        client = _client("icon-1")

        await pick_topic_icon(client, -100500)

        assert client.call_count(_FETCH) == 0


@pytest.mark.usefixtures("icons_on")
class TestFlagOn:
    async def test_picks_an_icon_and_a_valid_colour(self) -> None:
        client = _client("icon-1", "icon-2")

        kwargs = await pick_topic_icon(client, -100500)

        assert kwargs[ICON_EMOJI_KEY] in {"icon-1", "icon-2"}
        assert kwargs["icon_color"] in TOPIC_ICON_COLORS

    async def test_fetches_the_sticker_list_once_per_process(self) -> None:
        client = _client("icon-1", "icon-2")

        await pick_topic_icon(client, -100500)
        await pick_topic_icon(client, -100501)

        assert client.call_count(_FETCH) == 1

    async def test_ignores_stickers_without_a_custom_emoji_id(self) -> None:
        client = _client(None, "icon-2")

        kwargs = await pick_topic_icon(client, -100500)

        assert kwargs[ICON_EMOJI_KEY] == "icon-2"


@pytest.mark.usefixtures("icons_on")
class TestFetchFailure:
    async def test_degrades_to_the_colour_alone(self) -> None:
        client = FakeTelegramClient()
        client.set_side_effect(_FETCH, [TimedOut("no answer")])

        kwargs = await pick_topic_icon(client, -100500)

        assert ICON_EMOJI_KEY not in kwargs
        assert kwargs["icon_color"] in TOPIC_ICON_COLORS

    async def test_is_not_cached_so_the_next_topic_retries(self) -> None:
        client = FakeTelegramClient()
        client.set_side_effect(_FETCH, [TimedOut("blip"), _stickers("icon-1")])

        first = await pick_topic_icon(client, -100500)
        second = await pick_topic_icon(client, -100500)

        assert ICON_EMOJI_KEY not in first
        assert second[ICON_EMOJI_KEY] == "icon-1"

    async def test_an_empty_published_list_leaves_the_colour(self) -> None:
        client = _client()

        kwargs = await pick_topic_icon(client, -100500)

        assert ICON_EMOJI_KEY not in kwargs
        assert kwargs["icon_color"] in TOPIC_ICON_COLORS


@pytest.mark.usefixtures("icons_on")
class TestPerChatDeduplication:
    async def test_prefers_icons_this_chat_has_not_used(self) -> None:
        client = _client("icon-1", "icon-2", "icon-3")

        picked = [
            (await pick_topic_icon(client, -100500))[ICON_EMOJI_KEY] for _ in range(3)
        ]

        assert sorted(picked) == ["icon-1", "icon-2", "icon-3"]

    async def test_repeats_once_every_icon_is_taken(self) -> None:
        client = _client("icon-1")

        first = await pick_topic_icon(client, -100500)
        second = await pick_topic_icon(client, -100500)

        assert first[ICON_EMOJI_KEY] == second[ICON_EMOJI_KEY] == "icon-1"

    async def test_each_chat_keeps_its_own_memory(self) -> None:
        client = _client("icon-1")

        here = await pick_topic_icon(client, -100500)
        there = await pick_topic_icon(client, -100501)

        assert here[ICON_EMOJI_KEY] == there[ICON_EMOJI_KEY] == "icon-1"


@pytest.mark.usefixtures("icons_on")
class TestRefusedChat:
    async def test_keeps_the_colour_and_drops_the_icon(self) -> None:
        client = _client("icon-1")
        note_icon_refused(-100500)

        kwargs = await pick_topic_icon(client, -100500)

        assert ICON_EMOJI_KEY not in kwargs
        assert kwargs["icon_color"] in TOPIC_ICON_COLORS

    async def test_stops_asking_telegram_for_icons_in_that_chat(self) -> None:
        client = _client("icon-1")
        note_icon_refused(-100500)

        await pick_topic_icon(client, -100500)

        assert client.call_count(_FETCH) == 0

    async def test_leaves_every_other_chat_alone(self) -> None:
        client = _client("icon-1")
        note_icon_refused(-100500)

        kwargs = await pick_topic_icon(client, -100501)

        assert kwargs[ICON_EMOJI_KEY] == "icon-1"
