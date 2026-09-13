import asyncio
import contextlib
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.telegram_origin import (
    _PENDING_INJECTION_TTL_S,
    agent_origin_returned_to_shell,
    clear_pending_telegram_injections,
    consume_telegram_injection,
    forget_telegram_injection,
    remember_telegram_injection,
    send_telegram_followup_to_window,
    send_telegram_to_window,
)


def setup_function() -> None:
    clear_pending_telegram_injections()


def teardown_function() -> None:
    clear_pending_telegram_injections()


def test_matching_injections_are_consumed_once_in_fifo_order() -> None:
    with patch("ccgram.handlers.telegram_origin.time.monotonic", return_value=100.0):
        remember_telegram_injection(1, "@1", 42, "same")
        remember_telegram_injection(1, "@1", 42, "same")

    with patch("ccgram.handlers.telegram_origin.time.monotonic", return_value=101.0):
        assert consume_telegram_injection(1, "@1", 42, "same") is True
        assert consume_telegram_injection(1, "@1", 42, "same") is True
        assert consume_telegram_injection(1, "@1", 42, "same") is False


def test_matching_skips_unrelated_pending_entries() -> None:
    remember_telegram_injection(1, "@1", 42, "/local-command")
    remember_telegram_injection(1, "@1", 42, "second")

    assert consume_telegram_injection(1, "@1", 42, "second") is True
    assert consume_telegram_injection(1, "@1", 42, "/local-command") is True


def test_failed_send_forgets_exact_reservation() -> None:
    first = remember_telegram_injection(1, "@1", 42, "same")
    second = remember_telegram_injection(1, "@1", 42, "same")

    forget_telegram_injection(1, "@1", 42, second)

    assert consume_telegram_injection(1, "@1", 42, "same") is True
    assert consume_telegram_injection(1, "@1", 42, "same") is False
    forget_telegram_injection(1, "@1", 42, first)


def test_expired_injection_does_not_suppress_terminal_input() -> None:
    with patch("ccgram.handlers.telegram_origin.time.monotonic", return_value=100.0):
        remember_telegram_injection(1, "@1", 42, "hello")

    with patch(
        "ccgram.handlers.telegram_origin.time.monotonic",
        return_value=100.0 + _PENDING_INJECTION_TTL_S,
    ):
        assert consume_telegram_injection(1, "@1", 42, "hello") is False


def test_injection_is_scoped_to_its_bound_topic() -> None:
    remember_telegram_injection(1, "@1", 42, "hello", -100)

    assert consume_telegram_injection(1, "@1", 42, "hello", -200) is False
    assert consume_telegram_injection(1, "@1", 42, "hello", -100) is True


def test_injection_is_scoped_to_chat_when_thread_ids_collide() -> None:
    remember_telegram_injection(1, "@1", 42, "hello", -100)

    assert consume_telegram_injection(1, "@1", 42, "hello", -200) is False
    assert consume_telegram_injection(1, "@1", 42, "hello", -100) is True


def test_matches_provider_trimmed_user_text() -> None:
    remember_telegram_injection(1, "@1", 42, "  hello  \n")

    assert consume_telegram_injection(1, "@1", 42, "hello") is True


@pytest.mark.asyncio
async def test_agent_origin_shell_transition_is_detected() -> None:
    window = MagicMock(pane_current_command="bash")
    with (
        patch("ccgram.window_query.get_window_provider", return_value="claude"),
        patch(
            "ccgram.window_state_ports.identity_state.get_initial_provider_name",
            return_value="claude",
        ),
        patch(
            "ccgram.providers.detect_provider_from_pane",
            new_callable=AsyncMock,
            return_value="shell",
        ) as mock_detect,
    ):
        assert await agent_origin_returned_to_shell("@1", window) is True

    mock_detect.assert_awaited_once_with("bash", window_id="@1")


@pytest.mark.asyncio
async def test_shell_origin_agent_transition_remains_allowed() -> None:
    window = MagicMock(pane_current_command="bash")
    with (
        patch("ccgram.window_query.get_window_provider", return_value="claude"),
        patch(
            "ccgram.window_state_ports.identity_state.get_initial_provider_name",
            return_value="shell",
        ),
        patch(
            "ccgram.providers.detect_provider_from_pane", new_callable=AsyncMock
        ) as mock_detect,
    ):
        assert await agent_origin_returned_to_shell("@1", window) is False

    mock_detect.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_shell_without_origin_fails_closed() -> None:
    with (
        patch("ccgram.window_query.get_window_provider", return_value="shell"),
        patch(
            "ccgram.window_state_ports.identity_state.get_initial_provider_name",
            return_value="",
        ),
    ):
        assert await agent_origin_returned_to_shell("@1") is True


@pytest.mark.asyncio
async def test_origin_aware_send_blocks_agent_exit() -> None:
    with (
        patch(
            "ccgram.handlers.telegram_origin.agent_origin_returned_to_shell",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "ccgram.handlers.telegram_origin.send_to_window", new_callable=AsyncMock
        ) as mock_send,
    ):
        result = await send_telegram_to_window(1, "@1", 42, "hello")

    assert result == (
        False,
        "Agent exited or shell access is not confirmed; recover the session or run "
        "/agent shell before sending input.",
    )
    mock_send.assert_not_awaited()
    assert consume_telegram_injection(1, "@1", 42, "hello") is False


@pytest.mark.asyncio
async def test_origin_aware_send_reserves_before_terminal_injection() -> None:
    async def send_after_reservation(
        window_id: str, text: str, *, raw: bool = False
    ) -> tuple[bool, str]:
        assert consume_telegram_injection(1, window_id, 42, text) is True
        return True, "ok"

    with patch(
        "ccgram.handlers.telegram_origin.send_to_window",
        AsyncMock(side_effect=send_after_reservation),
    ):
        assert await send_telegram_to_window(1, "@1", 42, "hello") == (True, "ok")


@pytest.mark.asyncio
async def test_origin_aware_send_rolls_back_on_false_result() -> None:
    with patch(
        "ccgram.handlers.telegram_origin.send_to_window",
        AsyncMock(return_value=(False, "window gone")),
    ):
        assert await send_telegram_to_window(1, "@1", 42, "hello") == (
            False,
            "window gone",
        )

    assert consume_telegram_injection(1, "@1", 42, "hello") is False


@pytest.mark.asyncio
async def test_origin_aware_send_rolls_back_on_cancellation() -> None:
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await send_telegram_to_window(1, "@1", 42, "hello")

    assert consume_telegram_injection(1, "@1", 42, "hello") is False


@pytest.mark.asyncio
async def test_origin_aware_send_rolls_back_on_exception() -> None:
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(side_effect=RuntimeError("send failed")),
        ),
        pytest.raises(RuntimeError, match="send failed"),
    ):
        await send_telegram_to_window(1, "@1", 42, "hello")

    assert consume_telegram_injection(1, "@1", 42, "hello") is False


# ── desktop notification on a delivered injection ──────────────────────


def _fake_mux(*, supports: bool = True, notify: AsyncMock | None = None) -> MagicMock:
    mux = MagicMock()
    mux.capabilities.supports_notifications = supports
    mux.notify = notify or AsyncMock(return_value=True)
    return mux


@contextlib.contextmanager
def _notify_env(mux: MagicMock, *, enabled: bool = True) -> Iterator[None]:
    """Wire the flag, the backend and the window label the notifier reads."""
    with (
        patch("ccgram.config.config.herdr_notify_on_inject", enabled),
        patch("ccgram.multiplexer.multiplexer", mux),
        patch(
            "ccgram.window_query.view_window",
            lambda window_id: MagicMock(window_name=f"label-{window_id}"),
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_delivered_injection_notifies_the_desktop() -> None:
    mux = _fake_mux()
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(return_value=(True, "ok")),
        ),
        _notify_env(mux),
    ):
        assert await send_telegram_to_window(1, "@1", 42, "hello\nthere") == (
            True,
            "ok",
        )

    mux.notify.assert_awaited_once_with("Telegram \u2192 label-@1", "hello there")


@pytest.mark.asyncio
async def test_delivered_followup_notifies_the_desktop() -> None:
    mux = _fake_mux()
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_followup_to_window",
            AsyncMock(return_value=(True, "ok")),
        ),
        _notify_env(mux),
    ):
        assert await send_telegram_followup_to_window(1, "@1", 42, "hello") == (
            True,
            "ok",
        )

    mux.notify.assert_awaited_once_with("Telegram \u2192 label-@1", "hello")


@pytest.mark.asyncio
async def test_notification_body_is_capped() -> None:
    mux = _fake_mux()
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(return_value=(True, "ok")),
        ),
        _notify_env(mux),
    ):
        await send_telegram_to_window(1, "@1", 42, "x" * 250)

    body = mux.notify.await_args.args[1]
    assert body == "x" * 200 + "\u2026"


@pytest.mark.asyncio
async def test_notification_falls_back_to_the_window_id() -> None:
    """A window with no state yet is still nameable in the toast."""
    mux = _fake_mux()
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(return_value=(True, "ok")),
        ),
        _notify_env(mux),
        patch("ccgram.window_query.view_window", lambda window_id: None),
    ):
        await send_telegram_to_window(1, "@1", 42, "hello")

    mux.notify.assert_awaited_once_with("Telegram \u2192 @1", "hello")


@pytest.mark.asyncio
async def test_notification_is_off_by_default() -> None:
    mux = _fake_mux()
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(return_value=(True, "ok")),
        ),
        _notify_env(mux, enabled=False),
    ):
        await send_telegram_to_window(1, "@1", 42, "hello")

    mux.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_backend_without_notifications_is_never_called() -> None:
    mux = _fake_mux(supports=False)
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(return_value=(True, "ok")),
        ),
        _notify_env(mux),
    ):
        await send_telegram_to_window(1, "@1", 42, "hello")

    mux.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_send_does_not_notify() -> None:
    mux = _fake_mux()
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(return_value=(False, "window gone")),
        ),
        _notify_env(mux),
    ):
        assert await send_telegram_to_window(1, "@1", 42, "hello") == (
            False,
            "window gone",
        )

    mux.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_notification_never_fails_the_send() -> None:
    """The message already landed — a broken toast must not undo that."""
    mux = _fake_mux(notify=AsyncMock(side_effect=RuntimeError("no ui")))
    with (
        patch(
            "ccgram.handlers.telegram_origin.send_to_window",
            AsyncMock(return_value=(True, "ok")),
        ),
        _notify_env(mux),
    ):
        assert await send_telegram_to_window(1, "@1", 42, "hello") == (True, "ok")

    assert consume_telegram_injection(1, "@1", 42, "hello") is True


def test_pending_correlations_are_not_evicted_before_ttl() -> None:
    for index in range(300):
        remember_telegram_injection(index, f"@{index}", 42, f"message-{index}")
    for index in range(20):
        remember_telegram_injection(999, "@burst", 42, f"burst-{index}")

    assert consume_telegram_injection(0, "@0", 42, "message-0") is True
    assert consume_telegram_injection(999, "@burst", 42, "burst-0") is True
