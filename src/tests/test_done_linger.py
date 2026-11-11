"""DONE grace applies to both shell watchers without stopping detached jobs."""

import os
from pathlib import Path
import subprocess
import time

import pytest

LIB = Path(__file__).resolve().parents[2] / "scripts/th-watch-lib.sh"


def inspect(
    task: Path, prefix: str, distinct: bool = False, *, real_python: bool = False
) -> str:
    script = r"""
source "$1"
task=$2; distinct=$4
inspect_context_and_job() { printf '\t%s\t\n' "$distinct"; }
task_live_pids() { cat "$task/worker.pid"; }
th_python() { echo terminate >> "$task/terminated"; }
run_verification() { echo 없음; }
callback() { printf '%s\n' "$2" >> "$task/events"; }
inspect_task_event "$2" "$3" "$5" 0 '' callback
"""
    if real_python:
        script = script.replace(
            'th_python() { echo terminate >> "$task/terminated"; }', ""
        )
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "_",
            str(LIB),
            str(task),
            str(task),
            str(int(distinct)),
            prefix,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stderr


@pytest.mark.parametrize("prefix", [".th_daemon", ".th_notified"])
def test_done_linger_grace_and_termination(tmp_path: Path, prefix: str) -> None:
    (tmp_path / "worker.pid").write_text(str(os.getpid()))
    (tmp_path / "DONE").write_text("done")
    (tmp_path / "codex.log").write_text("final output")
    inspect(tmp_path, prefix)
    assert not (tmp_path / "events").exists()
    assert not (tmp_path / "terminated").exists()
    wait = tmp_path / f"{prefix}.done_wait"
    signature = wait.read_text().rsplit("|", 1)[0]
    wait.write_text(f"{signature}|{int(time.time()) - 61}\n")
    inspect(tmp_path, prefix)
    assert (tmp_path / "events").read_text().strip() == "완료"
    assert (tmp_path / "terminated").read_text().strip() == "terminate"
    assert "DONE 후 잔류 프로세스 종료" in (tmp_path / "events.log").read_text()


@pytest.mark.parametrize("prefix", [".th_daemon", ".th_notified"])
def test_distinct_job_is_not_terminated(tmp_path: Path, prefix: str) -> None:
    (tmp_path / "worker.pid").write_text(str(os.getpid()))
    (tmp_path / "DONE").write_text("done")
    inspect(tmp_path, prefix, distinct=True)
    assert not (tmp_path / "terminated").exists()
    assert not (tmp_path / f"{prefix}.done").exists()


@pytest.mark.parametrize("identity", ["matching", "missing", "mismatched"])
@pytest.mark.parametrize("prefix", [".th_daemon", ".th_notified"])
def test_done_linger_real_python_respects_launch_identity(
    tmp_path: Path, identity: str, prefix: str
) -> None:
    """Exercise the shell -> real termination function boundary on an isolated group."""
    from team_harness.agents.process_identity import capture_starttime

    worker = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        (tmp_path / "worker.pid").write_text(str(worker.pid))
        if identity != "missing":
            import json

            token = capture_starttime(worker.pid)
            assert token is not None
            (tmp_path / "worker.pid.start").write_text(
                json.dumps(
                    {
                        "pid": worker.pid,
                        "start": token
                        if identity == "matching"
                        else "linux:old-boot:0",
                    }
                )
            )
        (tmp_path / "DONE").write_text("done")
        (tmp_path / "codex.log").write_text("final output")
        inspect(tmp_path, prefix, real_python=True)
        wait = tmp_path / f"{prefix}.done_wait"
        signature = wait.read_text().rsplit("|", 1)[0]
        wait.write_text(f"{signature}|{int(time.time()) - 61}\n")
        stderr = inspect(tmp_path, prefix, real_python=True)
        if identity == "matching":
            worker.wait(timeout=5)
            assert worker.returncode is not None and worker.returncode < 0
            assert (tmp_path / ".th_done_terminated").is_file()
        else:
            assert worker.poll() is None
            assert not (tmp_path / ".th_done_terminated").exists()
            assert "termination deferred" in stderr
    finally:
        if worker.poll() is None:
            worker.terminate()
        worker.wait(timeout=5)
