"""Detached job startup and read-only process discovery."""

import os
from pathlib import Path
import signal
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def found(task: Path) -> list[int]:
    result = subprocess.check_output(
        [sys.executable, str(SCRIPTS / "th-job-liveness.py"), str(task)], text=True
    )
    return [int(pid) for pid in result.split()]


def test_start_records_exec_pid_and_tag(tmp_path: Path) -> None:
    result = subprocess.check_output(
        [str(SCRIPTS / "th-job-start.sh"), str(tmp_path), "--", "sleep", "30"],
        text=True,
    )
    pid = int(result)
    try:
        assert (tmp_path / "job.pid").read_text() == f"{pid}\n"
        assert (
            Path(f"/proc/{pid}/cmdline")
            .read_bytes()
            .split(b"\0")[0]
            .endswith((b"sleep", b"bash"))
        )
        assert f"TH_JOB_TASK={tmp_path}".encode() in Path(
            f"/proc/{pid}/environ"
        ).read_bytes().split(b"\0")
        assert pid in found(tmp_path)
    finally:
        os.kill(pid, signal.SIGTERM)


def test_tag_and_session_fallback(tmp_path: Path) -> None:
    task = tmp_path / "unique_liveness_job_8193"
    task.mkdir()
    (task / "job.pid").write_text("99999998\n 99999999 \n")
    env = os.environ.copy()
    env["TH_JOB_TASK"] = str(task)
    tagged = subprocess.Popen(["sleep", "30"], env=env)
    try:
        assert tagged.pid in found(task)
        assert (task / "job.pid").read_text() == "99999998\n 99999999 \n"
    finally:
        tagged.terminate()
        tagged.wait()
    env.pop("TH_JOB_TASK")
    leader = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", task.name],
        env=env,
        start_new_session=True,
    )
    try:
        assert leader.pid in found(task)
        watch = tmp_path / "watch"
        watch.mkdir()
        (watch / "tasks.txt").write_text(f"{task}\n")
        (task / "STATUS").write_text("2026-10-27 01:02:03 exact status\n")
        env["TH_WATCH_DIR"] = str(watch)
        result = subprocess.run(
            [str(SCRIPTS / "th-status.sh"), "-a"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "실행(추정)" in result.stdout
        assert "exact status" in result.stdout
        (task / "FAILED").write_text("old failure\n")
        result = subprocess.run(
            [str(SCRIPTS / "th-status.sh")],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "실행(추정)" in result.stdout
    finally:
        leader.terminate()
        leader.wait()
    assert not found(task)


def test_live_attempt_ignores_only_older_markers(tmp_path: Path) -> None:
    task = tmp_path / "attempt"
    task.mkdir()
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "tasks.txt").write_text(f"{task}\n")
    (task / "STATUS").write_text("working\n")
    env = os.environ.copy()
    env["TH_WATCH_DIR"] = str(watch)
    env["TH_JOB_TASK"] = str(task)
    proc = subprocess.Popen(["sleep", "30"], env=env)
    try:
        (task / "job.pid").write_text(f"{proc.pid}\n")
        old = task / "VERIFY_FAILED"
        old.write_text("previous attempt")
        os.utime(old, (1, 1))
        failed = task / "FAILED"
        failed.write_text("previous attempt")
        os.utime(failed, (1, 1))
        result = subprocess.run(
            [str(SCRIPTS / "th-status.sh")],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "실행 1 · 주의 0" in result.stdout
        command = 'source "$1"; cb() { printf "%s\\n" "$2"; }; inspect_task_event "$2" "$3" .th_test 999999 "" cb'
        result = subprocess.run(
            [
                "bash",
                "-c",
                command,
                "bash",
                str(SCRIPTS / "th-watch-lib.sh"),
                str(task),
                str(watch),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "실패" not in result.stdout
        os.utime(failed, None)
        result = subprocess.run(
            [str(SCRIPTS / "th-status.sh")],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "실행 0 · 주의 1" in result.stdout
        result = subprocess.run(
            [
                "bash",
                "-c",
                command,
                "bash",
                str(SCRIPTS / "th-watch-lib.sh"),
                str(task),
                str(watch),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "실패" in result.stdout
    finally:
        proc.terminate()
        proc.wait()
