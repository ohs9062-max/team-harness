"""Codex guard subprocess behavior and persistent state."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

GUARD = Path(__file__).resolve().parents[2] / "scripts/hooks/th-codex-guard.py"


def run(
    tmp_path: Path,
    command: str = "true",
    event: str = "PreToolUse",
    output: str = "",
    env: dict[str, str] | None = None,
    raw: str | None = None,
) -> bool:
    environ = {**os.environ, "TH_WATCH_DIR": str(tmp_path)}
    for key in ("TH_ROLE", "TH_JOB_TASK", "TH_CODEX_GUARD", "ORCA_PANE_KEY"):
        environ.pop(key, None)
    environ.update(env or {})
    payload = {
        "hook_event_name": event,
        "session_id": "test",
        "tool_input": {"cmd": command},
        "tool_response": output,
    }
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=raw if raw is not None else json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=environ,
    )
    return bool(result.stdout)


def test_sleep_limits(tmp_path: Path) -> None:
    assert not run(tmp_path, "sleep 9")
    assert run(tmp_path, "sleep 10")
    assert not run(tmp_path, "sleep 59", env={"TH_ROLE": "worker"})
    assert run(tmp_path, "sleep 60", env={"TH_ROLE": "worker"})
    assert run(tmp_path, "while true; do sleep 10; done")
    assert run(tmp_path, "timeout 10 command")
    assert run(tmp_path, "watch -n 10 command")
    assert not run(tmp_path, "sleep 59", env={"TH_JOB_TASK": "job"})


def test_polling_window(tmp_path: Path) -> None:
    assert not run(tmp_path, "cat /tmp/a/STATUS")
    assert not run(tmp_path, "cat /tmp/a/STATUS")
    assert run(tmp_path, "cat /tmp/a/STATUS")
    assert not run(tmp_path, "cat /tmp/b/STATUS")
    state_path = next((tmp_path / "codex_guard").glob("*.json"))
    state = json.loads(state_path.read_text())
    for entry in state["checks"]:
        entry[0] = time.time() - 601
    state_path.write_text(json.dumps(state))
    assert not run(tmp_path, "cat /tmp/a/STATUS")
    assert not run(tmp_path, "ths", env={"TH_ROLE": "worker"})


def test_denial_and_fail_open(tmp_path: Path) -> None:
    assert not run(tmp_path, "cat /tmp/a", "PostToolUse", "permission denied")
    assert run(tmp_path, "cat /tmp/a")
    assert not run(tmp_path, "cat /tmp/b")
    assert not run(tmp_path, "sleep 100", env={"TH_CODEX_GUARD": "off"})
    assert not run(tmp_path, raw="{")
    assert (
        json.loads((tmp_path / "guard.log").read_text().splitlines()[0])["agent"]
        == "codex"
    )
