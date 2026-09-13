"""Tests for FORUM_TOPIC_CLOSED handler (unbind thread, keep window)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.multiplexer.base import WindowRef

from ccgram.handlers.topics.topic_lifecycle import topic_closed_handler

GENERAL_TOPIC_THREAD_ID = 1


def _make_update(thread_id: int | None = 42, user_id: int = 1) -> MagicMock:
    """Create a mock Update for FORUM_TOPIC_CLOSED."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.message_thread_id = thread_id
    return update


@pytest.fixture
def router():
    with patch("ccgram.handlers.topics.topic_lifecycle.thread_router") as mock_tr:
        yield mock_tr


@pytest.fixture
def clear_state():
    with patch(
        "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
        new_callable=AsyncMock,
    ) as mock_clear:
        yield mock_clear


@pytest.fixture
def allowed_user():
    with patch("ccgram.config.Config.is_user_allowed", return_value=True):
        yield


@pytest.mark.usefixtures("allowed_user")
class TestTopicClosedHandler:
    async def test_unbinds_bound_topic(
        self, router: MagicMock, clear_state: AsyncMock
    ) -> None:
        router.get_window_for_thread.return_value = "@0"
        router.get_display_name.return_value = "my-project"
        ctx = MagicMock()

        await topic_closed_handler(_make_update(), ctx)

        router.get_window_for_thread.assert_called_once_with(1, 42)
        router.unbind_thread.assert_called_once_with(
            1, 42, retirement_reason="remote_closed"
        )
        clear_args = clear_state.call_args
        assert clear_args.args[0:2] == (1, 42)
        assert clear_args.args[2].bot is ctx.bot
        assert clear_args.args[3] is ctx.user_data
        assert clear_args.kwargs == {"window_id": "@0", "window_dead": False}

    async def test_skips_unbound_topic(
        self, router: MagicMock, clear_state: AsyncMock
    ) -> None:
        router.get_window_for_thread.return_value = None

        await topic_closed_handler(_make_update(), MagicMock())

        router.unbind_thread.assert_not_called()
        clear_state.assert_not_called()

    @pytest.mark.parametrize(
        "thread_id",
        [GENERAL_TOPIC_THREAD_ID, None],
        ids=["general_topic", "no_thread_id"],
    )
    async def test_skips_non_forum_thread(
        self, router: MagicMock, clear_state: AsyncMock, thread_id: int | None
    ) -> None:
        await topic_closed_handler(_make_update(thread_id=thread_id), MagicMock())

        router.get_window_for_thread.assert_not_called()
        clear_state.assert_not_called()


@pytest.mark.usefixtures("allowed_user")
class TestTopicClosedKillFlag:
    """CCGRAM_KILL_ON_TOPIC_CLOSE ends the session with its topic."""

    @staticmethod
    async def _close_bound_topic(
        router: MagicMock,
        *,
        kill_on_topic_close: bool,
        listing: list[WindowRef] | None = None,
        kill_ok: bool = True,
    ) -> MagicMock:
        from ccgram.handlers.topics import topic_lifecycle as tl

        router.get_window_for_thread.return_value = "@0"
        router.get_display_name.return_value = "my-project"
        with (
            patch.object(tl.config, "kill_on_topic_close", kill_on_topic_close),
            patch.object(tl, "tmux_manager") as mock_tmux,
        ):
            # Presence goes through the real reconciliation seam, so a backend
            # that cannot produce a listing stays "unknown" rather than "gone".
            mock_tmux.list_windows_for_reconciliation = AsyncMock(return_value=listing)
            mock_tmux.kill_window = AsyncMock(return_value=kill_ok)
            await topic_closed_handler(_make_update(), MagicMock())
        return mock_tmux

    async def test_flag_off_keeps_the_window_alive(
        self, router: MagicMock, clear_state: AsyncMock
    ) -> None:
        mux = await self._close_bound_topic(router, kill_on_topic_close=False)

        mux.kill_window.assert_not_called()
        mux.list_windows_for_reconciliation.assert_not_called()
        assert clear_state.call_args.kwargs["window_dead"] is False
        router.unbind_thread.assert_called_once_with(
            1, 42, retirement_reason="remote_closed"
        )

    async def test_flag_on_kills_a_present_window(
        self, router: MagicMock, clear_state: AsyncMock
    ) -> None:
        mux = await self._close_bound_topic(
            router,
            kill_on_topic_close=True,
            listing=[WindowRef(window_id="@0", window_name="p", cwd="/p")],
        )

        mux.kill_window.assert_awaited_once_with("@0")
        assert clear_state.call_args.kwargs["window_dead"] is True
        router.unbind_thread.assert_called_once_with(
            1, 42, retirement_reason="remote_closed"
        )

    @pytest.mark.parametrize(
        "listing",
        [[], None],
        ids=["window_absent", "presence_unknown"],
    )
    async def test_flag_on_kills_nothing_without_a_confirmed_window(
        self, router: MagicMock, clear_state: AsyncMock, listing: list | None
    ) -> None:
        mux = await self._close_bound_topic(
            router, kill_on_topic_close=True, listing=listing
        )

        mux.kill_window.assert_not_called()
        assert clear_state.call_args.kwargs["window_dead"] is False
        router.unbind_thread.assert_called_once()

    async def test_failed_kill_still_unbinds(
        self, router: MagicMock, clear_state: AsyncMock
    ) -> None:
        """A refused kill must not leave the topic bound to it."""
        mux = await self._close_bound_topic(
            router,
            kill_on_topic_close=True,
            listing=[WindowRef(window_id="@0", window_name="p", cwd="/p")],
            kill_ok=False,
        )

        mux.kill_window.assert_awaited_once_with("@0")
        assert clear_state.call_args.kwargs["window_dead"] is False
        router.unbind_thread.assert_called_once_with(
            1, 42, retirement_reason="remote_closed"
        )


class TestTopicClosedAccessControl:
    async def test_skips_disallowed_user(
        self, router: MagicMock, clear_state: AsyncMock
    ) -> None:
        with patch("ccgram.config.Config.is_user_allowed", return_value=False):
            await topic_closed_handler(_make_update(), MagicMock())

        router.get_window_for_thread.assert_not_called()
        clear_state.assert_not_called()
