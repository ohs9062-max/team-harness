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
    for name in ("DONE", ".th_daemon.done", ".th_digest.done", "VERIFIED"):
        (task / name).touch()
    result = run_bash('watch_task_signature "$1" > "$1/.th_daemon.settled"', task)
    assert result.returncode == 0


def test_finished_task_is_skipped(tmp_path: Path) -> None:
    task = tmp_path / "task"
    settled(task)
    assert run_bash('watch_task_settled "$1"', task).returncode == 0


def test_legacy_registry_first_and_second_pass_are_fast(tmp_path: Path) -> None:
    registry = tmp_path / "tasks.txt"
    tasks = []
    for index in range(250):
        task = tmp_path / str(index)
        task.mkdir()
        for name in ("DONE", ".th_daemon.done", ".th_digest.done", "VERIFIED"):
            (task / name).touch()
        (task / ".th_daemon.settled").touch()
        tasks.append(task)
    registry.write_text("".join(f"{task}\n" for task in tasks))
    code = (
        'while IFS= read -r task; do watch_task_settled "$task" || exit 1; done < "$1"'
    )
    for limit in (10, 1):
        start = time.monotonic()
        result = run_bash(code, registry)
        assert result.returncode == 0, result.stderr
        assert time.monotonic() - start <= limit


def test_legacy_marker_does_not_skip_rollover_or_changed_log(tmp_path: Path) -> None:
    task = tmp_path / "task"
    settled(task)
    marker = task / ".th_daemon.settled"
    marker.write_text("")
    (task / "context_exit_requested.json").touch()
    assert run_bash('watch_task_settled "$1"', task).returncode != 0
    (task / "context_exit_requested.json").unlink()
    (task / "codex.log").touch()
    os.utime(marker, (1, 1))
    assert run_bash('watch_task_settled "$1"', task).returncode != 0


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
    (pending / ".th_daemon.settled").unlink()
    epoch = time.time() - 90000
    for path in old.iterdir():
        os.utime(path, (epoch, epoch))
    assert (
        run_bash('watch_task_signature "$1" > "$1/.th_daemon.settled"', old).returncode
        == 0
    )
    os.utime(old / ".th_daemon.settled", (epoch, epoch))
    (watch / "tasks.txt").write_text(f"{old}\n{recent}\n{pending}\n")
    result = run_bash('watch_retire_settled "$1"', watch)
    assert result.returncode == 0, result.stderr
    assert (watch / "tasks.txt").read_text().splitlines() == [str(recent), str(pending)]
    assert (watch / "tasks_done.txt").read_text().splitlines() == [str(old)]


def test_failed_pending_continue_reaches_policy_on_next_pass(tmp_path: Path) -> None:
    task = tmp_path / "task"
    watch = tmp_path / "watch"
    task.mkdir()
    watch.mkdir()
    (task / "FAILED").write_text("INCOMPLETE: step2 remains")
    (task / ".th_daemon.failed").touch()
    (task / ".th_digest.failed").touch()
    (task / "VERIFY_FAILED").touch()
    script = r"""
inspect_context_and_job() { printf 'none\t0\t\n'; }
callback() { printf '%s\n' "$2"; }
th_python() { if [[ $2 == team_harness.stop_policy ]]; then printf '자동 계속(1/2)'; fi; }
if ! watch_task_settled "$1"; then
  inspect_task_event "$1" "$2" .th_daemon 600 '' callback
fi
"""
    result = run_bash(script, task, watch)
    assert result.returncode == 0, result.stderr
    assert "자동 계속" in result.stdout


def test_context_rollover_request_is_never_skipped(tmp_path: Path) -> None:
    task = tmp_path / "task"
    settled(task)
    (task / "context_exit_requested.json").touch()
    assert run_bash('watch_task_settled "$1"', task).returncode != 0


def test_quota_expiration_in_shell_matches_launch(tmp_path: Path) -> None:
    lock = tmp_path / "quota_lock.codex"
    lock.write_text("capacity expires=1\n")
    assert run_bash('watch_quota_lock_active "$1"', lock).returncode != 0
    lock.write_text("capacity expires=9999999999\n")
    assert run_bash('watch_quota_lock_active "$1"', lock).returncode == 0
    lock.write_text("quota exhausted\n")
    assert run_bash('watch_quota_lock_active "$1"', lock).returncode == 0


def test_settled_signature_detects_changed_and_deleted_files(tmp_path: Path) -> None:
    task = tmp_path / "task"
    settled(task)
    (task / "STATUS").write_text("new attempt")
    assert run_bash('watch_task_settled "$1"', task).returncode != 0
    (task / "STATUS").unlink()
    (task / "VERIFIED").unlink()
    assert run_bash('watch_task_settled "$1"', task).returncode != 0


def test_successful_policy_settles_only_after_inspection(tmp_path: Path) -> None:
    task = tmp_path / "task"
    watch = tmp_path / "watch"
    settled(task)
    watch.mkdir()
    (task / ".th_daemon.settled").unlink()
    script = r"""
inspect_context_and_job() { printf 'none\t0\t\n'; }
callback() { :; }
th_python() { touch "$3/.th_daemon.settled"; }
watch_task_settled "$1" && exit 1
inspect_task_event "$1" "$2" .th_daemon 600 '' callback
watch_task_settled "$1"
"""
    result = run_bash(script, task, watch)
    assert result.returncode == 0, result.stderr


def test_notified_handoff_skips_only_after_wait_is_finished(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "STATUS").write_text("END step=work")
    (task / ".th_daemon.handoff_stall.1").touch()
    assert (
        run_bash('watch_task_signature "$1" > "$1/.th_daemon.settled"', task).returncode
        == 0
    )
    assert run_bash('watch_task_settled "$1"', task).returncode == 0
    (task / ".th_daemon.handoff_wait.1").touch()
    assert run_bash('watch_task_settled "$1"', task).returncode != 0


def test_new_launch_invalidates_settled_signature(tmp_path: Path) -> None:
    task = tmp_path / "task"
    settled(task)
    (task / ".th_session").write_text("new launch")
    assert run_bash('watch_task_settled "$1"', task).returncode != 0
