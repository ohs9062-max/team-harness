from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from team_harness import exec_pipeline
from team_harness import launch_shim


def test_registered_stage_runs_in_shim_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "real" / "codex"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("TH_SHIM_DEPTH", "0")
    monkeypatch.setenv("PATH", str(fake.parent))
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setattr(
        sys, "argv", [str(tmp_path / "shim" / "codex"), "exec", "hello"]
    )
    monkeypatch.setattr(
        "team_harness.proc_observer.has_ancestor_command", lambda _: False
    )
    monkeypatch.setattr(os, "execv", lambda *args: None)
    monkeypatch.setattr(exec_pipeline, "_pre", [])
    exec_pipeline.register_pre(
        "fake", lambda event: exec_pipeline.StageResult("fake", 0, "ran")
    )
    launch_shim.main("codex")
    records = [
        json.loads(line)
        for line in (tmp_path / "watch" / "exec_pipeline.jsonl")
        .read_text()
        .splitlines()
    ]
    assert records[-1]["stage"]["name"] == "fake"
    assert records[-1]["stage"]["message"] == "[redacted]"


def test_stage_exception_is_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path))
    monkeypatch.setattr(exec_pipeline, "_pre", [])

    def broken(event: exec_pipeline.ExecEvent) -> exec_pipeline.StageResult:
        raise ValueError("bad stage")

    exec_pipeline.register_pre("broken", broken)
    exec_pipeline.register_pre(
        "next", lambda event: exec_pipeline.StageResult("next", 0)
    )
    results = exec_pipeline.run_pre(exec_pipeline.ExecEvent())
    assert [(result.code, result.block) for result in results] == [
        (1, False),
        (0, False),
    ]


@pytest.mark.parametrize(
    "tool, args", [("codex", ["exec", "hello"]), ("claude", ["-p", "hello"])]
)
def test_real_path_shim_observed_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, args: list[str]
) -> None:
    import subprocess

    from team_harness import state_index
    from team_harness.agents.process_identity import capture_starttime

    root = Path(__file__).resolve().parents[2]
    task = tmp_path / "task"
    task.mkdir()
    (task / ".git").mkdir()
    (task / ".claude").mkdir()
    (task / "task.md").write_text("# Task\n## 검수\n```bash\ntrue\n```\n")
    real = tmp_path / "bin"
    real.mkdir()
    executable = real / tool
    executable.write_text("#!/bin/sh\nprintf 'ready\\n'\nread line\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("TH_NO_MAINTENANCE", "1")
    monkeypatch.setenv("TH_OBSERVE", "1")
    monkeypatch.setenv("TH_OBSERVED_VERIFY", "0")
    monkeypatch.setenv("TH_SKIP_COORD_LOG", "1")
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        PATH=f"{root / 'shim'}:{real}:{os.environ['PATH']}",
        TH_SHIM_TIMEOUT="2",
    )
    for key in ("TH_ROLE", "TH_WORKER_OUTPUT_DIR", "TH_JOB_TASK", "TH_SHIM_DEPTH"):
        env.pop(key, None)
    process = subprocess.Popen(
        [tool, *args],
        cwd=task,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None and process.stdin is not None
    assert process.stdout.readline().strip() == "ready"
    start = capture_starttime(process.pid)
    assert start
    row = {
        "pid": process.pid,
        "start": start,
        "task": str(task),
        "session_id": None,
        "claude_pid": None,
        "workspace": str(tmp_path),
        "kind": "job",
        "origin": "user",
        "worker_task": None,
        "cmd": tool,
        "route_key": "",
    }
    state_index.update([row], maintenance=True)
    process.stdin.write("done\n")
    process.stdin.flush()
    assert process.wait(timeout=5) == 0
    state_index.update([], maintenance=True)
    records = [
        json.loads(line)
        for line in (tmp_path / "watch" / "exec_pipeline.jsonl")
        .read_text()
        .splitlines()
    ]
    pre = [item for item in records if item["phase"] == "pre"]
    post = [item for item in records if item["phase"] == "post"]
    assert [x["stage"]["name"] for x in pre] == [
        "execution_policy",
        "quota_lock",
        "brief_check",
        "repeat_warning",
    ]
    assert all(x["stage"]["code"] == 0 for x in pre)
    assert len(post) == 1 and post[0]["stage"]["name"] == "observed_verify"
    assert pre[0]["event"]["event_id"] == post[0]["event"]["event_id"]
    assert post[0]["event"]["exit_code"] == 0
    assert not (task / ".claude" / "inbox.md").exists()
    process.stdin.close()
    process.stdout.close()
    assert process.stderr is not None
    process.stderr.close()


def test_claude_hook_post_does_not_write_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = tmp_path / "owner"
    (owner / ".git").mkdir(parents=True)
    (owner / ".claude").mkdir()
    task = tmp_path / "watch" / "claude-jobs" / "job"
    task.mkdir(parents=True)
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    event = exec_pipeline.ExecEvent(
        task=str(task), owner=str(owner), route="claude_hook", message="response"
    )
    assert exec_pipeline.run_post(event)[0].code == 0
    assert not (owner / ".claude" / "inbox.md").exists()


def test_register_pre_then_exec_real_binary(tmp_path: Path) -> None:
    import subprocess

    root = Path(__file__).resolve().parents[2]
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "codex"
    binary.write_text("#!/bin/sh\nprintf 'real binary\\n'\n")
    binary.chmod(0o755)
    env = dict(
        os.environ,
        PYTHONPATH=str(root / "src"),
        HOME=str(tmp_path),
        TH_WATCH_DIR=str(tmp_path / "watch"),
        TH_NO_MAINTENANCE="1",
        PATH=f"{binary_dir}:{os.environ['PATH']}",
    )
    code = """import sys
from team_harness.exec_pipeline import register_pre, StageResult
from team_harness.launch_shim import main
register_pre('fake', lambda event: StageResult('fake', 0, 'called'))
sys.argv = [sys.argv[1], 'exec', 'hello']
main('codex')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root / "shim" / "codex")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and result.stdout == "real binary\n"
    records = [
        json.loads(line)
        for line in (tmp_path / "watch" / "exec_pipeline.jsonl")
        .read_text()
        .splitlines()
    ]
    assert any(
        x["stage"]["name"] == "fake" and x["stage"]["message"] == "[redacted]"
        for x in records
    )


def test_ledger_does_not_store_command_arguments_or_echoed_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path))
    monkeypatch.setattr(exec_pipeline, "_pre", [])
    monkeypatch.setattr(exec_pipeline, "_post", [])
    secret = "private prompt sentinel 3917"
    exec_pipeline.register_pre(
        "echo", lambda event: exec_pipeline.StageResult("echo", 0, event.argv[-1])
    )
    exec_pipeline.register_post(
        "done", lambda event: exec_pipeline.StageResult("done", 0)
    )
    event = exec_pipeline.ExecEvent(
        tool="codex",
        argv=["exec", secret],
        message=secret,
        warnings=[secret],
        context={"private": secret},
        pid=123,
        process_start="42",
    )
    exec_pipeline.run_pre(event)
    restored = exec_pipeline.find_pre_event(123, "42")
    assert restored is not None and restored.event_id == event.event_id
    restored.message = secret
    exec_pipeline.run_post(restored)
    assert secret not in (tmp_path / "exec_pipeline.jsonl").read_text()


def test_nested_path_shim_runs_real_cli_once(tmp_path: Path) -> None:
    import subprocess

    root = Path(__file__).resolve().parents[2]
    real = tmp_path / "bin"
    real.mkdir()
    codex = real / "codex"
    codex.write_text("#!/bin/sh\nagy -p inner\n")
    codex.chmod(0o755)
    agy = real / "agy"
    agy.write_text("#!/bin/sh\nprintf 'inner=%s\\n' \"$TH_JOB_TASK\"\nexit 23\n")
    agy.chmod(0o755)
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TH_WATCH_DIR=str(tmp_path / "watch"),
        TH_NO_MAINTENANCE="1",
        TH_SHIM_QUIET="1",
        PATH=f"{root / 'shim'}:{real}:{os.environ['PATH']}",
    )
    for key in ("TH_ROLE", "TH_WORKER_OUTPUT_DIR", "TH_JOB_TASK", "TH_SHIM_DEPTH"):
        env.pop(key, None)
    result = subprocess.run(
        ["codex", "exec", "outer"], env=env, capture_output=True, text=True, check=False
    )
    tasks = list((tmp_path / "watch" / "shim-jobs").iterdir())
    assert len(tasks) == 1
    assert result.stdout.strip() == f"inner={tasks[0]}"
    assert result.returncode == 23
