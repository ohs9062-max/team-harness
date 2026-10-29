"""DONE grace applies to both shell watchers without stopping detached jobs."""

import os
from pathlib import Path
import subprocess
import time

import pytest

LIB = Path(__file__).resolve().parents[2] / "scripts/th-watch-lib.sh"


def inspect(task: Path, prefix: str, distinct: bool = False) -> str:
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
