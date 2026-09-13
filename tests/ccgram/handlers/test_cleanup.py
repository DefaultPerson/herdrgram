from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from ccgram.config import config
from ccgram.handlers.cleanup import clear_topic_state, close_command, rollback_command


class TestClearTopicState:
    async def test_enqueues_status_clear_when_bot_available(self) -> None:
        bot = AsyncMock()
        with (
            patch("ccgram.handlers.cleanup.enqueue_status_update") as mock_enqueue,
            patch("ccgram.handlers.cleanup.clear_interactive_msg"),
            patch("ccgram.thread_router.thread_router") as mock_tr,
        ):
            mock_tr.resolve_chat_id.return_value = -100
            await clear_topic_state(1, 42, client=bot, window_id="@0")

        mock_enqueue.assert_called_once()
        args = mock_enqueue.call_args
        assert args[0][1] == 1
        assert args[0][2] == "@0"
        assert args[0][3] is None
        assert args[1]["thread_id"] == 42

    async def test_skips_enqueue_when_no_bot(self) -> None:
        with (
            patch("ccgram.handlers.cleanup.enqueue_status_update") as mock_enqueue,
            patch("ccgram.handlers.cleanup.clear_interactive_msg"),
            patch("ccgram.thread_router.thread_router") as mock_tr,
        ):
            mock_tr.resolve_chat_id.return_value = -100
            await clear_topic_state(1, 42, client=None, window_id="@0")

        mock_enqueue.assert_not_called()

    async def test_enqueues_empty_window_id_when_none(self) -> None:
        bot = AsyncMock()
        with (
            patch("ccgram.handlers.cleanup.enqueue_status_update") as mock_enqueue,
            patch("ccgram.handlers.cleanup.clear_interactive_msg"),
            patch("ccgram.thread_router.thread_router") as mock_tr,
        ):
            mock_tr.resolve_chat_id.return_value = -100
            await clear_topic_state(1, 42, client=bot, window_id=None)

        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args[0][2] == ""

    async def test_revokes_window_callback_tokens(self) -> None:
        with (
            patch("ccgram.handlers.cleanup.clear_interactive_msg"),
            patch("ccgram.thread_router.thread_router") as mock_tr,
            patch("ccgram.handlers.cleanup.revoke_window_tokens") as revoke,
        ):
            mock_tr.resolve_chat_id.return_value = -100
            await clear_topic_state(1, 42, client=None, window_id="@0")

        revoke.assert_called_once_with("@0")


class TestClearTopicStateQualifiedId:
    """qualified_id is built from session_map_prefix(), not hardcoded tmux prefix."""

    @pytest.fixture
    def _common_patches(self):
        with (
            patch("ccgram.handlers.cleanup.enqueue_status_update"),
            patch("ccgram.handlers.cleanup.clear_interactive_msg"),
            patch("ccgram.thread_router.thread_router") as mock_tr,
            patch("ccgram.handlers.cleanup.topic_state") as mock_ts,
        ):
            mock_tr.resolve_chat_id.return_value = -100
            yield mock_ts

    async def test_qualified_id_tmux_prefix(self, monkeypatch, _common_patches) -> None:
        monkeypatch.setattr(config, "multiplexer_name", "tmux")
        monkeypatch.setattr(config, "tmux_session_name", "ccgram")
        mock_ts = _common_patches
        await clear_topic_state(1, 42, client=None, window_id="@5", window_dead=True)
        kwargs = mock_ts.clear_all.call_args[1]
        assert kwargs["qualified_id"] == "ccgram:@5"

    async def test_qualified_id_herdr_prefix(
        self, monkeypatch, _common_patches
    ) -> None:
        monkeypatch.setattr(config, "multiplexer_name", "herdr")
        mock_ts = _common_patches
        target = "herdr-session-v1-" + "a" * 64
        await clear_topic_state(1, 42, client=None, window_id=target, window_dead=True)
        kwargs = mock_ts.clear_all.call_args[1]
        assert kwargs["qualified_id"] == f"herdr:{target}"

    async def test_qualified_id_none_when_window_alive(
        self, monkeypatch, _common_patches
    ) -> None:
        monkeypatch.setattr(config, "multiplexer_name", "herdr")
        mock_ts = _common_patches
        # window_dead defaults to True; pass False to suppress qualified_id
        await clear_topic_state(
            1,
            42,
            client=None,
            window_id="herdr-session-v1-" + "a" * 64,
            window_dead=False,
        )
        kwargs = mock_ts.clear_all.call_args[1]
        assert kwargs["qualified_id"] is None


class TestRollbackCommand:
    async def test_restores_only_this_topics_archived_binding(self) -> None:
        update = AsyncMock()
        update.effective_user.id = 1
        update.message = AsyncMock()
        with (
            patch("ccgram.handlers.cleanup.config") as mock_config,
            patch("ccgram.handlers.cleanup.get_thread_id", return_value=42),
            patch("ccgram.handlers.cleanup.thread_router") as router,
            patch("ccgram.handlers.cleanup.legacy_state") as legacy_state,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.rollback_legacy_herdr_binding",
                return_value=True,
            ) as rollback,
            patch("ccgram.handlers.cleanup.safe_reply", new_callable=AsyncMock),
        ):
            mock_config.is_user_allowed.return_value = True
            router.get_window_for_thread.return_value = None
            legacy_state.get_archived_legacy_herdr_binding.return_value = "w2:t1"

            await rollback_command(update, AsyncMock())

        legacy_state.get_archived_legacy_herdr_binding.assert_called_once_with(1, 42)
        rollback.assert_called_once_with(1, 42, "w2:t1")

    async def test_does_not_restore_another_users_archived_binding(self) -> None:
        update = AsyncMock()
        update.effective_user.id = 2
        update.message = AsyncMock()
        with (
            patch("ccgram.handlers.cleanup.config") as mock_config,
            patch("ccgram.handlers.cleanup.get_thread_id", return_value=42),
            patch("ccgram.handlers.cleanup.thread_router") as router,
            patch("ccgram.handlers.cleanup.legacy_state") as legacy_state,
            patch("ccgram.handlers.cleanup.safe_reply", new_callable=AsyncMock),
        ):
            mock_config.is_user_allowed.return_value = True
            router.get_window_for_thread.return_value = None
            legacy_state.get_archived_legacy_herdr_binding.return_value = None

            await rollback_command(update, AsyncMock())

        legacy_state.get_archived_legacy_herdr_binding.assert_called_once_with(2, 42)


# ── /close ────────────────────────────────────────────────────────────────

USER_ID = 4242


def _close_update(thread_id: int | None = 42, chat_id: int = -100):
    """An /close update in a topic (or in General when thread_id is None)."""
    update = MagicMock()
    update.effective_user = MagicMock(id=USER_ID)
    update.effective_chat = MagicMock(id=chat_id)
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    return update


@contextmanager
def _close_env(
    window_id: str | None, *, close_result=True, delete_result=True, allowed=True
):
    """Patch the seam /close runs on: router, state teardown, Telegram calls."""
    client = AsyncMock()
    client.close_forum_topic = (
        AsyncMock(side_effect=close_result)
        if isinstance(close_result, Exception)
        else AsyncMock(return_value=close_result)
    )
    client.delete_forum_topic = (
        AsyncMock(side_effect=delete_result)
        if isinstance(delete_result, Exception)
        else AsyncMock(return_value=delete_result)
    )
    with (
        patch("ccgram.handlers.cleanup.config") as cfg,
        patch("ccgram.handlers.cleanup.thread_router") as tr,
        patch("ccgram.handlers.cleanup.clear_topic_state", AsyncMock()) as clear,
        patch("ccgram.handlers.cleanup.enqueue_status_update", AsyncMock()),
        patch("ccgram.handlers.cleanup.PTBTelegramClient", return_value=client),
        patch("ccgram.handlers.cleanup.safe_reply", AsyncMock()) as reply,
        patch("ccgram.handlers.cleanup.safe_send", AsyncMock()) as send,
    ):
        cfg.is_user_allowed.return_value = allowed
        tr.get_window_for_thread.return_value = window_id
        tr.get_display_name.return_value = "spreads"
        tr.resolve_chat_id.return_value = -100
        yield SimpleNamespace(client=client, tr=tr, clear=clear, reply=reply, send=send)


class TestCloseCommand:
    async def test_closes_the_topic_and_keeps_the_session(self) -> None:
        with _close_env("@7") as env:
            await close_command(_close_update(), MagicMock())

        env.client.close_forum_topic.assert_awaited_once()
        assert env.client.close_forum_topic.await_args.kwargs == {
            "chat_id": -100,
            "message_thread_id": 42,
        }
        env.client.delete_forum_topic.assert_not_awaited()
        # The binding is dropped, the window is explicitly kept alive.
        assert env.clear.await_args.kwargs["window_dead"] is False
        env.tr.unbind_thread.assert_called_once()
        note = env.send.await_args.args[2]
        assert "Topic closed: spreads" in note
        assert "keeps running" in note

    async def test_deletes_where_telegram_cannot_close(self) -> None:
        """A private chat with topics refuses closeForumTopic; delete is the only removal."""
        refusal = BadRequest("the chat is not a supergroup forum")
        with _close_env("@7", close_result=refusal) as env:
            await close_command(_close_update(), MagicMock())

        env.client.delete_forum_topic.assert_awaited_once()
        assert "Topic deleted: spreads" in env.send.await_args.args[2]

    async def test_other_bad_request_is_reported_and_nothing_is_deleted(self) -> None:
        with _close_env("@7", close_result=BadRequest("TOPIC_NOT_MODIFIED")) as env:
            await close_command(_close_update(), MagicMock())

        env.client.delete_forum_topic.assert_not_awaited()
        env.send.assert_not_awaited()
        text = env.reply.await_args.args[1]
        assert "would not remove this topic" in text
        assert "TOPIC_NOT_MODIFIED" in text
        # The binding is still dropped: the session outlives the failure.
        env.tr.unbind_thread.assert_called_once()

    async def test_a_topic_that_is_already_gone_counts_as_closed(self) -> None:
        with _close_env(
            "@7", close_result=BadRequest("Message thread not found")
        ) as env:
            await close_command(_close_update(), MagicMock())

        env.client.delete_forum_topic.assert_not_awaited()
        assert "Topic deleted: spreads" in env.send.await_args.args[2]

    async def test_unbound_topic_is_still_removed(self) -> None:
        with _close_env(None) as env:
            await close_command(_close_update(), MagicMock())

        env.client.close_forum_topic.assert_awaited_once()
        env.tr.unbind_thread.assert_not_called()
        assert "No session was bound" in env.send.await_args.args[2]

    async def test_general_topic_is_never_removed(self) -> None:
        with (
            _close_env("@7") as env,
            patch("ccgram.handlers.cleanup.is_general_topic", return_value=True),
            patch(
                "ccgram.handlers.cleanup.handle_general_topic_message", AsyncMock()
            ) as general,
        ):
            await close_command(_close_update(thread_id=None), MagicMock())

        general.assert_awaited_once()
        env.client.close_forum_topic.assert_not_awaited()
        env.client.delete_forum_topic.assert_not_awaited()

    async def test_unauthorized_user_is_ignored(self) -> None:
        with _close_env("@7", allowed=False) as env:
            await close_command(_close_update(), MagicMock())

        env.client.close_forum_topic.assert_not_awaited()
        env.tr.unbind_thread.assert_not_called()
