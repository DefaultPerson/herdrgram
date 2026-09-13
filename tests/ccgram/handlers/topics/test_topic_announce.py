"""CCGRAM_TOPIC_ON_DEMAND: offer a topic, open it on request, replay the tail."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from ccgram.handlers.topics import topic_announce
from ccgram.handlers.topics.topic_announce import (
    ANNOUNCE_DIED,
    ANNOUNCE_GONE,
    ANNOUNCE_HIDDEN,
    ANNOUNCE_OPENED,
    BUTTON_HIDE,
    BUTTON_OPEN,
)
from ccgram.handlers.topics.topic_orchestration import handle_new_window
from ccgram.multiplexer.base import WindowRef
from ccgram.session_monitor import NewWindowEvent
from ccgram.telegram_client import FakeTelegramClient

WINDOW = "ccgram-herdr-session-v1-" + "a" * 64
CHAT = -100500
USER = 12345


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Give every test its own announcement record on disk."""
    monkeypatch.setattr(topic_announce.config, "config_dir", tmp_path)
    topic_announce.reset_for_testing()
    yield
    topic_announce.reset_for_testing()


@pytest.fixture
def on_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topic_announce.config, "topic_on_demand", True)


@pytest.fixture
def client() -> FakeTelegramClient:
    fake = FakeTelegramClient()
    counter = {"n": 0}

    def _sent(**kwargs: object) -> MagicMock:
        counter["n"] += 1
        message = MagicMock()
        message.message_id = 1000 + counter["n"]
        message.chat.id = kwargs.get("chat_id")
        return message

    fake.returns["send_message"] = _sent
    return fake


def _event(name: str = "Claude ▸ ws ▸ tab") -> NewWindowEvent:
    return NewWindowEvent(
        window_id=WINDOW,
        session_id="",
        window_name=name,
        cwd="/home/user/proj",
    )


def _window_ref(name: str = "Claude ▸ ws ▸ tab", *, eligible: bool = True) -> WindowRef:
    return WindowRef(
        window_id=WINDOW,
        window_name=name,
        cwd="/home/user/proj",
        topic_eligible=eligible,
    )


def _sends(client: FakeTelegramClient) -> list[dict]:
    return [call.kwargs for call in client.calls if call.method == "send_message"]


def _edits(client: FakeTelegramClient) -> list[dict]:
    return [call.kwargs for call in client.calls if call.method == "edit_message_text"]


def _orchestration(chats: set[int] | None = None):
    """Patch the orchestration seam the way the topic-creation tests do."""
    chats = {CHAT} if chats is None else chats
    return (
        patch("ccgram.handlers.topics.topic_orchestration.session_manager"),
        patch("ccgram.handlers.topics.topic_orchestration.thread_router"),
        patch("ccgram.handlers.topics.topic_orchestration.config"),
        patch(
            "ccgram.handlers.topics.topic_orchestration.collect_target_chats",
            return_value=chats,
        ),
        patch(
            "ccgram.handlers.topics.topic_announce.collect_target_chats",
            return_value=chats,
        ),
        patch("ccgram.handlers.topics.topic_announce.window_query"),
        patch("ccgram.handlers.topics.topic_orchestration.tmux_manager"),
    )


async def _discover(client: FakeTelegramClient, event: NewWindowEvent | None = None):
    """Run one automatic discovery of an unbound window."""
    event = event or _event()
    ctx = _orchestration()
    with (
        ctx[0],
        ctx[1] as tr,
        ctx[2] as cfg,
        ctx[3],
        ctx[4],
        ctx[5] as wq,
        ctx[6] as mux,
    ):
        tr.has_window.return_value = False
        tr.iter_thread_bindings.return_value = iter([])
        cfg.group_id = CHAT
        cfg.allowed_users = {USER}
        wq.get_window_provider.return_value = "claude"
        mux.find_window_by_id = AsyncMock(return_value=None)
        mux.capabilities.supports_display_name_rebind = False
        return await handle_new_window(event, client)


# ── Offering instead of creating ───────────────────────────────────────────


class TestOffer:
    async def test_flag_off_creates_the_topic_and_announces_nothing(
        self, client: FakeTelegramClient
    ) -> None:
        client.returns["create_forum_topic"] = MagicMock(message_thread_id=42)

        assert await _discover(client) is True

        assert client.call_count("create_forum_topic") == 1
        assert _sends(client) == []
        assert topic_announce.has_announcement(WINDOW) is False

    @pytest.mark.usefixtures("on_demand")
    async def test_flag_on_offers_a_topic_instead_of_creating_one(
        self, client: FakeTelegramClient
    ) -> None:
        assert await _discover(client) is False

        assert client.call_count("create_forum_topic") == 0
        sent = _sends(client)
        assert len(sent) == 1
        assert sent[0]["chat_id"] == CHAT
        # General is addressed by the absence of a thread id, in a forum and in
        # a private chat with topics alike.
        assert "message_thread_id" not in sent[0]
        assert "🆕" in sent[0]["text"]
        assert "Claude ▸ ws ▸ tab" in sent[0]["text"]
        assert "/home/user/proj" in sent[0]["text"]
        assert "claude" in sent[0]["text"]
        assert topic_announce.has_announcement(WINDOW) is True

    @pytest.mark.usefixtures("on_demand")
    async def test_the_keyboard_carries_short_resolvable_tokens(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)

        keyboard = _sends(client)[0]["reply_markup"]
        buttons = keyboard.inline_keyboard[0]
        assert [button.text for button in buttons] == [BUTTON_OPEN, BUTTON_HIDE]
        for button in buttons:
            data = button.callback_data
            assert len(data.encode("utf-8")) <= 64
            prefix = "an:open:" if data.startswith("an:open:") else "an:hide:"
            assert topic_announce.resolve_token(data[len(prefix) :]) == WINDOW

    @pytest.mark.usefixtures("on_demand")
    async def test_a_window_is_offered_once(self, client: FakeTelegramClient) -> None:
        await _discover(client)
        await _discover(client)

        assert len(_sends(client)) == 1

    @pytest.mark.usefixtures("on_demand")
    async def test_a_restart_does_not_re_offer_a_live_offer(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        topic_announce.reset_for_testing()  # as if the process restarted

        await _discover(client)

        assert len(_sends(client)) == 1
        assert topic_announce.has_announcement(WINDOW) is True

    @pytest.mark.usefixtures("on_demand")
    async def test_each_target_chat_gets_one_offer(
        self, client: FakeTelegramClient
    ) -> None:
        ctx = _orchestration({CHAT, -100600})
        with (
            ctx[0],
            ctx[1] as tr,
            ctx[2] as cfg,
            ctx[3],
            ctx[4],
            ctx[5] as wq,
            ctx[6] as mux,
        ):
            tr.has_window.return_value = False
            tr.iter_thread_bindings.return_value = iter([])
            cfg.group_id = CHAT
            cfg.allowed_users = {USER}
            wq.get_window_provider.return_value = "claude"
            mux.find_window_by_id = AsyncMock(return_value=None)
            mux.capabilities.supports_display_name_rebind = False
            await handle_new_window(_event(), client)

        assert {send["chat_id"] for send in _sends(client)} == {CHAT, -100600}
        assert len(_sends(client)) == 2

    @pytest.mark.usefixtures("on_demand")
    async def test_an_offer_nobody_could_receive_is_not_recorded(
        self, client: FakeTelegramClient
    ) -> None:
        # Two: the sender retries once in plain text before giving up.
        client.set_side_effect(
            "send_message",
            [BadRequest("chat not found"), BadRequest("chat not found")],
        )

        await _discover(client)

        assert topic_announce.has_announcement(WINDOW) is False

    @pytest.mark.usefixtures("on_demand")
    async def test_a_dismissed_window_is_never_offered_again(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        await topic_announce.hide_announcement(client, WINDOW)
        client.calls.clear()

        await _discover(client)

        assert _sends(client) == []
        assert topic_announce.is_dismissed(WINDOW) is True


# ── Hiding ─────────────────────────────────────────────────────────────────


class TestHide:
    @pytest.mark.usefixtures("on_demand")
    async def test_hide_deletes_the_offer_and_remembers_the_refusal(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        message_id = _sends(client)[0] and client.last_call("send_message")
        assert message_id is not None

        await topic_announce.hide_announcement(client, WINDOW)

        deleted = client.last_call("delete_message")
        assert deleted is not None
        assert deleted.kwargs["chat_id"] == CHAT
        assert topic_announce.has_announcement(WINDOW) is False
        assert topic_announce.is_dismissed(WINDOW) is True

    @pytest.mark.usefixtures("on_demand")
    async def test_an_undeletable_offer_is_edited_down_instead(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        client.returns["delete_message"] = False

        await topic_announce.hide_announcement(client, WINDOW)

        assert _edits(client)[-1]["text"] == ANNOUNCE_HIDDEN.format(
            name="Claude ▸ ws ▸ tab"
        )
        assert topic_announce.is_dismissed(WINDOW) is True

    @pytest.mark.usefixtures("on_demand")
    async def test_a_refusal_survives_a_restart(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        await topic_announce.hide_announcement(client, WINDOW)

        topic_announce.reset_for_testing()

        assert topic_announce.is_dismissed(WINDOW) is True


# ── Death of an unanswered offer ───────────────────────────────────────────


class TestReconcile:
    @pytest.mark.usefixtures("on_demand")
    async def test_a_live_window_keeps_its_offer(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)

        await topic_announce.reconcile_announcements(client, [_window_ref()])

        assert _edits(client) == []
        assert topic_announce.has_announcement(WINDOW) is True

    @pytest.mark.usefixtures("on_demand")
    async def test_a_dead_window_retires_its_offer(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)

        await topic_announce.reconcile_announcements(client, [])

        assert _edits(client)[-1]["text"] == ANNOUNCE_DIED.format(
            name="Claude ▸ ws ▸ tab"
        )
        assert _edits(client)[-1].get("reply_markup") is None
        assert topic_announce.has_announcement(WINDOW) is False

    @pytest.mark.usefixtures("on_demand")
    async def test_a_dead_window_stops_being_remembered_as_dismissed(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        await topic_announce.hide_announcement(client, WINDOW)

        await topic_announce.reconcile_announcements(client, [])

        assert topic_announce.is_dismissed(WINDOW) is False


# ── Opening a topic from an offer ──────────────────────────────────────────


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


class _FakeMonitor:
    def __init__(self, offset: int | None = 4096) -> None:
        self.offset = offset
        self.calls: list[tuple[str, Path]] = []

    async def mark_delivered_to_eof(self, session_id: str, path: Path) -> int | None:
        self.calls.append((session_id, path))
        return self.offset


def _open_seam(
    messages: list[dict],
    total: int,
    *,
    monitor: _FakeMonitor | None = None,
    thread_id: int | None = 77,
):
    monitor = monitor or _FakeMonitor()
    identity = MagicMock(session_id="sess-1", transcript_path=Path("/t/sess-1.jsonl"))
    bindings = [(USER, CHAT, thread_id, WINDOW)] if thread_id is not None else []
    return (
        patch(
            "ccgram.handlers.topics.topic_announce.handle_new_window",
            AsyncMock(return_value=True),
        ),
        patch(
            "ccgram.handlers.topics.topic_announce.session_query.get_recent_messages",
            AsyncMock(return_value=(messages, total)),
        ),
        patch(
            "ccgram.handlers.topics.topic_announce.identity_state.get_identity",
            return_value=identity,
        ),
        patch(
            "ccgram.handlers.topics.topic_announce.get_active_monitor",
            return_value=monitor,
        ),
        patch("ccgram.handlers.topics.topic_announce.thread_router"),
        patch(
            "ccgram.multiplexer.reconciliation.list_windows_for_reconciliation",
            AsyncMock(return_value=[_window_ref()]),
        ),
        monitor,
        bindings,
    )


class TestOpen:
    @pytest.mark.usefixtures("on_demand")
    async def test_open_runs_the_ordinary_creation_path(
        self, client: FakeTelegramClient
    ) -> None:
        """The topic is created by create_topic_in_chat, icons and naming alike."""
        await _discover(client)
        client.returns["create_forum_topic"] = MagicMock(message_thread_id=42)
        ctx = _orchestration()
        with (
            ctx[0],
            ctx[1] as tr,
            ctx[2] as cfg,
            ctx[3],
            ctx[4],
            ctx[5],
            ctx[6] as mux,
            patch(
                "ccgram.multiplexer.reconciliation.list_windows_for_reconciliation",
                AsyncMock(return_value=[_window_ref("Claude ▸ ws ▸ renamed")]),
            ),
            patch("ccgram.handlers.topics.topic_announce.thread_router") as announce_tr,
        ):
            announce_tr.has_window.return_value = False
            tr.has_window.return_value = False
            tr.iter_thread_bindings.return_value = iter([])
            cfg.group_id = CHAT
            cfg.allowed_users = {USER}
            mux.find_window_by_id = AsyncMock(return_value=None)
            mux.capabilities.supports_display_name_rebind = False

            assert (
                await topic_announce.open_topic_from_announcement(
                    client, WINDOW, USER, CHAT
                )
                is True
            )

        created = client.last_call("create_forum_topic")
        assert created is not None
        assert created.kwargs["chat_id"] == CHAT
        assert created.kwargs["name"] == "Claude ▸ ws ▸ renamed"

    @pytest.mark.usefixtures("on_demand")
    async def test_open_backfills_the_tail_and_seals_the_watermark(
        self, client: FakeTelegramClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_announce.config, "topic_backfill_messages", 10)
        await _discover(client)
        client.calls.clear()
        seam = _open_seam(_messages(25), 25)
        monitor = seam[6]
        with seam[0], seam[1] as recent, seam[2], seam[3], seam[4] as tr, seam[5]:
            tr.has_window.return_value = False
            tr.iter_thread_bindings_with_chat.return_value = iter(seam[7])
            await topic_announce.open_topic_from_announcement(
                client, WINDOW, USER, CHAT
            )

        # The watermark is sealed before the replay is read, and the replay is
        # bounded by it, so nothing sent here is also delivered live.
        assert monitor.calls == [("sess-1", Path("/t/sess-1.jsonl"))]
        assert recent.await_args is not None
        assert recent.await_args.kwargs["end_byte"] == 4096

        topic_sends = [send for send in _sends(client) if send.get("message_thread_id")]
        assert len(topic_sends) == 11  # one header, then exactly ten messages
        assert topic_sends[0]["text"] == (
            "⏮ В сессии 25 сообщений, пропущено 15 — ниже последние 10"
        )
        assert all(send["message_thread_id"] == 77 for send in topic_sends)
        # Chronological order, user turns prefixed the way /history prefixes them.
        assert topic_sends[1]["text"] == "message 15"  # assistant turn
        assert topic_sends[2]["text"] == "👤 message 16"
        assert topic_sends[-1]["text"] == "👤 message 24"

    @pytest.mark.usefixtures("on_demand")
    async def test_a_short_session_reports_the_whole_history(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        client.calls.clear()
        seam = _open_seam(_messages(3), 3)
        with seam[0], seam[1], seam[2], seam[3], seam[4] as tr, seam[5]:
            tr.has_window.return_value = False
            tr.iter_thread_bindings_with_chat.return_value = iter(seam[7])
            await topic_announce.open_topic_from_announcement(
                client, WINDOW, USER, CHAT
            )

        topic_sends = [send for send in _sends(client) if send.get("message_thread_id")]
        assert topic_sends[0]["text"] == "⏮ Загружена вся история: 3 сообщений"
        assert len(topic_sends) == 4

    @pytest.mark.usefixtures("on_demand")
    async def test_a_silent_session_gets_no_replay_at_all(
        self, client: FakeTelegramClient
    ) -> None:
        """A brand-new session has nothing to report about nothing."""
        await _discover(client)
        client.calls.clear()
        seam = _open_seam([], 0)
        with seam[0], seam[1], seam[2], seam[3], seam[4] as tr, seam[5]:
            tr.has_window.return_value = False
            tr.iter_thread_bindings_with_chat.return_value = iter(seam[7])
            await topic_announce.open_topic_from_announcement(
                client, WINDOW, USER, CHAT
            )

        assert [send for send in _sends(client) if send.get("message_thread_id")] == []

    @pytest.mark.usefixtures("on_demand")
    async def test_a_zero_backfill_limit_replays_nothing(
        self, client: FakeTelegramClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_announce.config, "topic_backfill_messages", 0)
        await _discover(client)
        client.calls.clear()
        seam = _open_seam(_messages(5), 5)
        with seam[0], seam[1] as recent, seam[2], seam[3], seam[4] as tr, seam[5]:
            tr.has_window.return_value = False
            tr.iter_thread_bindings_with_chat.return_value = iter(seam[7])
            await topic_announce.open_topic_from_announcement(
                client, WINDOW, USER, CHAT
            )

        recent.assert_not_awaited()
        assert [send for send in _sends(client) if send.get("message_thread_id")] == []

    @pytest.mark.usefixtures("on_demand")
    async def test_opening_retires_the_offer(self, client: FakeTelegramClient) -> None:
        await _discover(client)
        seam = _open_seam(_messages(2), 2)
        with seam[0], seam[1], seam[2], seam[3], seam[4] as tr, seam[5]:
            tr.has_window.return_value = False
            tr.iter_thread_bindings_with_chat.return_value = iter(seam[7])
            await topic_announce.open_topic_from_announcement(
                client, WINDOW, USER, CHAT
            )

        assert _edits(client)[-1]["text"] == ANNOUNCE_OPENED.format(
            name="Claude ▸ ws ▸ tab"
        )
        assert _edits(client)[-1].get("reply_markup") is None
        assert topic_announce.has_announcement(WINDOW) is False
        # Not dismissed: unbinding the topic later may offer the window again.
        assert topic_announce.is_dismissed(WINDOW) is False

    @pytest.mark.usefixtures("on_demand")
    async def test_a_second_press_does_not_open_a_second_topic(
        self, client: FakeTelegramClient
    ) -> None:
        """The duplicate-topic failure this whole feature exists to avoid."""
        await _discover(client)
        seam = _open_seam(_messages(2), 2)
        with seam[0] as created, seam[1], seam[2], seam[3], seam[4] as tr, seam[5]:
            tr.has_window.return_value = True
            tr.iter_thread_bindings_with_chat.return_value = iter(seam[7])
            assert (
                await topic_announce.open_topic_from_announcement(
                    client, WINDOW, USER, CHAT
                )
                is False
            )

        created.assert_not_awaited()
        assert _edits(client)[-1]["text"] == ANNOUNCE_OPENED.format(
            name="Claude ▸ ws ▸ tab"
        )
        assert topic_announce.has_announcement(WINDOW) is False

    @pytest.mark.usefixtures("on_demand")
    async def test_opening_a_window_that_died_first_reports_the_death(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        with patch(
            "ccgram.multiplexer.reconciliation.list_windows_for_reconciliation",
            AsyncMock(return_value=[]),
        ):
            assert (
                await topic_announce.open_topic_from_announcement(
                    client, WINDOW, USER, CHAT
                )
                is False
            )

        assert _edits(client)[-1]["text"] == ANNOUNCE_DIED.format(
            name="Claude ▸ ws ▸ tab"
        )
        assert topic_announce.has_announcement(WINDOW) is False


# ── Callback dispatch ──────────────────────────────────────────────────────


def _query(data: str) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message.chat.id = CHAT
    query.message.message_id = 4242
    return query


def _update(query: MagicMock) -> MagicMock:
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = USER
    return update


class TestCallback:
    @pytest.mark.usefixtures("on_demand")
    async def test_the_query_is_answered_before_the_work(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        token = _sends(client)[0]["reply_markup"].inline_keyboard[0][0].callback_data
        query = _query(token)
        context = MagicMock()

        with patch(
            "ccgram.handlers.topics.topic_announce.open_topic_from_announcement",
            AsyncMock(return_value=True),
        ) as opened:
            await topic_announce.handle_announce_callback(_query_update(query), context)

        query.answer.assert_awaited_once_with()
        assert opened.await_args is not None
        assert opened.await_args.args[1:] == (WINDOW, USER, CHAT)

    @pytest.mark.usefixtures("on_demand")
    async def test_the_hide_button_routes_to_the_refusal(
        self, client: FakeTelegramClient
    ) -> None:
        await _discover(client)
        token = _sends(client)[0]["reply_markup"].inline_keyboard[0][1].callback_data

        with patch(
            "ccgram.handlers.topics.topic_announce.hide_announcement",
            AsyncMock(),
        ) as hidden:
            await topic_announce.handle_announce_callback(
                _query_update(_query(token)), MagicMock()
            )

        assert hidden.await_args is not None
        assert hidden.await_args.args[1] == WINDOW

    async def test_a_token_from_a_forgotten_offer_says_so(
        self, client: FakeTelegramClient
    ) -> None:
        query = _query("an:open:nosuchtoken")

        with patch(
            "ccgram.handlers.topics.topic_announce.edit_with_fallback",
            AsyncMock(return_value=True),
        ) as edited:
            await topic_announce.handle_announce_callback(
                _query_update(query), MagicMock()
            )

        assert edited.await_args is not None
        assert edited.await_args.args[3] == ANNOUNCE_GONE


def _query_update(query: MagicMock) -> MagicMock:
    return _update(query)


# ── Persistence shape ──────────────────────────────────────────────────────


@pytest.mark.usefixtures("on_demand")
async def test_the_record_on_disk_is_readable_json(
    client: FakeTelegramClient, tmp_path: Path
) -> None:
    await _discover(client)

    raw = json.loads((tmp_path / "topic_announcements.json").read_text())

    assert list(raw["announced"]) == [WINDOW]
    entry = raw["announced"][WINDOW]
    assert entry["name"] == "Claude ▸ ws ▸ tab"
    assert list(entry["targets"]) == [str(CHAT)]
    assert topic_announce.resolve_token(entry["token"]) == WINDOW


@pytest.mark.usefixtures("on_demand")
async def test_a_corrupt_record_does_not_stop_an_offer(
    client: FakeTelegramClient, tmp_path: Path
) -> None:
    (tmp_path / "topic_announcements.json").write_text("{not json")

    await _discover(client)

    assert topic_announce.has_announcement(WINDOW) is True
