import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from team_harness import proc_observer
from team_harness import state_index


def test_detached_process_origin_and_ended(tmp_path, monkeypatch):
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
        assert record["origin"] == "worker"
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


def test_status_displays_observed_session(tmp_path, monkeypatch):
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
        assert "12345" in run.stdout
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
        assert "12345" in run_all.stdout
    finally:
        child.terminate()
        child.wait()


def test_sql_header(tmp_path):
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


def test_status_index_failure_preserves_output(tmp_path):
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
    assert first.stdout == second.stdout
    assert "observation unavailable" in second.stderr


def test_coord_log_incremental_and_why(tmp_path, monkeypatch):
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


def test_parent_environment_fallback(tmp_path, monkeypatch):
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


def test_why_similar_failure(tmp_path, monkeypatch):
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


def test_hook_events_and_disabled(tmp_path, monkeypatch):
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


def test_hook_recall_limits_and_error(tmp_path, monkeypatch):
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


def test_hook_session_recall_limit_and_disable(tmp_path, monkeypatch):
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
