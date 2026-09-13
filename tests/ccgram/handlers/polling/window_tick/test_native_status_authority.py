"""Unit tests for ``CCGRAM_HERDR_NATIVE_STATUS_AUTHORITY`` in ``observe``.

Flag off (the default) keeps the upstream order: scrape the terminal first
and ask a ``native_agent_status`` backend only to gap-fill what the scrapers
could not read.

Flag on, on a backend that reports native status: the backend's own agent
state decides, and the scrapers only supply the label — or the interactive
prompt — for the state it reports. That is what stops a finished-turn line
left on screen from pinning an idle session to "working" forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.polling.polling_runtime import PollingRuntime
from ccgram.handlers.polling.window_tick import observe
from ccgram.handlers.polling.window_tick.observe import _resolve_status
from ccgram.multiplexer import agent_status_cache
from ccgram.multiplexer.base import AgentStatus
from ccgram.providers.base import StatusUpdate
from ccgram.providers.claude import ClaudeProvider

_SEPARATOR = "─" * 90
_COMPLETED_TURN = "✻ Sautéed for 2m 26s · done 11:06 AM · 1 shell still running"


@pytest.fixture(autouse=True)
def _clear_status_cache():
    # The push-status cache is a process-global; isolate every test so the
    # subprocess-fallback path sees a cold cache regardless of run order.
    agent_status_cache.reset()
    yield
    agent_status_cache.reset()


@pytest.fixture
def authority(monkeypatch: pytest.MonkeyPatch):
    """Set the flag for one test (the config singleton is process-global)."""

    def _set(enabled: bool) -> None:
        monkeypatch.setattr(
            observe.config, "herdr_native_status_authority", enabled, raising=True
        )

    return _set


def _fake_mux(*, native: bool = True, status: AgentStatus | None = None) -> MagicMock:
    mux = MagicMock()
    mux.capabilities = SimpleNamespace(native_agent_status=native)
    mux.agent_status = AsyncMock(return_value=status)
    return mux


def _window(window_id: str = "w2:t1") -> MagicMock:
    w = MagicMock()
    w.window_id = window_id
    w.pane_width = 120
    w.pane_height = 40
    return w


async def _resolve(
    mux: MagicMock, scraped: StatusUpdate | None
) -> tuple[StatusUpdate | None, AsyncMock]:
    """Run ``_resolve_status`` with the terminal-scraping pass stubbed out."""
    scrape = AsyncMock(return_value=scraped)
    with (
        patch.object(observe, "tmux_manager", mux),
        patch.object(observe, "_scrape_status", scrape),
    ):
        status = await _resolve_status("w2:t1", "pane text", _window())
    return status, scrape


def _busy(label: str = "Sautéed… (esc to interrupt)") -> StatusUpdate:
    return StatusUpdate(raw_text=label, display_label=label)


def _interactive() -> StatusUpdate:
    return StatusUpdate(
        raw_text="  ☐ Option A\n  Enter to select",
        display_label="AskUserQuestion",
        is_interactive=True,
        ui_type="AskUserQuestion",
    )


# ── Flag off: upstream order ────────────────────────────────────────────


async def test_flag_off_keeps_scraper_first(authority) -> None:
    authority(False)
    mux = _fake_mux(status=AgentStatus(state="idle", agent="claude"))
    status, scrape = await _resolve(mux, _busy())
    assert status is not None
    assert status.raw_text == "Sautéed… (esc to interrupt)"
    scrape.assert_awaited_once()
    mux.agent_status.assert_not_awaited()  # never consulted; scrapers answered


async def test_flag_off_still_gap_fills_from_native(authority) -> None:
    authority(False)
    mux = _fake_mux(status=AgentStatus(state="working", custom_status="indexing"))
    status, _ = await _resolve(mux, None)
    assert status is not None
    assert status.raw_text == "indexing"


# ── Flag on: idle / done ────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["idle", "done"])
async def test_idle_and_done_drop_a_spinner_looking_status(
    authority, state: str
) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state=state, agent="claude"))
    status, scrape = await _resolve(mux, _busy())
    assert status is None  # no typing, no bubble, no "active" emoji
    # The scrapers still run: the screen buffer they feed drives remote-control
    # detection, vim-insert detection and the passive shell relay.
    scrape.assert_awaited_once()


async def test_idle_keeps_interactive_content(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="idle", agent="claude"))
    status, _ = await _resolve(mux, _interactive())
    assert status is not None
    assert status.is_interactive is True
    assert status.ui_type == "AskUserQuestion"


# ── Flag on: blocked ────────────────────────────────────────────────────


async def test_blocked_surfaces_the_interactive_prompt(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="blocked", agent="claude"))
    status, _ = await _resolve(mux, _interactive())
    assert status is not None
    assert status.is_interactive is True


async def test_blocked_without_a_scraped_prompt_waits_for_input(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="blocked", agent="claude"))
    status, _ = await _resolve(mux, None)
    assert status is not None
    assert status.raw_text == "waiting for input"
    assert status.display_label == "waiting"


# ── Flag on: working ────────────────────────────────────────────────────


async def test_working_prefers_the_scraped_label(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="working", custom_status="indexing"))
    status, _ = await _resolve(mux, _busy("Perusing… (3m 35s)"))
    assert status is not None
    assert status.raw_text == "Perusing… (3m 35s)"


async def test_working_falls_back_to_the_native_label(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="working", custom_status="indexing"))
    status, _ = await _resolve(mux, None)
    assert status is not None
    assert status.raw_text == "indexing"


async def test_working_without_custom_status_uses_default_label(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="working", agent="codex"))
    status, _ = await _resolve(mux, None)
    assert status is not None
    assert status.raw_text == "working"


# ── Flag on: no usable native answer ────────────────────────────────────


async def test_unknown_falls_back_to_the_scraper_order(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="unknown", agent="claude"))
    status, scrape = await _resolve(mux, _busy())
    assert status is not None
    assert status.raw_text == "Sautéed… (esc to interrupt)"
    scrape.assert_awaited_once()
    mux.agent_status.assert_awaited_once()  # read once per tick, not twice


async def test_unknown_with_nothing_scraped_yields_none(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="unknown", agent="claude"))
    status, _ = await _resolve(mux, None)
    assert status is None
    mux.agent_status.assert_awaited_once()


async def test_no_native_answer_falls_back_to_the_scrapers(authority) -> None:
    authority(True)
    mux = _fake_mux(status=None)
    status, scrape = await _resolve(mux, _busy())
    assert status is not None
    assert status.raw_text == "Sautéed… (esc to interrupt)"
    scrape.assert_awaited_once()


async def test_backend_without_native_status_is_unaffected(authority) -> None:
    authority(True)
    mux = _fake_mux(native=False, status=AgentStatus(state="idle"))
    status, scrape = await _resolve(mux, _busy())
    assert status is not None  # tmux: the flag changes nothing
    assert status.raw_text == "Sautéed… (esc to interrupt)"
    scrape.assert_awaited_once()
    mux.agent_status.assert_not_awaited()  # gated on capabilities


async def test_push_cache_is_read_before_the_subprocess(authority) -> None:
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="working", custom_status="stale"))
    agent_status_cache.set_status("w2:t1", AgentStatus(state="idle", agent="claude"))
    status, _ = await _resolve(mux, _busy())
    assert status is None  # the pushed "idle" won, not the subprocess
    mux.agent_status.assert_not_awaited()


# ── End to end: real scrapers over a real finished-turn screen ───────────


def _finished_turn_pane() -> str:
    return (
        "  Aside: the watcher for the first event is still hanging.\n"
        "\n"
        f"{_COMPLETED_TURN}\n"
        "\n"
        f"{_SEPARATOR}\n"
        "❯ ok, waiting on the report\n"
        f"{_SEPARATOR}\n"
        "  ▌ dca-services › main › Opus 5 › high\n"
    )


async def test_finished_turn_screen_resolves_to_no_status(authority) -> None:
    # Both halves of the fix together, with the real pyte + terminal_parser
    # path: the parser no longer reads the finished-turn line as a spinner,
    # and herdr's "idle" has the final say either way.
    authority(True)
    mux = _fake_mux(status=AgentStatus(state="idle", agent="claude"))
    runtime = PollingRuntime.create()
    with (
        patch.object(observe, "tmux_manager", mux),
        patch.object(observe, "_get_provider", lambda _wid: ClaudeProvider()),
    ):
        status = await _resolve_status(
            "w2:t1", _finished_turn_pane(), _window(), runtime=runtime
        )
    assert status is None


async def test_finished_turn_screen_is_not_a_status_without_the_flag(
    authority,
) -> None:
    # The parser fix alone is enough — no flag, no backend involved.
    authority(False)
    mux = _fake_mux(native=False, status=None)
    runtime = PollingRuntime.create()
    with (
        patch.object(observe, "tmux_manager", mux),
        patch.object(observe, "_get_provider", lambda _wid: ClaudeProvider()),
    ):
        status = await _resolve_status(
            "w2:t1", _finished_turn_pane(), _window(), runtime=runtime
        )
    assert status is None
