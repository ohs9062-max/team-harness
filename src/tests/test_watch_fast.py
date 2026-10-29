"""Finished-task fast path and registry retirement."""

import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts/th-watch-lib.sh"


def run_bash(code: str, *args: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "$1"; shift; {code}',
            "bash",
            str(LIB),
            *map(str, args),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def settled(task: Path) -> None:
    task.mkdir()
    for name in (
        "DONE",
        ".th_daemon.done",
        ".th_digest.done",
        "VERIFIED",
        ".th_daemon.auto_continue",
    ):
        (task / name).touch()


def test_finished_task_is_skipped(tmp_path: Path) -> None:
    task = tmp_path / "task"
    settled(task)
    assert run_bash('watch_task_settled "$1"', task).returncode == 0


def test_active_or_unnotified_task_is_not_skipped(tmp_path: Path) -> None:
    task = tmp_path / "task"
    settled(task)
    (task / "worker.pid").write_text(str(os.getpid()))
    assert run_bash('watch_task_settled "$1"', task).returncode != 0
    (task / "worker.pid").unlink()
    (task / ".th_daemon.done").unlink()
    assert run_bash('watch_task_settled "$1"', task).returncode != 0


def test_pending_or_changed_task_is_not_skipped(tmp_path: Path) -> None:
    task = tmp_path / "task"
    settled(task)
    (task / ".th_daemon.handoff_wait.1").touch()
    assert run_bash('watch_task_settled "$1"', task).returncode != 0
    (task / ".th_daemon.handoff_wait.1").unlink()
    (task / "FAILED.extra").touch()
    assert run_bash('watch_task_settled "$1"', task).returncode != 0


def test_retirement_keeps_recent_and_pending_tasks(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    old, recent, pending = (tmp_path / n for n in ("old", "recent", "pending"))
    for task in (old, recent, pending):
        settled(task)
    (pending / ".th_daemon.auto_continue").unlink()
    epoch = time.time() - 90000
    for path in old.iterdir():
        os.utime(path, (epoch, epoch))
    (watch / "tasks.txt").write_text(f"{old}\n{recent}\n{pending}\n")
    result = run_bash('watch_retire_settled "$1"', watch)
    assert result.returncode == 0, result.stderr
    assert (watch / "tasks.txt").read_text().splitlines() == [str(recent), str(pending)]
    assert (watch / "tasks_done.txt").read_text().splitlines() == [str(old)]
