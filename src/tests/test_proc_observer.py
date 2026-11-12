import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

from team_harness import proc_observer
from team_harness import state_index


def _watcher_ancestor() -> bool:
    return proc_observer.has_ancestor_command(
        "th-watchd.sh"
    ) or proc_observer.has_ancestor_command("team_harness.maintenance")


def test_detached_process_origin_and_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TH_OBSERVE", "1")
    watch = tmp_path / "watch"
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("# Child task\n")
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.setenv("TH_ROLE", "worker")
    child_env = dict(
        os.environ,
        TH_ROLE="worker",
        TH_WORKER_OUTPUT_DIR=str(task),
        CLAUDE_CODE_SESSION_ID="test-session",
        CLAUDE_PID="12345",
        TH_WORKSPACE=str(tmp_path),
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=task,
        env=child_env,
        start_new_session=True,
    )
    try:
        rows = proc_observer.observe()
        record = next(row for row in rows if row["pid"] == child.pid)
        assert record["task"] == str(task)
        # Verification may itself run under the watcher. Its descendants must
        # retain that origin even when their environment identifies a worker.
        assert record["origin"] == ("watchd" if _watcher_ancestor() else "worker")
        assert record["session_id"] == "test-session"
        state_index.update()
        with sqlite3.connect(watch / "th.db") as db:
            assert db.execute(
                "SELECT ended_at FROM processes WHERE pid=?", (child.pid,)
            ).fetchone() == (None,)
    finally:
        child.terminate()
        child.wait()
    state_index.update()
    with sqlite3.connect(watch / "th.db") as db:
        assert (
            db.execute(
                "SELECT ended_at FROM processes WHERE pid=?", (child.pid,)
            ).fetchone()[0]
            is not None
        )


def test_status_displays_observed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TH_OBSERVE", "1")
    repo = Path(__file__).resolve().parents[2]
    task = tmp_path / "detached"
    task.mkdir()
    (task / "task.md").write_text("# Observed title\n")
    env = dict(
        os.environ,
        PYTHONPATH="",
        TH_ROLE="worker",
        TH_SKIP_COORD_LOG="1",
        TH_WATCH_DIR=str(tmp_path / "watch"),
        HOME=str(tmp_path),
        TH_WORKER_OUTPUT_DIR=str(task),
        CLAUDE_CODE_SESSION_ID="isolated-session",
        CLAUDE_PID="12345",
        TH_WORKSPACE=str(tmp_path),
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=task,
        env=env,
        start_new_session=True,
    )
    try:
        run = subprocess.run(
            ["bash", str(repo / "scripts/th-status.sh")],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert "Observed title" in run.stdout
        assert (
            "감시 데몬" if _watcher_ancestor() else "코디네이터 종료됨"
        ) in run.stdout
        run_all = subprocess.run(
            ["bash", str(repo / "scripts/th-status.sh"), "--all"],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert "Observed title" in run_all.stdout
        assert (
            "감시 데몬" if _watcher_ancestor() else "코디네이터 종료됨"
        ) in run_all.stdout
    finally:
        child.terminate()
        child.wait()


def test_sql_header(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    env = dict(
        os.environ,
        PYTHONPATH="",
        TH_ROLE="worker",
        TH_SKIP_COORD_LOG="1",
        TH_WATCH_DIR=str(tmp_path / "watch"),
        HOME=str(tmp_path),
    )
    subprocess.run(
        ["bash", str(repo / "scripts/th-db.sh"), "update"], env=env, check=True
    )
    result = subprocess.run(
        [
            "bash",
            str(repo / "scripts/th-db.sh"),
            "sql",
            "SELECT count(*) AS n FROM processes",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.splitlines()[0] == "n"


def test_status_index_failure_preserves_output(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    watch = tmp_path / "watch"
    watch.mkdir()
    env = dict(os.environ, PYTHONPATH="", TH_WATCH_DIR=str(watch), HOME=str(tmp_path))
    command = ["bash", str(repo / "scripts/th-status.sh")]
    first = subprocess.run(
        command, cwd=tmp_path, env=env, text=True, capture_output=True, check=True
    )
    # A directory at the database path makes SQLite fail without affecting the table.
    bad_watch = tmp_path / "bad-watch"
    (bad_watch / "th.db").mkdir(parents=True)
    env["TH_WATCH_DIR"] = str(bad_watch)
    second = subprocess.run(
        command, cwd=tmp_path, env=env, text=True, capture_output=True, check=True
    )
    assert first.stdout.partition("  실행")[2] == second.stdout.partition("  실행")[2]
    assert "observation unavailable" in second.stderr


def test_status_observer_failure_preserves_output(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "if os.environ.get('TEST_OBSERVER_FAILURE') == '1':\n"
        " import team_harness.proc_observer as observer\n"
        " def fail(): raise RuntimeError('test observer failure')\n"
        " observer.observe = fail\n"
    )
    env = dict(
        os.environ,
        PYTHONPATH=f"{injection}:{repo / 'src'}",
        TH_WATCH_DIR=str(tmp_path / "watch"),
        HOME=str(tmp_path),
    )
    command = ["bash", str(repo / "scripts/th-status.sh")]
    first = subprocess.run(
        command, cwd=tmp_path, env=env, text=True, capture_output=True, check=True
    )
    env["TEST_OBSERVER_FAILURE"] = "1"
    second = subprocess.run(
        command, cwd=tmp_path, env=env, text=True, capture_output=True, check=True
    )
    assert first.stdout == second.stdout
    assert "test observer failure" in second.stderr


@pytest.mark.parametrize("with_pid", [True, False])
def test_session_file_workspace_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_pid: bool
):
    monkeypatch.setenv("TH_OBSERVE", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "12345.json").write_text(
        json.dumps({"sessionId": "fixture-session", "cwd": str(tmp_path)})
    )
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("# Test task\n")
    env = dict(os.environ, CLAUDE_PID="12345", CLAUDE_CODE_SESSION_ID="fixture-session")
    if not with_pid:
        env.pop("CLAUDE_PID", None)
    env.pop("TH_WORKSPACE", None)
    env.pop("ORCA_WORKTREE_ID", None)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=task, env=env
    )
    try:
        record = next(row for row in proc_observer.observe() if row["pid"] == child.pid)
        assert record["workspace"] == str(tmp_path)
        assert record["session_id"] == "fixture-session"
    finally:
        child.terminate()
        child.wait()


def test_all_places_child_below_registered_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TH_OBSERVE", "1")
    repo = Path(__file__).resolve().parents[2]
    watch = tmp_path / "watch"
    watch.mkdir()
    worker = tmp_path / "registered-worker"
    child_task = tmp_path / "child-task"
    worker.mkdir()
    child_task.mkdir()
    (worker / "task.md").write_text("# Registered worker title\n")
    (child_task / "task.md").write_text("# Child process title\n")
    (watch / "tasks.txt").write_text(f"{worker}\n")
    env = dict(
        os.environ,
        PYTHONPATH="",
        HOME=str(tmp_path),
        TH_WATCH_DIR=str(watch),
        TH_WORKER_OUTPUT_DIR=str(worker),
        TH_ROLE="worker",
        CLAUDE_PID=str(os.getpid()),
        CLAUDE_CODE_SESSION_ID="test-session",
        TH_WORKSPACE=str(tmp_path),
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=child_task, env=env
    )
    try:
        run = subprocess.run(
            ["bash", str(repo / "scripts/th-status.sh"), "--all"],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        lines = run.stdout.splitlines()
        parent_line = next(
            i
            for i, line in enumerate(lines)
            if "registered-worker" in line and line.startswith("|")
        )
        if _watcher_ancestor():
            assert any(
                "Child process title" in line and str(child.pid) in line
                for line in lines
            )
            assert "감시 데몬" in run.stdout
        else:
            assert "Child process title" in lines[parent_line + 1]
            assert str(child.pid) in lines[parent_line + 1]
    finally:
        child.terminate()
        child.wait()


def test_coord_log_incremental_and_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from datetime import datetime
    from datetime import timedelta
    from datetime import UTC

    from team_harness import state_recall

    workspace = tmp_path / "workspace"
    journal = workspace / ".claude"
    journal.mkdir(parents=True)
    task = tmp_path / "example-task"
    task.mkdir()
    (task / "task.md").write_text("# Example\n")
    watch = tmp_path / "watch"
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.delenv("TH_ROLE", raising=False)
    monkeypatch.delenv("TH_SKIP_COORD_LOG", raising=False)
    monkeypatch.setattr(state_index.proc_observer, "observe", lambda: [])
    state_index.update()
    (watch / "tasks.txt").write_text(str(task) + "\n")
    (task / ".th_owner").write_text(str(workspace))
    (task / "FAILED").write_text("quota exceeded")
    stamp = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    (journal / "work_log.md").write_text(
        f"{stamp} | 결정 | example-task | retry later\n"
    )
    state_index.update()
    state_index.update()
    with sqlite3.connect(watch / "th.db") as db:
        assert db.execute("SELECT kind,task FROM coord_log").fetchall() == [
            ("결정", str(task))
        ]
    text = state_recall.why(str(task))
    assert "쿼터" in text and "retry later" in text


def test_parent_environment_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TH_OBSERVE", "1")
    task = tmp_path / "parent-task"
    task.mkdir()
    (task / "task.md").write_text("# Parent task\n")
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import os,subprocess,sys; "
        'p=subprocess.Popen([sys.executable,"-c",sys.argv[1]],env={},cwd=os.environ["TASKPATH"]); '
        "print(p.pid,flush=True); "
        "sys.stdin.read(); p.terminate(); p.wait()"
    )
    env = dict(
        os.environ,
        TH_WORKER_OUTPUT_DIR=str(task),
        CLAUDE_CODE_SESSION_ID="parent-session",
        TASKPATH=str(task),
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, child_code],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        record = next(row for row in proc_observer.observe() if row["pid"] == child_pid)
        assert record["session_id"] == "parent-session"
        assert child_pid > 0
    finally:
        if parent.stdin:
            parent.stdin.close()
        parent.wait(timeout=5)


def test_why_similar_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from team_harness import state_recall

    watch = tmp_path / "watch"
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.setenv("TH_ROLE", "worker")
    monkeypatch.setattr(state_index.proc_observer, "observe", lambda: [])
    state_index.update()
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    with sqlite3.connect(watch / "th.db") as db:
        db.execute(
            "INSERT INTO failures VALUES (?,1,datetime('now'),'쿼터','quota','first')",
            (first,),
        )
        db.execute(
            "INSERT INTO failures VALUES (?,1,datetime('now'),'쿼터','quota','second')",
            (second,),
        )
        db.execute(
            "INSERT INTO coord_log VALUES (?,datetime('now'),'결정',?,'retry tomorrow','hash')",
            (str(tmp_path), second),
        )
    text = state_recall.why(first)
    assert second in text and "retry tomorrow" in text


def test_hook_events_and_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    watch = tmp_path / "watch"
    workspace = tmp_path / "workspace"
    journal = workspace / ".claude"
    journal.mkdir(parents=True)
    task = tmp_path / "hook-task"
    task.mkdir()
    (task / "task.md").write_text("# hook task\n")
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.setenv("TH_ROLE", "worker")
    monkeypatch.setattr(state_index.proc_observer, "observe", lambda: [])
    state_index.update()
    with sqlite3.connect(watch / "th.db") as db:
        db.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
            (str(task), str(workspace), None, None, "now", "FAILED", "step", "now"),
        )
    (journal / ".inbox_offset").write_text("0")
    (journal / "inbox.md").write_text(f"실패 {task}\n")
    repo = Path(__file__).resolve().parents[2]
    env = dict(
        os.environ,
        PYTHONPATH="",
        TH_ROLE="",
        TH_SKIP_COORD_LOG="1",
        TH_WATCH_DIR=str(watch),
        HOME=str(tmp_path),
        CLAUDE_PROJECT_DIR=str(workspace),
    )
    run = subprocess.run(
        ["bash", str(repo / "scripts/journal-hook.sh"), "prompt"],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert "## 새 사건" in run.stdout and f"작업: {task}" in run.stdout
    (journal / ".inbox_offset").write_text("0")
    env["TH_AUTO_RECALL"] = "0"
    run = subprocess.run(
        ["bash", str(repo / "scripts/journal-hook.sh"), "prompt"],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert "## 새 사건" in run.stdout and f"작업: {task}" not in run.stdout


def test_hook_recall_limits_and_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    watch = tmp_path / "watch"
    workspace = tmp_path / "workspace"
    journal = workspace / ".claude"
    journal.mkdir(parents=True)
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    state_index.update()
    tasks = [tmp_path / f"task-{i}" for i in range(3)]
    with sqlite3.connect(watch / "th.db") as db:
        for task in tasks:
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (str(task), str(workspace), None, None, "now", "FAILED", "step", "now"),
            )
    inbox = journal / "inbox.md"
    inbox.write_text("".join(f"실패 {task}\n" for task in tasks))
    offset = journal / ".inbox_offset"
    offset.write_text("0")
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH="", TH_ROLE="", CLAUDE_PROJECT_DIR=str(workspace))
    command = ["bash", str(repo / "scripts/journal-hook.sh"), "prompt"]
    run = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert run.stdout.count("작업: ") == 2
    assert len(run.stdout.splitlines()) <= len(inbox.read_text().splitlines()) + 41
    offset.write_text("0")
    error_watch = tmp_path / "error-watch"
    (error_watch / "th.db").mkdir(parents=True)
    env["TH_WATCH_DIR"] = str(error_watch)
    run = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert "작업: " not in run.stdout
    assert all(str(task) in run.stdout for task in tasks)


def test_hook_session_recall_limit_and_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = tmp_path / "watch"
    workspace = tmp_path / "workspace"
    journal = workspace / ".claude"
    journal.mkdir(parents=True)
    (journal / "inbox.md").write_text("recent inbox\n")
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    state_index.update()
    with sqlite3.connect(watch / "th.db") as db:
        for i in range(7):
            db.execute(
                "INSERT INTO coord_log VALUES (?,datetime('now'),'결정',NULL,?,?)",
                (str(workspace), f"decision-{i}", f"hash-{i}"),
            )
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH="", TH_ROLE="", CLAUDE_PROJECT_DIR=str(workspace))
    command = ["bash", str(repo / "scripts/journal-hook.sh")]
    run = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert "## 미해결 실패·최근 결정" in run.stdout
    assert run.stdout.count("결정: ") == 5
    assert len(run.stdout.split("## 미해결 실패·최근 결정\n", 1)[1].splitlines()) <= 9
    env["TH_AUTO_RECALL"] = "0"
    run = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert "## inbox (최근)" in run.stdout
    assert "## 미해결 실패·최근 결정" not in run.stdout


def test_watchd_descendants_and_grouped_registered_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TH_OBSERVE", "1")
    repo = Path(__file__).resolve().parents[2]
    watch = tmp_path / "watch"
    watch.mkdir()
    registered = tmp_path / "registered"
    registered.mkdir()
    (registered / "task.md").write_text("# Registered title\n")
    (watch / "tasks.txt").write_text(f"{registered}\n")
    task = tmp_path / "daemon-task"
    task.mkdir()
    (task / "task.md").write_text("# Daemon title\n")
    script = tmp_path / "th-watchd.sh"
    script.write_text('"$1" -c "import time; time.sleep(60)" &\nwait\n')
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        PYTHONPATH="",
        TH_WATCH_DIR=str(watch),
        TH_SKIP_COORD_LOG="1",
        CLAUDE_PID="12345",
        CLAUDE_CODE_SESSION_ID="wrong-session",
        TH_WORKSPACE=str(tmp_path),
    )
    daemon = subprocess.Popen(["bash", str(script), sys.executable], cwd=task, env=env)
    children = [
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], cwd=task, env=env
        )
        for _ in range(2)
    ]
    registered_child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=registered, env=env
    )
    try:
        import time

        for _ in range(100):
            records = proc_observer.observe()
            daemon_children = [r for r in records if r["parent"] == daemon.pid]
            if daemon_children:
                break
            time.sleep(0.01)
        assert daemon_children
        assert daemon_children[0]["origin"] == "watchd"
        assert daemon_children[0]["kind"] == "검수·유지보수"
        filtered = proc_observer.display_records(
            records, {str(registered)}, {daemon.pid}
        )
        assert not any(
            r["pid"] in (registered_child.pid, daemon_children[0]["pid"])
            for r in filtered
        )
        grouped = next(r for r in filtered if r["task"] == str(task))
        assert grouped["count"] >= 2
        for option in ([], ["--all"]):
            run = subprocess.run(
                ["bash", str(repo / "scripts/th-status.sh"), *option],
                cwd=tmp_path,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            lines = run.stdout.splitlines()
            daemon_rows = [
                line for line in lines if "↳" in line and "Daemon title" in line
            ]
            assert len(daemon_rows) == 1
            assert "3개" in daemon_rows[0]
            assert "검수·유지보수" in daemon_rows[0]
            assert "감시 데몬" in run.stdout
            assert not any("↳" in line and "Registered title" in line for line in lines)
    finally:
        for child in children + [registered_child]:
            child.terminate()
            child.wait()
        if daemon_children:
            os.kill(daemon_children[0]["pid"], 15)
        daemon.wait(timeout=5)


def test_session_job_age_and_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TH_OBSERVE", "1")
    child_env = dict(
        os.environ,
        CLAUDE_PID="99999999",
        CLAUDE_CODE_SESSION_ID="job-test",
        TH_WORKSPACE=str(tmp_path),
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=child_env,
        start_new_session=True,
    )
    helper = subprocess.Popen(
        ["sleep", "60"], cwd=tmp_path, env=child_env, start_new_session=True
    )
    clean_env = {
        key: value
        for key, value in child_env.items()
        if key not in ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID")
    }
    unrelated = subprocess.Popen(
        ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=clean_env,
        start_new_session=True,
    )
    try:
        assert not any(r["pid"] == child.pid for r in proc_observer.observe())
        original = Path.read_text

        def advanced(path: Path, *args: Any, **kwargs: Any) -> str:
            value = original(path, *args, **kwargs)
            if str(path) in {
                f"/proc/{pid}/stat" for pid in (child.pid, helper.pid, unrelated.pid)
            }:
                prefix, fields = value.rsplit(") ", 1)
                parts = fields.split()
                parts[1] = "1"
                return prefix + ") " + " ".join(parts)
            if str(path) == "/proc/uptime":
                return f"{float(value.split()[0]) + 31} 0"
            return value

        monkeypatch.setattr(Path, "read_text", advanced)
        rows = proc_observer.observe()
        row = next(r for r in rows if r["pid"] == child.pid)
        assert row["kind"] == "작업"
        assert row["session_id"] == "job-test"
        assert not any(r["pid"] in (helper.pid, unrelated.pid) for r in rows)
    finally:
        unrelated.terminate()
        unrelated.wait()
        child.terminate()
        helper.terminate()
        child.wait()
        helper.wait()


@pytest.mark.parametrize("mode", ["background", "nohup", "setsid", "script", "popen"])
def test_job_launch_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    import shlex

    monkeypatch.setenv("TH_OBSERVE", "1")
    executable = shlex.quote("/usr/bin/python3")
    command = f"{executable} -c 'import time; time.sleep(2)'"
    if mode == "nohup":
        command = "nohup " + command
    if mode == "setsid":
        command = "setsid " + command
    if mode == "script":
        script = tmp_path / "runner.sh"
        script.write_text(command + " &\nwait\n")
        args = ["bash", str(script)]
    elif mode == "popen":
        args = ["/usr/bin/python3", "-c", "import time; time.sleep(2)"]
    else:
        args = ["bash", "-c", command + " & wait"]
    env = dict(
        os.environ,
        CLAUDE_PID="99999999",
        CLAUDE_CODE_SESSION_ID="launch-test",
        TH_WORKSPACE=str(tmp_path),
    )
    child = subprocess.Popen(
        args,
        cwd=tmp_path,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        original = Path.read_text

        def advanced(path: Path, *args: Any, **kwargs: Any) -> str:
            value = original(path, *args, **kwargs)
            if str(path) == f"/proc/{child.pid}/stat":
                prefix, fields = value.rsplit(") ", 1)
                parts = fields.split()
                parts[1] = "1"
                return prefix + ") " + " ".join(parts)
            if str(path) == "/proc/uptime":
                return f"{float(value.split()[0]) + 31} 0"
            return value

        monkeypatch.setattr(Path, "read_text", advanced)
        rows = proc_observer.observe()
        row = next(r for r in rows if r["pid"] == child.pid)
        assert row["kind"] == "작업"
        assert row["session_id"] == "launch-test"
        assert row["claude_pid"] == "99999999"
    finally:
        child.wait(timeout=5)


@pytest.mark.parametrize("kind", ["작업", "job"])
def test_status_shows_ended_coordinator_for_job(tmp_path: Path, kind: str):
    repo = Path(__file__).resolve().parents[2]
    row = dict(
        pid=99999999,
        start="boot:1",
        cmd="example-job",
        cwd=str(tmp_path),
        task=None,
        origin="coordinator",
        session_id="job-test",
        route_key="",
        claude_pid="99999999",
        workspace=str(tmp_path),
        worker_task=None,
        kind=kind,
        parent=1,
        child_summary="child-command",
    )
    (tmp_path / "sitecustomize.py").write_text(
        "import team_harness.coordinator_identity as i\ni.live_coordinators = lambda: []\n"
        f"import team_harness.proc_observer as p\np.observe = lambda: [{row!r}]\n"
    )
    env = dict(
        os.environ,
        PYTHONPATH=str(tmp_path),
        HOME=str(tmp_path),
        TH_WATCH_DIR=str(tmp_path / "watch"),
        TH_ROLE="worker",
        TH_SKIP_COORD_LOG="1",
    )
    result = subprocess.run(
        ["bash", str(repo / "scripts/th-status.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert "코디네이터 종료됨" in result.stdout
    assert "example-job" in result.stdout
    if kind == "작업":
        assert "child-command" in result.stdout


def test_task_ignores_git_roots_and_accepts_subdirectories(tmp_path: Path):
    from team_harness.proc_observer import _task

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "STATUS").write_text("old")
    child = root / "child"
    child.mkdir()
    assert _task(str(root)) is None
    assert _task(str(child)) is None
    (child / "job.pid").write_text("1")
    assert _task(str(child)) == str(child)
    worktree = tmp_path / "linked"
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-q", str(worktree)], check=True
    )
    (worktree / "STATUS").write_text("old")
    assert _task(str(worktree)) is None


def test_display_records_filters_user_and_separates_owners(tmp_path: Path):
    from team_harness.proc_observer import display_records

    base = dict(
        start="boot:1",
        cmd="runner",
        cwd=str(tmp_path),
        task=str(tmp_path),
        origin="coordinator",
        workspace=str(tmp_path),
        kind="job",
    )
    records = [
        dict(base, pid=90000001, session_id="one", claude_pid="100"),
        dict(base, pid=90000002, session_id="two", claude_pid="200"),
        dict(base, pid=90000003, session_id="", claude_pid="", origin="user"),
    ]
    result = display_records(records, set(), set())
    assert len(result) == 2
    assert {row["session_id"] for row in result} == {"one", "two"}


def test_display_command_unwraps_snapshot_eval():
    from team_harness.proc_observer import display_command

    command = "/bin/bash -c source /tmp/shell-snapshots/snapshot-bash-x.sh ';' eval 'python run.py --name test'"
    assert display_command(command) == "python run.py --name test"
    from team_harness.proc_observer import display_records

    long_command = command.replace("/tmp/", "/tmp/" + "a" * 150 + "/")
    record = dict(
        pid=90000009,
        origin="coordinator",
        cmd=long_command[:120],
        display_cmd=long_command,
        kind="작업",
    )
    assert (
        display_records([record], set(), set())[0]["cmd"] == "python run.py --name test"
    )
    assert display_command("eval 'other'") == "eval 'other'"
    assert (
        display_command("source /tmp/shell-snapshots/x eval 'broken")
        == "source /tmp/shell-snapshots/x eval 'broken"
    )


def test_raw_descendants_and_wrapped_commands():
    proc = {
        1: dict(parent=0, ticks=1, args=["bash", "runner.sh"]),
        2: dict(parent=1, ticks=2, args=["flock", "/tmp/lock", "python", "-u", "x.py"]),
        3: dict(parent=2, ticks=3, args=["python", "-u", "x.py"]),
        4: dict(parent=1, ticks=1, args=["sleep", "10"]),
    }
    rows = [dict(pid=1, kind="job")]
    proc_observer._add_child_summaries(rows, proc)
    assert rows[0]["child_summary"] == "python -u x.py"
    for args in (
        ["timeout", "60", "python", "x.py"],
        ["env", "A=B", "python", "x.py"],
        ["nohup", "python", "x.py"],
    ):
        assert proc_observer._useful_command(args) == "python x.py"


def test_tool_shell_deduplicates_only_unique_matching_row():
    base = dict(
        origin="coordinator",
        claude_pid="10",
        session_id="sid",
        workspace="/work",
        task=None,
    )
    raw = "bash -c source /tmp/shell-snapshots/x ';' eval 'O=/tmp/out bash experiments/runner.sh'"
    shell = dict(base, pid=900001, kind="작업", cmd=raw, display_cmd=raw)
    runner = dict(base, pid=900002, kind="job", cmd="bash experiments/runner.sh")
    other = dict(
        base,
        pid=900003,
        kind="작업",
        cmd="bash -c other",
        display_cmd="bash -c source /tmp/shell-snapshots/x ';' eval 'python other.py'",
    )
    rows = proc_observer.display_records([shell, runner, other], set(), set())
    assert {row["pid"] for row in rows} == {900002, 900003}
    duplicate = dict(runner, pid=900004, task="/different")
    assert (
        len(proc_observer.display_records([shell, runner, duplicate], set(), set()))
        == 3
    )
