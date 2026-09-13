"""Seeding the session map from a multiplexer-published native session id.

A Claude session only reaches Telegram once ``session_map.json`` names it, and
only the SessionStart hook writes that entry. A session that predates the hook
install, or whose hook failed, gets a topic and then silence. herdr publishes
the running agent's own session id, so these tests pin the path that turns it
into the entry the hook never wrote.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ccgram.config import config
from ccgram.handlers.recovery import transcript_discovery
from ccgram.handlers.recovery.transcript_discovery import (
    discover_and_register_transcript,
    seed_session_from_native_id,
)
from ccgram.handlers.topics.topic_orchestration import _seed_native_session
from ccgram.multiplexer.base import WindowRef
from ccgram.session import SessionManager
from ccgram.thread_router import thread_router
from ccgram.window_state_ports import identity_state
from ccgram.window_state_store import WindowState, window_store

TARGET = "herdr-session-v1-" + "a" * 64
OTHER_TARGET = "herdr-session-v1-" + "b" * 64
SESSION = "ab8bdbd8-238e-4b50-b0a0-c22fa2adc83c"
OLD_SESSION = "93e6078b-9fd7-4499-8e32-ca252aa7c945"
CWD = "~/projects/dca-services"


@pytest.fixture
def mgr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SessionManager:
    """A wired SessionManager writing its state files under ``tmp_path``."""
    thread_router.reset()
    window_store.window_states.clear()
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    monkeypatch.setattr(config, "multiplexer_name", "herdr")
    monkeypatch.setattr(config, "claude_projects_path", tmp_path / "projects")
    monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
    return SessionManager()


def _transcript(session_id: str = SESSION, cwd: str = CWD) -> Path:
    """Write a transcript where Claude would have put it, and return the path."""
    path = config.claude_projects_path / cwd.replace("/", "-") / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "user"}\n')
    return path


def _herdr_window(
    *,
    window_id: str = TARGET,
    cwd: str = CWD,
    native_session_id: str = SESSION,
    native_agent: str = "claude",
) -> WindowRef:
    return WindowRef(
        window_id=window_id,
        window_name="Claude ▸ dca ▸ t2 ▸ p2",
        cwd=cwd,
        pane_current_command="claude",
        native_session_id=native_session_id,
        native_agent=native_agent,
    )


def _tmux_window() -> WindowRef:
    """What tmux reports: a pane, with nothing said about the agent inside it."""
    return WindowRef(
        window_id="@4", window_name="dca", cwd=CWD, pane_current_command="claude"
    )


def _map_entry(window_id: str = TARGET) -> dict[str, object]:
    return json.loads(config.session_map_file.read_text())[f"herdr:{window_id}"]


# ── seeding ────────────────────────────────────────────────────────────


async def test_seeds_a_window_that_tracks_no_session(mgr: SessionManager) -> None:
    """The whole point: a live session with no map entry gets one."""
    transcript = _transcript()

    assert await seed_session_from_native_id(TARGET, _herdr_window(), None) is True

    state = mgr.window_states[TARGET]
    assert state.session_id == SESSION
    assert state.cwd == CWD
    assert state.transcript_path == str(transcript)
    assert state.provider_name == "claude"
    assert _map_entry() == {
        "schema_version": 1,
        "session_id": SESSION,
        "cwd": CWD,
        "window_name": TARGET,
        "transcript_path": str(transcript),
        "provider_name": "claude",
    }


async def test_seeded_entry_never_asks_for_a_replay(mgr: SessionManager) -> None:
    """Tailing starts at EOF, so a 3 MB transcript is not poured into a topic.

    The monitor decides the offset, and it starts an untracked session at the
    current end of the file; the one flag that overrides that is Pi's
    ``replay_from_start``, which this path must never set.
    """
    _transcript()

    assert await seed_session_from_native_id(TARGET, _herdr_window(), None) is True

    assert "replay_from_start" not in _map_entry()


async def test_re_seeds_when_the_backend_reports_a_different_session(
    mgr: SessionManager,
) -> None:
    """A pane that re-keyed its session must not keep tailing the old file."""
    old_transcript = _transcript(OLD_SESSION)
    new_transcript = _transcript(SESSION)
    mgr.window_states[TARGET] = WindowState(
        session_id=OLD_SESSION,
        cwd=CWD,
        transcript_path=str(old_transcript),
        provider_name="claude",
    )

    assert (
        await seed_session_from_native_id(
            TARGET, _herdr_window(), identity_state.get_identity(TARGET)
        )
        is True
    )

    assert mgr.window_states[TARGET].session_id == SESSION
    assert mgr.window_states[TARGET].transcript_path == str(new_transcript)


async def test_falls_back_to_the_stored_working_directory(
    mgr: SessionManager,
) -> None:
    """The backend's cwd leads, but a stale one must not block the heal."""
    transcript = _transcript(SESSION, CWD)
    mgr.window_states[TARGET] = WindowState(cwd=CWD, provider_name="claude")

    seeded = await seed_session_from_native_id(
        TARGET,
        _herdr_window(cwd="/somewhere/else"),
        identity_state.get_identity(TARGET),
    )

    assert seeded is True
    assert _map_entry()["cwd"] == CWD
    assert _map_entry()["transcript_path"] == str(transcript)


# ── declining to seed ──────────────────────────────────────────────────


async def test_no_op_and_no_disk_access_when_the_session_already_matches(
    mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tick of every bound topic takes this path, so it must be free."""
    transcript = _transcript()
    mgr.window_states[TARGET] = WindowState(
        session_id=SESSION,
        cwd=CWD,
        transcript_path=str(transcript),
        provider_name="claude",
    )

    def _explode(session_id: str, cwd: str) -> Path | None:
        raise AssertionError("derived a transcript path for an unchanged session")

    monkeypatch.setattr(
        transcript_discovery, "_NATIVE_SEED_TRANSCRIPTS", {"claude": _explode}
    )

    seeded = await seed_session_from_native_id(
        TARGET, _herdr_window(), identity_state.get_identity(TARGET)
    )

    assert seeded is False
    assert not config.session_map_file.exists()


async def test_no_op_when_the_derived_transcript_does_not_exist(
    mgr: SessionManager,
) -> None:
    """An id and a directory that never belonged together name nothing."""
    assert await seed_session_from_native_id(TARGET, _herdr_window(), None) is False

    assert TARGET not in mgr.window_states
    assert not config.session_map_file.exists()


async def test_no_op_on_a_backend_that_publishes_no_native_session_id(
    mgr: SessionManager,
) -> None:
    """tmux sees a pane, not a session — the path has to stay inert there."""
    _transcript()

    assert await seed_session_from_native_id("@4", _tmux_window(), None) is False

    assert "@4" not in mgr.window_states
    assert not config.session_map_file.exists()


async def test_no_op_for_an_agent_whose_transcript_is_not_derivable(
    mgr: SessionManager,
) -> None:
    """Codex names its files by its own scheme; the hookless scan owns it."""
    _transcript()

    seeded = await seed_session_from_native_id(
        TARGET, _herdr_window(native_agent="codex"), None
    )

    assert seeded is False
    assert not config.session_map_file.exists()


async def test_no_op_when_another_window_is_already_bound_to_the_session(
    mgr: SessionManager,
) -> None:
    """Two topics tailing one session would double every message it emits."""
    transcript = _transcript()
    mgr.window_states[OTHER_TARGET] = WindowState(
        session_id=SESSION, cwd=CWD, transcript_path=str(transcript)
    )
    thread_router.bind_thread(12345, 7, OTHER_TARGET)

    assert await seed_session_from_native_id(TARGET, _herdr_window(), None) is False

    assert TARGET not in mgr.window_states
    assert not config.session_map_file.exists()


async def test_no_op_without_a_live_window(mgr: SessionManager) -> None:
    assert await seed_session_from_native_id(TARGET, None, None) is False


# ── the per-tick discovery path ────────────────────────────────────────


async def test_discovery_seeds_and_then_skips_the_hookless_scan(
    mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once seeded the window reads as hook-resolved, as it would have anyway."""
    transcript = _transcript()
    mgr.window_states[TARGET] = WindowState(cwd=CWD, provider_name="claude")
    monkeypatch.setattr(
        transcript_discovery,
        "_detect_and_apply_provider",
        AsyncMock(return_value=False),
    )

    with patch(
        "ccgram.handlers.recovery.transcript_discovery._find_and_register_transcript"
    ) as scan:
        agent_exited = await discover_and_register_transcript(
            TARGET, _window=_herdr_window()
        )

    assert agent_exited is False
    scan.assert_not_called()
    assert mgr.window_states[TARGET].session_id == SESSION
    assert mgr.window_states[TARGET].transcript_path == str(transcript)


# ── the adoption path ──────────────────────────────────────────────────


async def test_adoption_seeds_before_the_first_poll_tick(mgr: SessionManager) -> None:
    """A window adopted mid-session must not lose the replies until the tick."""
    transcript = _transcript()

    with patch(
        "ccgram.multiplexer.tmux.tmux_manager.find_window_by_id",
        AsyncMock(return_value=_herdr_window()),
    ):
        await _seed_native_session(TARGET)

    assert mgr.window_states[TARGET].session_id == SESSION
    assert mgr.window_states[TARGET].transcript_path == str(transcript)


async def test_adoption_does_not_query_the_backend_for_a_tracked_window(
    mgr: SessionManager,
) -> None:
    """The guard is what keeps this off the path of every healthy adoption."""
    mgr.window_states[TARGET] = WindowState(
        session_id=SESSION,
        cwd=CWD,
        transcript_path=str(_transcript()),
        provider_name="claude",
    )

    with patch(
        "ccgram.multiplexer.tmux.tmux_manager.find_window_by_id",
        AsyncMock(return_value=_herdr_window()),
    ) as lookup:
        await _seed_native_session(TARGET)

    lookup.assert_not_called()
