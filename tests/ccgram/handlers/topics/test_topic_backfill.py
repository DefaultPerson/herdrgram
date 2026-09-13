"""CCGRAM_TOPIC_BACKFILL_MESSAGES: replay the tail into a newly bound topic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.topics import topic_backfill
from ccgram.handlers.topics.topic_orchestration import handle_new_window
from ccgram.session_monitor import NewWindowEvent
from ccgram.telegram_client import FakeTelegramClient

WINDOW = "@7"
CHAT = -100500
USER = 12345
THREAD = 77
TRANSCRIPT = Path("/t/sess-1.jsonl")


# ── Fixtures and doubles ───────────────────────────────────────────────────


@pytest.fixture
def client() -> FakeTelegramClient:
    fake = FakeTelegramClient()
    fake.returns["create_forum_topic"] = MagicMock(message_thread_id=THREAD)
    return fake


@pytest.fixture(autouse=True)
def _no_pacing():
    """Collapse the per-chat send interval; the pacing itself is not under test."""
    with patch(
        "ccgram.handlers.topics.topic_backfill.rate_limit_send_message",
        new_callable=AsyncMock,
    ) as send:
        send.return_value = MagicMock(message_id=1)
        yield send


class _FakeMonitor:
    """Records the seal and reports where the watermark landed."""

    def __init__(self, offset: int | None = 4096) -> None:
        self.offset = offset
        self.calls: list[tuple[str, Path]] = []

    async def mark_delivered_to_eof(self, session_id: str, path: Path) -> int | None:
        self.calls.append((session_id, path))
        return self.offset


def _messages(count: int) -> list[dict]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "text": f"message {index}",
            "content_type": "text",
            "timestamp": None,
        }
        for index in range(count)
    ]


def _transcript(messages: list[dict], total: int, monitor: _FakeMonitor):
    """Patch the transcript and monitor seam ``topic_backfill`` reads through."""
    identity = MagicMock(session_id="sess-1", transcript_path=TRANSCRIPT)
    return (
        patch(
            "ccgram.handlers.topics.topic_backfill.session_query.get_recent_messages",
            AsyncMock(return_value=(messages, total)),
        ),
        patch(
            "ccgram.handlers.topics.topic_backfill.identity_state.get_identity",
            return_value=identity,
        ),
        patch(
            "ccgram.handlers.topics.topic_backfill.get_active_monitor",
            return_value=monitor,
        ),
    )


def _texts(send: AsyncMock) -> list[str]:
    return [call.args[2] for call in send.await_args_list]


def _threads(send: AsyncMock) -> list[int | None]:
    return [call.kwargs.get("message_thread_id") for call in send.await_args_list]


# ── The replay itself ──────────────────────────────────────────────────────


class TestBackfill:
    async def test_off_by_default_reads_nothing_and_seals_nothing(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock
    ) -> None:
        """Upstream behaviour: a newly bound topic shows only what happens next."""
        assert topic_backfill.backfill_enabled() is False
        monitor = _FakeMonitor()
        seam = _transcript(_messages(25), 25, monitor)
        with seam[0] as recent, seam[1], seam[2]:
            assert (
                await topic_backfill.backfill_new_topic(client, CHAT, THREAD, WINDOW)
                == 0
            )

        recent.assert_not_awaited()
        assert monitor.calls == []
        _no_pacing.assert_not_awaited()

    async def test_a_long_session_reports_what_it_skipped(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock, backfill_10: None
    ) -> None:
        monitor = _FakeMonitor()
        seam = _transcript(_messages(25), 25, monitor)
        with seam[0] as recent, seam[1], seam[2]:
            assert (
                await topic_backfill.backfill_new_topic(client, CHAT, THREAD, WINDOW)
                == 10
            )

        # Sealed before the read, and the read stops at the seal, so the replay
        # and live delivery cannot overlap.
        assert monitor.calls == [("sess-1", TRANSCRIPT)]
        assert recent.await_args is not None
        assert recent.await_args.kwargs["end_byte"] == 4096

        texts = _texts(_no_pacing)
        assert len(texts) == 11  # one header, then exactly ten messages
        assert texts[0] == "⏮ 25 messages in this session, 15 skipped — last 10 below"
        assert texts[1] == "message 15"  # assistant turn
        assert texts[2] == "👤 message 16"  # user turns keep the /history prefix
        assert texts[-1] == "👤 message 24"
        assert set(_threads(_no_pacing)) == {THREAD}

    async def test_a_short_session_reports_the_whole_history(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock, backfill_10: None
    ) -> None:
        seam = _transcript(_messages(3), 3, _FakeMonitor())
        with seam[0], seam[1], seam[2]:
            assert (
                await topic_backfill.backfill_new_topic(client, CHAT, THREAD, WINDOW)
                == 3
            )

        texts = _texts(_no_pacing)
        assert texts[0] == "⏮ Full history loaded: 3 messages"
        assert len(texts) == 4

    async def test_a_silent_session_sends_no_header(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock, backfill_10: None
    ) -> None:
        """A brand-new session has nothing to report about nothing."""
        seam = _transcript([], 0, _FakeMonitor())
        with seam[0], seam[1], seam[2]:
            assert (
                await topic_backfill.backfill_new_topic(client, CHAT, THREAD, WINDOW)
                == 0
            )

        _no_pacing.assert_not_awaited()

    async def test_an_untracked_session_replays_unbounded(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock, backfill_10: None
    ) -> None:
        """No seal means the monitor has not started; it will start at EOF."""
        with (
            patch(
                "ccgram.handlers.topics.topic_backfill."
                "session_query.get_recent_messages",
                AsyncMock(return_value=(_messages(2), 2)),
            ) as recent,
            patch(
                "ccgram.handlers.topics.topic_backfill.identity_state.get_identity",
                return_value=None,
            ),
        ):
            assert (
                await topic_backfill.backfill_new_topic(client, CHAT, THREAD, WINDOW)
                == 2
            )

        assert recent.await_args is not None
        assert recent.await_args.kwargs["end_byte"] is None


@pytest.fixture
def backfill_10(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topic_backfill.config, "topic_backfill_messages", 10)


# ── The bind paths that drive it ───────────────────────────────────────────


def _orchestration():
    return (
        patch("ccgram.handlers.topics.topic_orchestration.session_manager"),
        patch("ccgram.handlers.topics.topic_orchestration.thread_router"),
        patch("ccgram.handlers.topics.topic_orchestration.config"),
        patch("ccgram.handlers.topics.topic_orchestration.tmux_manager"),
    )


async def _auto_create(client: FakeTelegramClient) -> bool:
    """Discover an unbound window the way the session monitor does."""
    event = NewWindowEvent(
        window_id=WINDOW,
        session_id="sess-1",
        window_name="my-project",
        cwd="/home/user/my-project",
    )
    ctx = _orchestration()
    with ctx[0], ctx[1] as tr, ctx[2] as cfg, ctx[3] as mux:
        tr.has_window.return_value = False
        tr.iter_thread_bindings.return_value = iter([])
        cfg.group_id = CHAT
        cfg.allowed_users = {USER}
        mux.find_window_by_id = AsyncMock(return_value=None)
        mux.capabilities.supports_display_name_rebind = False
        return await handle_new_window(event, client)


class TestAutoCreatedTopic:
    async def test_flag_off_leaves_the_creation_path_untouched(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock
    ) -> None:
        monitor = _FakeMonitor()
        seam = _transcript(_messages(25), 25, monitor)
        with seam[0] as recent, seam[1], seam[2]:
            assert await _auto_create(client) is True

        assert client.call_count("create_forum_topic") == 1
        recent.assert_not_awaited()
        assert monitor.calls == []
        _no_pacing.assert_not_awaited()

    async def test_an_auto_created_topic_replays_the_tail(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock, backfill_10: None
    ) -> None:
        """The case the user hit: a recreated topic showing nothing."""
        monitor = _FakeMonitor()
        seam = _transcript(_messages(25), 25, monitor)
        with seam[0], seam[1], seam[2]:
            assert await _auto_create(client) is True

        assert client.call_count("create_forum_topic") == 1
        assert monitor.calls == [("sess-1", TRANSCRIPT)]
        texts = _texts(_no_pacing)
        assert len(texts) == 11
        assert texts[0] == "⏮ 25 messages in this session, 15 skipped — last 10 below"
        # Into the thread the topic was just created with, not the General lane.
        assert set(_threads(_no_pacing)) == {THREAD}

    async def test_a_failed_topic_creation_replays_nothing(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock, backfill_10: None
    ) -> None:
        from telegram.error import TelegramError

        client.set_side_effect("create_forum_topic", [TelegramError("nope")])
        seam = _transcript(_messages(5), 5, _FakeMonitor())
        with seam[0] as recent, seam[1], seam[2]:
            assert await _auto_create(client) is False

        recent.assert_not_awaited()
        _no_pacing.assert_not_awaited()


class TestWindowPickerBind:
    """Binding an existing session into a topic the user already opened."""

    @staticmethod
    def _update() -> MagicMock:
        update = MagicMock()
        update.callback_query.message.chat.id = CHAT
        update.message = None
        return update

    async def _bind(self, client: FakeTelegramClient, monitor: _FakeMonitor) -> None:
        # Lazy import mirrors the module under test living behind the registry.
        from ccgram.handlers.topics import window_callbacks

        window = MagicMock(window_name="my-project", pane_current_command="claude")
        context = MagicMock()
        context.user_data = {window_callbacks.UNBOUND_WINDOWS_KEY: [WINDOW]}
        seam = _transcript(_messages(25), 25, monitor)
        with (
            seam[0],
            seam[1],
            seam[2],
            patch.object(window_callbacks, "tmux_manager") as mux,
            patch.object(window_callbacks, "thread_router") as tr,
            patch.object(window_callbacks, "get_thread_id", return_value=THREAD),
            patch.object(window_callbacks, "_detect_and_setup_provider", AsyncMock()),
            patch.object(window_callbacks, "_rename_forum_topic", AsyncMock()),
            patch.object(window_callbacks, "safe_edit", AsyncMock()),
            patch.object(window_callbacks, "PTBTelegramClient", lambda _bot: client),
        ):
            mux.find_window_by_id = AsyncMock(return_value=window)
            tr.resolve_chat_id.return_value = CHAT
            await window_callbacks._handle_bind(
                MagicMock(answer=AsyncMock()),
                USER,
                f"{window_callbacks.CB_WIN_BIND}0",
                self._update(),
                context,
            )

    async def test_flag_off_replays_nothing(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock
    ) -> None:
        monitor = _FakeMonitor()
        await self._bind(client, monitor)

        assert monitor.calls == []
        _no_pacing.assert_not_awaited()

    async def test_a_picked_window_replays_into_its_topic(
        self, client: FakeTelegramClient, _no_pacing: AsyncMock, backfill_10: None
    ) -> None:
        monitor = _FakeMonitor()
        await self._bind(client, monitor)

        assert monitor.calls == [("sess-1", TRANSCRIPT)]
        texts = _texts(_no_pacing)
        assert texts[0] == "⏮ 25 messages in this session, 15 skipped — last 10 below"
        assert len(texts) == 11
        assert set(_threads(_no_pacing)) == {THREAD}
