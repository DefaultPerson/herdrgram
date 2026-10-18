"""Integration tests for hook event offset persistence across monitor instances.

Tests that byte offsets survive monitor restarts and that multiple event types
are dispatched correctly through the full pipeline. Basic read/skip/truncate
behaviors are covered by unit tests in test_session_monitor_events.py.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from ccgram.hook import _current_hook_command
from ccgram.providers.base import HookEvent

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("shadow_source", ["cwd", "PYTHONPATH"])
@pytest.mark.parametrize("legacy_command", [False, True])
def test_hook_ignores_project_packages(
    tmp_path: Path, shadow_source: str, legacy_command: bool
) -> None:
    """A project's telegram package must not replace the bot's dependency."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "telegram.py").write_text(
        "raise RuntimeError('project package loaded')\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    session_id = "550e8400-e29b-41d4-a716-446655440000"
    snapshot = {
        "result": {
            "agents": [
                {
                    "agent": "claude",
                    "agent_session": {
                        "source": "herdr:claude",
                        "agent": "claude",
                        "kind": "id",
                        "value": session_id,
                    },
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "pane_id": "w1:p1",
                    "terminal_id": "term-test",
                    "cwd": str(project),
                }
            ]
        }
    }
    herdr = bin_dir / "herdr"
    herdr.write_text(
        "#!/bin/sh\ncat <<'SNAPSHOT'\n" + json.dumps(snapshot) + "\nSNAPSHOT\n"
    )
    herdr.chmod(0o755)
    state_dir = tmp_path / "state"
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "CCGRAM_DIR": str(state_dir),
        "TELEGRAM_BOT_TOKEN": "test:local",
        "ALLOWED_USERS": "1",
        "HERDR_WORKSPACE_ID": "w1",
        "HERDR_PANE_ID": "w1:p1",
        "PYTHONPATH": str(project) if shadow_source == "PYTHONPATH" else "",
    }
    env.pop("TMUX_PANE", None)
    command = shlex.split(_current_hook_command())
    if legacy_command:
        command.remove("-I")
    result = subprocess.run(
        command,
        input=json.dumps(
            {"hook_event_name": "Stop", "session_id": session_id, "cwd": str(project)}
        ),
        text=True,
        capture_output=True,
        timeout=10,
        env=env,
        cwd=project if shadow_source == "cwd" else tmp_path,
    )
    assert result.returncode == 0, result.stderr
    events = [
        json.loads(line)
        for line in (state_dir / "events.jsonl").read_text().splitlines()
    ]
    assert len(events) == 1
    assert events[0]["event"] == "Stop"
    assert events[0]["session_id"] == session_id
    assert events[0]["window_key"].startswith("herdr:herdr-session-v1-")


async def test_event_offset_persists_across_monitor_instances(
    make_monitor, append_event
) -> None:
    received: list[HookEvent] = []

    async def on_event(event: HookEvent) -> None:
        received.append(event)

    append_event("Stop")

    monitor1 = make_monitor()
    monitor1.set_hook_event_callback(on_event)
    await monitor1._read_hook_events()
    monitor1.state.save()
    assert len(received) == 1

    monitor2 = make_monitor()
    monitor2.set_hook_event_callback(on_event)
    await monitor2._read_hook_events()
    assert len(received) == 1


async def test_multiple_event_types_dispatched(make_monitor, append_event) -> None:
    received: list[HookEvent] = []

    async def on_event(event: HookEvent) -> None:
        received.append(event)

    monitor = make_monitor()
    monitor.set_hook_event_callback(on_event)

    append_event("SubagentStart", data={"subagent_id": "a1", "name": "researcher"})
    append_event("TeammateIdle", data={"teammate_name": "coder"})
    append_event(
        "TaskCompleted",
        data={"task_subject": "Write tests", "teammate_name": "coder"},
    )
    append_event("SubagentStop", data={"subagent_id": "a1"})

    await monitor._read_hook_events()

    assert len(received) == 4
    types = [e.event_type for e in received]
    assert types == ["SubagentStart", "TeammateIdle", "TaskCompleted", "SubagentStop"]
    assert received[0].data["name"] == "researcher"
    assert received[1].data["teammate_name"] == "coder"
    assert received[2].data["task_subject"] == "Write tests"
