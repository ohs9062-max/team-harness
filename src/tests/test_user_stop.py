"""User stops never signal unrelated or recycled processes."""

import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from team_harness.agents.process_identity import capture_starttime

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def test_parent_stop_propagates_and_rejects_reused_pid(tmp_path: Path) -> None:
    parent = tmp_path / "task"
    child = parent / ".th_adopt" / "verify"
    child.mkdir(parents=True)
    worker = subprocess.Popen(["sleep", "300"], start_new_session=True)
    unrelated = subprocess.Popen(["sleep", "300"], start_new_session=True)
    threading.Thread(target=worker.wait, daemon=True).start()
    try:
        (child / "worker.pid").write_text(str(worker.pid))
        (child / "worker.pid.start").write_text(
            json.dumps({"pid": worker.pid, "start": capture_starttime(worker.pid)})
        )
        (parent / "STATUS").write_text(
            "2026-01-01 00:00:00 START step=check replay=safe\n"
        )
        subprocess.run([str(SCRIPTS / "th-stop.sh"), str(parent)], check=True)
        assert worker.poll() is not None
        assert unrelated.poll() is None
        assert (child / "STOPPED_BY_USER").exists()
        assert "rc=130" in (parent / "STATUS").read_text()
        (parent / "worker.pid").write_text(str(unrelated.pid))
        (parent / "worker.pid.start").write_text(
            json.dumps({"pid": unrelated.pid, "start": "linux:1"})
        )
        assert (
            subprocess.run(
                [str(SCRIPTS / "th-stop.sh"), str(parent)], capture_output=True
            ).returncode
            != 0
        )
        assert unrelated.poll() is None
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait()
        unrelated.terminate()
        unrelated.wait()


def test_observed_shim_only_signals_matching_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    observed = subprocess.Popen(["sleep", "300"])
    unrelated = subprocess.Popen(["sleep", "300"])
    try:
        events = [
            {
                "phase": "pre",
                "event": {
                    "pid": observed.pid,
                    "process_start": capture_starttime(observed.pid),
                    "task": str(task),
                    "route": "",
                    "tool": "codex",
                },
            },
            {
                "phase": "pre",
                "event": {
                    "pid": unrelated.pid,
                    "process_start": "linux:1",
                    "task": str(task),
                    "route": "",
                    "tool": "codex",
                },
            },
        ]
        (watch / "exec_pipeline.jsonl").write_text("\n".join(map(json.dumps, events)))
        from team_harness.task_stop import stop

        stop(task, "test")
        assert observed.wait(timeout=3) != 0
        assert unrelated.poll() is None
    finally:
        if observed.poll() is None:
            observed.terminate()
            observed.wait()
        unrelated.terminate()
        unrelated.wait()


def test_wait_job_can_be_stopped(tmp_path: Path) -> None:
    import os

    read_fd, write_fd = os.pipe()
    env = dict(os.environ, TH_JOB_START_NOTIFY_FD=str(write_fd))
    runner = subprocess.Popen(
        [
            str(SCRIPTS / "th-job-start.sh"),
            str(tmp_path),
            "--wait",
            "--",
            "sleep",
            "300",
        ],
        env=env,
        pass_fds=(write_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write_fd)
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as ready:
            assert ready.read(1) == b"R"
            assert ready.read(1) == b"S"
        result = subprocess.run(
            [str(SCRIPTS / "th-stop.sh"), str(tmp_path)],
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert runner.wait(timeout=3) != 0
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.wait()


def test_stopped_job_does_not_signal_fallback_ready(tmp_path: Path) -> None:
    import os

    (tmp_path / "STOPPED_BY_USER").touch()
    read_fd, write_fd = os.pipe()
    try:
        result = subprocess.run(
            [str(SCRIPTS / "th-job-start.sh"), str(tmp_path), "--wait", "--", "true"],
            env=dict(os.environ, TH_JOB_START_NOTIFY_FD=str(write_fd)),
            pass_fds=(write_fd,),
            capture_output=True,
        )
        os.close(write_fd)
        write_fd = -1
        assert result.returncode != 0
        assert os.read(read_fd, 1) == b""
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_stop_suppresses_pending_watch_verification_notice(tmp_path: Path) -> None:
    from team_harness.task_stop import stop

    (tmp_path / ".th_daemon.done").touch()
    (tmp_path / ".th_digest.done").touch()
    stop(tmp_path, "test")
    assert (tmp_path / ".th_daemon.verify_notice").is_file()


@pytest.mark.parametrize("job_mismatch", [False, True])
def test_independent_stop_and_incomplete_watch_event(
    tmp_path: Path, job_mismatch: bool
) -> None:
    task = tmp_path / "task"
    task.mkdir()
    watch = tmp_path / "watch"
    watch.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = dict(
        os.environ, HOME=str(home), TH_WATCH_DIR=str(watch), TH_JOB_TASK=str(task)
    )
    worker = subprocess.Popen(["sleep", "300"], start_new_session=True, env=env)
    job = subprocess.Popen(["sleep", "300"], start_new_session=True, env=env)
    try:
        for name, proc, token in (
            ("worker", worker, "linux:1"),
            ("job", job, "linux:1" if job_mismatch else capture_starttime(job.pid)),
        ):
            (task / f"{name}.pid").write_text(str(proc.pid))
            (task / f"{name}.pid.start").write_text(
                json.dumps({"pid": proc.pid, "start": token})
            )
        (task / "STATUS").write_text(
            "2026-01-01 00:00:00 START step=check replay=safe\n"
        )
        result = subprocess.run(
            [str(SCRIPTS / "th-stop.sh"), str(task)], env=env, capture_output=True
        )
        assert result.returncode != 0
        assert str(worker.pid).encode() in result.stderr
        assert worker.poll() is None
        if job_mismatch:
            assert job.poll() is None
            assert str(job.pid).encode() in result.stderr
        else:
            assert job.wait(timeout=3) != 0
        assert "incomplete" in (task / "STOPPED_BY_USER").read_text()
        status = (task / "STATUS").read_text()
        assert "STOP_INCOMPLETE" in status and "rc=130" not in status
        (watch / "tasks.txt").write_text(f"{task}\n")
        for _ in range(2):
            subprocess.run(
                [str(SCRIPTS / "th-watchd.sh")],
                env=dict(env, TH_ONCE="1"),
                check=True,
                capture_output=True,
            )
        events = (watch / "events.log").read_text()
        assert events.count("[사용자 정지 미완료]") == 1
        assert str(worker.pid) in events
    finally:
        for proc in (worker, job):
            if proc.poll() is None:
                proc.terminate()
            proc.wait()


def test_observed_ignoring_term_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    proc = subprocess.Popen(
        [
            "python3",
            "-u",
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready'); time.sleep(300)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        (watch / "exec_pipeline.jsonl").write_text(
            json.dumps(
                {
                    "phase": "pre",
                    "event": {
                        "pid": proc.pid,
                        "process_start": capture_starttime(proc.pid),
                        "task": str(task),
                        "route": "",
                        "tool": "codex",
                    },
                }
            )
        )
        from team_harness.task_stop import stop

        assert stop(task, "test") == []
        assert proc.wait(timeout=3) == -9
        assert "rc=130" in (task / "STATUS").read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_observed_signal_error_reports_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from team_harness import task_stop

    watch = tmp_path / "watch"
    watch.mkdir()
    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    proc = subprocess.Popen(["sleep", "300"])
    try:
        (watch / "exec_pipeline.jsonl").write_text(
            json.dumps(
                {
                    "phase": "pre",
                    "event": {
                        "pid": proc.pid,
                        "process_start": capture_starttime(proc.pid),
                        "task": str(task),
                        "route": "",
                        "tool": "codex",
                    },
                }
            )
        )

        def denied(*args: object) -> None:
            raise PermissionError("denied")

        monkeypatch.setattr(task_stop.signal, "pidfd_send_signal", denied)
        assert task_stop.stop(task, "test")
        assert "STOP_INCOMPLETE" in (task / "STATUS").read_text()
        assert "incomplete" in (task / "STOPPED_BY_USER").read_text()
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait()
