from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from team_harness import state_index
from team_harness import state_recall


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_merged_failure_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "commit", "--allow-empty", "-m", "base")
    git(repo, "checkout", "-b", "task/example")
    git(repo, "commit", "--allow-empty", "-m", "task")
    git(repo, "checkout", "main")
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    task = str(tmp_path / "example")
    with sqlite3.connect(watch / "th.db") as db:
        db.executescript(state_index.SCHEMA)
        db.execute(
            "INSERT INTO tasks(task,owner,task_key,state) VALUES (?,?,?,?)",
            (task, str(repo), "example", "verify_failed"),
        )
        db.execute(
            "INSERT INTO failures VALUES (?,1,datetime('now','-1 minute'),'검증 실패','detail','test')",
            (task,),
        )
    monkeypatch.setattr(sys, "argv", ["state_recall", "recent", str(repo)])
    start = time.perf_counter()
    state_recall.main()
    assert "실패:" in capsys.readouterr().out
    git(repo, "merge", "--ff-only", "task/example")
    state_recall.main()
    output = capsys.readouterr().out
    print(f"recall elapsed: {time.perf_counter() - start:.3f}s")
    assert "실패:" not in output
    assert "지난 실패(해결됨)" in state_recall.why(task, readonly=True)
    git(repo, "reset", "--hard", "HEAD~1")
    with sqlite3.connect(watch / "th.db") as db:
        db.execute(
            "INSERT INTO coord_log VALUES (?,datetime('now'),'완료',?,'완료','hash')",
            (str(repo), task),
        )
    state_recall.main()
    assert "실패:" not in capsys.readouterr().out
    with sqlite3.connect(watch / "th.db") as db:
        db.execute("DELETE FROM coord_log")
        db.execute("UPDATE tasks SET owner=?", (str(tmp_path / "missing"),))
    monkeypatch.setattr(
        sys, "argv", ["state_recall", "recent", str(tmp_path / "missing")]
    )
    state_recall.main()
    assert "실패:" in capsys.readouterr().out
