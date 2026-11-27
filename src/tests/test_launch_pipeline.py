from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def ledger(watch: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (watch / "exec_pipeline.jsonl").read_text().splitlines()
    ]


def test_job_wait_records_one_event_and_exit(tmp_path: Path) -> None:
    task, watch = tmp_path / "task", tmp_path / "watch"
    task.mkdir()
    env = dict(os.environ, TH_WATCH_DIR=str(watch), TH_NO_DAEMON="1")
    for key in ("TH_EXEC_ID", "TH_EXEC_EVENT_ID", "TH_ROLE"):
        env.pop(key, None)
    proc = subprocess.run(
        [
            str(ROOT / "scripts/th-job-start.sh"),
            str(task),
            "--wait",
            "--",
            "sh",
            "-c",
            "exit 7",
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 7
    records = ledger(watch)
    assert {r["event"]["event_id"] for r in records}.__len__() == 1
    assert {r["phase"] for r in records} == {"pre", "post"}
    assert records[-1]["event"]["exit_code"] == 7
    assert records[-1]["event"]["task"] == str(task)


def test_hook_execution_has_one_shared_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    watch = tmp_path / "watch"
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.setenv("TH_NO_DAEMON", "1")
    monkeypatch.setenv("TH_HARNESS_SRC", str(ROOT / "src"))
    monkeypatch.delenv("TH_ROLE", raising=False)
    monkeypatch.delenv("TH_EXEC_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "train.py").write_text("print('ok')\n")
    spec = importlib.util.spec_from_file_location(
        "test_launch_hook", ROOT / "scripts/hooks/th-exec-pipeline.py"
    )
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(hook, "ancestor_is_th", lambda: False)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "python3 train.py"},
        "tool_use_id": "test",
        "session_id": "s",
        "cwd": str(tmp_path),
    }
    hook.main(payload)
    command = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["updatedInput"][
        "command"
    ]
    proc = subprocess.run(
        command, shell=True, env=os.environ.copy(), capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr.decode()
    payload["hook_event_name"] = "PostToolUse"
    hook.main(payload)
    records = ledger(watch)
    assert len({r["event"]["event_id"] for r in records}) == 1
    assert {r["event"]["route"] for r in records} == {"claude_hook"}
    assert {r["phase"] for r in records} == {"pre", "post"}
    print("hook/--execute unique_events=1 phases=pre,post exit=0")


def test_pipeline_failure_is_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from team_harness import launch_pipeline

    def broken(*args: object) -> None:
        raise RuntimeError("pipeline failure")

    monkeypatch.setattr(launch_pipeline, "run_pre", broken)
    monkeypatch.setattr(
        "sys.argv",
        ["launch_pipeline", "pre", str(tmp_path), "th_job_start", "sh", "true"],
    )
    assert launch_pipeline.main() == 0


def test_job_process_group_termination_records_exit(tmp_path: Path) -> None:
    import signal

    task, watch = tmp_path / "task", tmp_path / "watch"
    task.mkdir()
    env = dict(os.environ, TH_WATCH_DIR=str(watch), TH_NO_DAEMON="1")
    for key in ("TH_EXEC_ID", "TH_EXEC_EVENT_ID", "TH_ROLE"):
        env.pop(key, None)
    proc = subprocess.Popen(
        [
            str(ROOT / "scripts/th-job-start.sh"),
            str(task),
            "--wait",
            "--",
            "sh",
            "-c",
            "echo ready; exec sleep 30",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    pid = int((task / "job.pid").read_text())
    os.killpg(pid, signal.SIGTERM)
    assert proc.wait(timeout=10) == 143
    assert ledger(watch)[-1]["event"]["exit_code"] == 143


def test_launch_records_completion(tmp_path: Path) -> None:

    task, watch, work, fake = (
        tmp_path / name for name in ("task", "watch", "work", "bin")
    )
    for path in (task, work, fake):
        path.mkdir()
    (task / "task.md").write_text("---\nkind: code\ntask_key: launch_test\n---\n")
    (tmp_path / "suffix.md").write_text("test")
    executable = fake / "codex"
    executable.write_text(
        '#!/bin/sh\necho done > "$TH_WORKER_OUTPUT_DIR/DONE"\nexit 7\n'
    )
    executable.chmod(0o755)
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TH_WATCH_DIR=str(watch),
        TH_NO_DAEMON="1",
        TH_WORKSPACE=str(work),
        TH_WORKER_SUFFIX=str(tmp_path / "suffix.md"),
        TH_CODEX_5H_BUDGET="1000000",
        TH_CODEX_SESSIONS_DIR=str(tmp_path / "sessions"),
        TH_DIGEST_COMMAND="/bin/true",
        TH_AUTO_RECALL="0",
        TEAM_HARNESS_JEV="0",
        TH_BRIEF_OK="test",
        PATH=f"{fake}:{os.environ['PATH']}",
    )
    for key in (
        "TH_ROLE",
        "TH_EXEC_ID",
        "TH_EXEC_EVENT_ID",
        "TH_WORKER_OUTPUT_DIR",
        "TH_JOB_TASK",
    ):
        env.pop(key, None)
    proc = subprocess.run(
        [str(ROOT / "scripts/th-launch.sh"), str(task), "codex", str(work)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    daemon_env = dict(env, TH_ONCE="1", TH_JEV_WATCH="off")
    for _ in range(2):
        subprocess.run(
            [str(ROOT / "scripts/th-watchd.sh")],
            env=daemon_env,
            check=True,
            capture_output=True,
            timeout=30,
        )
    records = ledger(watch)
    assert {r["phase"] for r in records} == {"pre", "post"}
    assert len({r["event"]["event_id"] for r in records}) == 1
    assert len([r for r in records if r["phase"] == "post"]) == 1
    assert records[-1]["event"]["exit_code"] is None
    assert records[-1]["event"]["result_paths"] == [str(task / "DONE")]


def test_detached_job_has_no_existing_post_site(tmp_path: Path) -> None:
    import time

    task, watch = tmp_path / "task", tmp_path / "watch"
    task.mkdir()
    env = dict(os.environ, TH_WATCH_DIR=str(watch), TH_NO_DAEMON="1")
    for key in ("TH_EXEC_ID", "TH_EXEC_EVENT_ID", "TH_ROLE"):
        env.pop(key, None)
    proc = subprocess.run(
        [
            str(ROOT / "scripts/th-job-start.sh"),
            str(task),
            "--",
            "sh",
            "-c",
            'echo 7 > "$TH_JOB_TASK/exit_code"; exit 7',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    deadline = time.monotonic() + 5
    while not (task / "exit_code").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (task / "exit_code").read_text().strip() == "7"
    rows = ledger(watch)
    assert {row["phase"] for row in rows} == {"pre"}
    assert len({row["event"]["event_id"] for row in rows}) == 1
    print("job/direct unique_events=1 phases=pre; post absent")
