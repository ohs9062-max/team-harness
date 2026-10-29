"""Watchd rollover decisions and launch accounting."""

import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from team_harness import watch_inspect
from team_harness.worker_context import ContextMeasurement


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any):
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("---\ntask_key: test\ncontext_budget: 100\n---\n")
    (task / "worker.pid").write_text("12345")
    (task / ".th_context.json").write_text(
        json.dumps(
            {
                "workspace": str(tmp_path),
                "agent_type": "codex",
                "session_id": "s",
                "registry_path": str(tmp_path / "registry.json"),
            }
        )
    )
    (task / "WORKLOG.md").write_text("progress")
    (task / "rollover_state.json").write_text(
        json.dumps({"last_check": 0, "count": 0, "last_hash": None})
    )
    monkeypatch.setenv("TH_WATCH_ROLLOVER", "1")
    monkeypatch.setattr(watch_inspect, "pid_alive", lambda pid: True)
    monkeypatch.setattr(
        watch_inspect,
        "measure_codex_rollout",
        lambda **kw: ContextMeasurement(101, "measured"),
    )
    calls = []
    monkeypatch.setattr(watch_inspect, "launch_arguments", lambda *args: ["launch"])
    monkeypatch.setattr(
        watch_inspect, "terminate_worker_group", lambda pid: calls.append(("stop", pid))
    )
    monkeypatch.setattr(
        watch_inspect,
        "relaunch_worker",
        lambda *args: calls.append(("launch", args[1])),
    )
    return task, calls, capsys


def test_boundary_rollover(scenario: Any):
    task, calls, output = scenario
    watch_inspect.main(task)
    assert calls == [("stop", 12345), ("launch", ["launch"])]
    assert "롤오버(1/3)" in output.readouterr().out


def test_no_boundary_does_nothing(scenario: Any):
    task, calls, _ = scenario
    state = {
        "last_check": (task / "WORKLOG.md").stat().st_mtime,
        "count": 0,
        "last_hash": None,
    }
    (task / "rollover_state.json").write_text(json.dumps(state))
    watch_inspect.main(task)
    assert not calls


def test_split_without_progress_only_notifies(scenario: Any):
    import hashlib

    task, calls, output = scenario
    state = {
        "last_check": 0,
        "count": 1,
        "last_hash": hashlib.sha256(b"progress").hexdigest(),
    }
    (task / "rollover_state.json").write_text(json.dumps(state))
    watch_inspect.main(task)
    assert not calls
    assert "작업 분할 필요" in output.readouterr().out


def test_three_rollover_limit(scenario: Any):
    task, calls, output = scenario
    (task / "rollover_state.json").write_text(
        json.dumps({"last_check": 0, "count": 3, "last_hash": None})
    )
    watch_inspect.main(task)
    assert not calls
    assert "작업 분할 필요" in output.readouterr().out


def test_rollover_is_excluded_from_retry_count(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    watch = tmp_path / "watch"
    watch.mkdir()
    (task / ".th_worktree").write_text(str(tmp_path))
    (task / "STATUS").write_text("2026-01-01 00:00:00 START step=a replay=safe\n")
    (watch / "usage.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "v": 1,
                    "task": str(task),
                    "worker": "codex",
                    "model": "m",
                    "effort": "medium",
                    "rollover": v,
                }
            )
            for v in (False, True, True)
        )
        + "\n"
    )
    root = Path(__file__).resolve().parents[2]
    script = f'''source "{root}/scripts/th-watch-lib.sh"
worker_digest_program() {{ printf '%s' "{root}/src/team_harness/worker_digest.py"; }}
th_python() {{ python "$@"; }}
callback() {{ printf '%s' "$2"; }}
safe_step_auto_retry "$1" "$2" "$1/marker" callback "$1"
'''
    # A failing launcher still proves the retry gate was reached.
    env = dict(os.environ, TH_MAX_RETRIES="1", TH_WATCH_DIR=str(watch))
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(task), str(watch)],
        env=env,
        text=True,
        capture_output=True,
    )
    assert "자동 재시도 실패" in result.stdout
