"""Tests for the /show command handler and its shared focus helper."""

from unittest.mock import AsyncMock, MagicMock, patch


def _make_update(user_id=1, thread_id=42, chat_id=100):
    user = MagicMock()
    user.id = user_id
    message = MagicMock()
    message.message_thread_id = thread_id
    chat = MagicMock()
    chat.id = chat_id
    update = MagicMock()
    update.effective_user = user
    update.message = message
    update.effective_chat = chat
    return update


async def _run(
    update,
    *,
    window_id: str | None,
    supports_focus: bool = True,
    focused: bool = True,
    thread_id: int | None = 42,
):
    from ccgram.handlers.show_command import show_command

    mock_reply = AsyncMock()
    mock_tr = MagicMock()
    mock_tr.get_window_for_thread.return_value = window_id
    mock_tm = MagicMock()
    mock_tm.capabilities.supports_focus = supports_focus
    mock_tm.focus_window = AsyncMock(return_value=focused)

    with (
        patch("ccgram.config.config") as mock_cfg,
        patch("ccgram.handlers.callback_helpers.get_thread_id", return_value=thread_id),
        patch("ccgram.handlers.show_command.thread_router", mock_tr),
        patch("ccgram.handlers.show_command.tmux_manager", mock_tm),
        patch(
            "ccgram.handlers.messaging_pipeline.message_sender.safe_reply",
            mock_reply,
        ),
    ):
        mock_cfg.is_user_allowed.return_value = True
        await show_command(update, MagicMock())
    return mock_reply, mock_tm


async def test_focuses_the_bound_window() -> None:
    reply, tm = await _run(_make_update(), window_id="@0")
    tm.focus_window.assert_awaited_once_with("@0")
    assert "focused" in reply.call_args[0][1].lower()


async def test_outside_a_topic_sends_error() -> None:
    reply, tm = await _run(_make_update(), window_id="@0", thread_id=None)
    tm.focus_window.assert_not_called()
    assert "inside a topic" in reply.call_args[0][1].lower()


async def test_unbound_topic_sends_error() -> None:
    reply, tm = await _run(_make_update(), window_id=None)
    tm.focus_window.assert_not_called()
    assert "not bound" in reply.call_args[0][1].lower()


async def test_backend_without_focus_says_so_before_dispatch() -> None:
    reply, tm = await _run(_make_update(), window_id="@0", supports_focus=False)
    tm.focus_window.assert_not_called()
    assert "not supported" in reply.call_args[0][1].lower()


async def test_unresolved_session_reports_the_failure() -> None:
    reply, tm = await _run(_make_update(), window_id="@0", focused=False)
    tm.focus_window.assert_awaited_once_with("@0")
    assert "could not focus" in reply.call_args[0][1].lower()


async def test_disallowed_user_is_ignored() -> None:
    from ccgram.handlers.show_command import show_command

    mock_reply = AsyncMock()
    mock_tm = MagicMock()
    mock_tm.focus_window = AsyncMock(return_value=True)
    with (
        patch("ccgram.config.config") as mock_cfg,
        patch("ccgram.handlers.show_command.tmux_manager", mock_tm),
        patch(
            "ccgram.handlers.messaging_pipeline.message_sender.safe_reply",
            mock_reply,
        ),
    ):
        mock_cfg.is_user_allowed.return_value = False
        await show_command(_make_update(), MagicMock())

    mock_reply.assert_not_called()
    mock_tm.focus_window.assert_not_called()


async def test_focus_bound_window_is_the_shared_entry_point() -> None:
    """The toolbar builtin reuses this helper, so it owns both answers."""
    from ccgram.handlers import show_command as module

    mock_tm = MagicMock()
    mock_tm.capabilities.supports_focus = True
    mock_tm.focus_window = AsyncMock(return_value=True)
    with patch.object(module, "tmux_manager", mock_tm):
        assert await module.focus_bound_window("@0") == (True, module.FOCUS_OK)

    mock_tm.focus_window = AsyncMock(return_value=False)
    with patch.object(module, "tmux_manager", mock_tm):
        assert await module.focus_bound_window("@0") == (False, module.FOCUS_FAILED)

    mock_tm.capabilities.supports_focus = False
    with patch.object(module, "tmux_manager", mock_tm):
        assert await module.focus_bound_window("@0") == (
            False,
            module.FOCUS_UNSUPPORTED,
        )
