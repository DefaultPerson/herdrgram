"""The persisted topic-title record, and the restart no-op it prevents.

Telegram posts a "topic renamed" service message even when the new title is
the one the topic already carries, and ``topic_emoji``'s name cache is empty
in a fresh process. Without a record on disk, the first status update after
every restart renamed every bound topic to the name it already had.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from ccgram import topic_titles
from ccgram.handlers.status import topic_emoji
from ccgram.handlers.status.topic_emoji import (
    DEBOUNCE_TO_IDLE_SECONDS,
    EMOJI_ACTIVE,
    EMOJI_IDLE,
    clear_topic_emoji_state,
    mark_awaiting_first_paint,
    sync_topic_name,
    update_topic_emoji,
)

CHAT_ID = -100
THREAD_ID = 42
_PATCH_MONOTONIC = "ccgram.handlers.status.topic_emoji.time.monotonic"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Give every test its own record on disk and a clean process state."""
    monkeypatch.setattr(topic_titles.config, "config_dir", tmp_path)
    topic_emoji.reset_all_state()
    yield
    topic_emoji.reset_all_state()


@pytest.fixture
def _plain(monkeypatch: pytest.MonkeyPatch):
    """CCGRAM_TOPIC_NAME_DECORATIONS=false — the title is the clean name."""
    from ccgram.config import config

    monkeypatch.setattr(config, "topic_name_decorations", False)
    yield


def _restart() -> None:
    """Simulate a bot restart: memory gone, the record on disk untouched."""
    topic_emoji._topic_states.clear()
    topic_emoji._pending_transitions.clear()
    topic_emoji._awaiting_first_paint.clear()
    topic_emoji._topic_names.clear()
    topic_emoji._disabled_chats.clear()
    topic_emoji._flood_cooldown_until.clear()
    topic_emoji._last_chat_edit.clear()
    topic_titles._titles.clear()
    topic_titles._loaded_store_path = None


async def _debounced_idle(bot: AsyncMock, display: str) -> None:
    """Drive an idle transition through its full debounce."""
    with patch(_PATCH_MONOTONIC) as clock:
        clock.return_value = 0.0
        await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "idle", display)
        clock.return_value = DEBOUNCE_TO_IDLE_SECONDS + 0.1
        await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "idle", display)


class TestRestartStopsRenamingToTheSameName:
    """The reported bug: one rename per bound topic per bot restart."""

    async def test_a_title_ccgram_wrote_is_not_written_again(
        self, _plain, tmp_path: Path
    ) -> None:
        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "active", "myproject")
        bot.edit_forum_topic.assert_called_once()
        assert json.loads((tmp_path / "topic_titles.json").read_text()) == {
            f"{CHAT_ID}:{THREAD_ID}": "myproject"
        }

        _restart()
        bot.edit_forum_topic.reset_mock()
        with patch(_PATCH_MONOTONIC, return_value=100.0):
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "active", "myproject")

        bot.edit_forum_topic.assert_not_called()

    async def test_a_name_changed_while_the_bot_was_down_is_still_renamed(
        self, _plain
    ) -> None:
        topic_titles.remember_title(CHAT_ID, THREAD_ID, "old-tab")
        _restart()

        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "active", "new-tab")

        bot.edit_forum_topic.assert_called_once_with(
            chat_id=CHAT_ID,
            message_thread_id=THREAD_ID,
            name="new-tab",
        )
        assert topic_titles.get_title(CHAT_ID, THREAD_ID) == "new-tab"

    async def test_a_topic_with_no_record_is_renamed_once_and_then_recorded(
        self, _plain
    ) -> None:
        """Topics bound before ccgram kept the record keep the old behaviour."""
        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "active", "legacy-topic")

        bot.edit_forum_topic.assert_called_once()
        assert topic_titles.get_title(CHAT_ID, THREAD_ID) == "legacy-topic"

    async def test_sync_still_repairs_a_title_that_matches_the_record(
        self, _plain
    ) -> None:
        """/sync is the escape hatch: it renames whatever the record says."""
        topic_titles.remember_title(CHAT_ID, THREAD_ID, "myproject")
        _restart()

        bot = AsyncMock()
        await sync_topic_name(bot, CHAT_ID, THREAD_ID, "myproject")

        bot.edit_forum_topic.assert_called_once_with(
            chat_id=CHAT_ID,
            message_thread_id=THREAD_ID,
            name="myproject",
        )


class TestDecoratedTitlesRestartToo:
    """Decorations on (the upstream default) — only the no-op is suppressed."""

    async def test_first_paint_skips_an_unchanged_decorated_title(self) -> None:
        bot = AsyncMock()
        await _debounced_idle(bot, "myproject")
        bot.edit_forum_topic.assert_called_once_with(
            chat_id=CHAT_ID,
            message_thread_id=THREAD_ID,
            name=f"{EMOJI_IDLE} myproject",
        )

        _restart()
        mark_awaiting_first_paint(CHAT_ID, THREAD_ID)
        bot.edit_forum_topic.reset_mock()
        with patch(_PATCH_MONOTONIC, return_value=100.0):
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "idle", "myproject")
            # The state counts as applied, so the next cycle is quiet too.
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "idle", "myproject")

        bot.edit_forum_topic.assert_not_called()

    async def test_a_state_change_while_the_bot_was_down_still_repaints(self) -> None:
        topic_titles.remember_title(CHAT_ID, THREAD_ID, f"{EMOJI_IDLE} myproject")
        _restart()
        mark_awaiting_first_paint(CHAT_ID, THREAD_ID)

        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "active", "myproject")

        bot.edit_forum_topic.assert_called_once_with(
            chat_id=CHAT_ID,
            message_thread_id=THREAD_ID,
            name=f"{EMOJI_ACTIVE} myproject",
        )

    async def test_the_debounce_still_governs_the_next_transition(self) -> None:
        """Suppressing the no-op leaves the debounce machinery untouched."""
        topic_titles.remember_title(CHAT_ID, THREAD_ID, f"{EMOJI_ACTIVE} myproject")
        _restart()
        mark_awaiting_first_paint(CHAT_ID, THREAD_ID)

        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "active", "myproject")
        bot.edit_forum_topic.assert_not_called()

        await _debounced_idle(bot, "myproject")

        bot.edit_forum_topic.assert_called_once_with(
            chat_id=CHAT_ID,
            message_thread_id=THREAD_ID,
            name=f"{EMOJI_IDLE} myproject",
        )

    async def test_a_rename_refused_as_not_modified_is_recorded(self) -> None:
        bot = AsyncMock()
        bot.edit_forum_topic.side_effect = BadRequest("Bad Request: TOPIC_NOT_MODIFIED")

        await _debounced_idle(bot, "myproject")

        assert topic_titles.get_title(CHAT_ID, THREAD_ID) == f"{EMOJI_IDLE} myproject"


class TestRecordLifecycle:
    async def test_creating_a_topic_records_its_title(self, tmp_path: Path) -> None:
        from ccgram.handlers.topics import topic_orchestration

        client = AsyncMock()
        topic = MagicMock()
        topic.message_thread_id = THREAD_ID
        client.create_forum_topic = AsyncMock(return_value=topic)

        with patch.object(topic_orchestration, "thread_router", MagicMock()):
            created = await topic_orchestration.create_topic_in_chat(
                client, CHAT_ID, "@7", "myproject", user_id=12345
            )

        assert created is True
        assert topic_titles.get_title(CHAT_ID, THREAD_ID) == "myproject"

    def test_unbinding_a_topic_forgets_its_title(self, tmp_path: Path) -> None:
        topic_titles.remember_title(CHAT_ID, THREAD_ID, "myproject")
        assert (tmp_path / "topic_titles.json").exists()

        clear_topic_emoji_state(CHAT_ID, THREAD_ID)

        assert topic_titles.get_title(CHAT_ID, THREAD_ID) is None
        assert not (tmp_path / "topic_titles.json").exists()

    def test_another_topics_title_survives_one_unbinding(self) -> None:
        topic_titles.remember_title(CHAT_ID, THREAD_ID, "myproject")
        topic_titles.remember_title(CHAT_ID, 43, "other")

        clear_topic_emoji_state(CHAT_ID, THREAD_ID)

        assert topic_titles.get_title(CHAT_ID, 43) == "other"

    @pytest.mark.parametrize(
        "content",
        ["{not json", "[]", '{"-100:42": 7}', "\udcff"],
        ids=["truncated", "not_an_object", "not_a_title", "not_utf8"],
    )
    async def test_a_corrupt_record_reads_as_empty(
        self, _plain, tmp_path: Path, content: str
    ) -> None:
        (tmp_path / "topic_titles.json").write_text(content, errors="surrogatepass")
        _restart()

        assert topic_titles.get_title(CHAT_ID, THREAD_ID) is None

        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_emoji(bot, CHAT_ID, THREAD_ID, "active", "myproject")

        bot.edit_forum_topic.assert_called_once()
        assert topic_titles.get_title(CHAT_ID, THREAD_ID) == "myproject"
        assert json.loads((tmp_path / "topic_titles.json").read_text()) == {
            f"{CHAT_ID}:{THREAD_ID}": "myproject"
        }

    def test_a_title_survives_a_restart_through_the_file(self, tmp_path: Path) -> None:
        topic_titles.remember_title(CHAT_ID, THREAD_ID, "myproject")
        _restart()

        assert topic_titles.get_title(CHAT_ID, THREAD_ID) == "myproject"

    def test_an_unwritable_record_costs_the_record_not_the_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(topic_titles, "atomic_write_json", _boom)

        topic_titles.remember_title(CHAT_ID, THREAD_ID, "myproject")

        assert topic_titles.get_title(CHAT_ID, THREAD_ID) == "myproject"

    def test_the_record_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A topic retired while the bot was down leaves an entry behind."""
        monkeypatch.setattr(topic_titles, "_MAX_TITLES", 3)

        for thread_id in range(5):
            topic_titles.remember_title(CHAT_ID, thread_id, f"topic-{thread_id}")

        assert topic_titles.get_title(CHAT_ID, 0) is None
        assert topic_titles.get_title(CHAT_ID, 1) is None
        assert topic_titles.get_title(CHAT_ID, 4) == "topic-4"

    def test_refreshing_a_title_keeps_it_from_being_evicted_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_titles, "_MAX_TITLES", 2)
        topic_titles.remember_title(CHAT_ID, 1, "first")
        topic_titles.remember_title(CHAT_ID, 2, "second")

        topic_titles.remember_title(CHAT_ID, 1, "first-renamed")
        topic_titles.remember_title(CHAT_ID, 3, "third")

        assert topic_titles.get_title(CHAT_ID, 1) == "first-renamed"
        assert topic_titles.get_title(CHAT_ID, 2) is None
