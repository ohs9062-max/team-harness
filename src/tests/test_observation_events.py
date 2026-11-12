from datetime import datetime
from datetime import UTC
import json
from pathlib import Path
import sqlite3

import pytest

from team_harness import observation_events
from team_harness import state_index


def test_notices_and_breaker_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = sqlite3.connect(":memory:")
    db.executescript(state_index.SCHEMA)
    now = datetime.now(UTC).isoformat()
    owner = str(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    (task / "FAILED").write_text("first\nfailed result\n")
    db.execute(
        "INSERT INTO processes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            "start",
            now,
            now,
            now,
            "job",
            "coordinator",
            "s",
            owner,
            None,
            str(task),
            "job",
        ),
    )
    db.execute(
        "INSERT INTO processes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (2, "start", now, now, now, "job", "user", "s", owner, None, None, "user job"),
    )
    for i in range(10):
        db.execute(
            "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(task), i + 1, "codex", None, None, now, 1, 40000, "FAILED", None),
        )
    notices = []
    monkeypatch.setattr(
        observation_events, "_append", lambda *args: notices.append(args)
    )
    observation_events.process_events(db, set(), now)
    observation_events.process_events(db, set(), now)
    assert len([x for x in notices if " | 관찰 종료 | " in x[2]]) == 1
    assert "failed result" in notices[0][2]
    assert len([x for x in notices if x[2].startswith("차단기(관찰)")]) == 2
    assert db.execute("SELECT count(*) FROM breaker_events").fetchone()[0] == 2


def test_repeated_failure_uses_prompt_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = sqlite3.connect(":memory:")
    db.executescript(state_index.SCHEMA)
    now = datetime.now(UTC).isoformat()
    task = str(tmp_path / "task")
    verdicts = json.dumps({"work_location": "/work", "prompt_hash": "hash"})
    db.execute(
        "INSERT INTO shim_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            now,
            1,
            "s",
            str(tmp_path),
            task,
            "codex",
            None,
            None,
            1,
            verdicts,
            0,
            "observe",
        ),
    )
    for attempt in (1, 2):
        db.execute(
            "INSERT INTO failures VALUES (?,?,?,?,?,?)",
            (task, attempt, now, "cause", "detail", str(attempt)),
        )
    notices = []
    monkeypatch.setattr(
        observation_events, "_append", lambda *args: notices.append(args)
    )
    observation_events.process_events(db, set(), now)
    observation_events.process_events(db, set(), now)
    assert len(notices) == 1
    assert "같은 원인 반복 실패 2회" in notices[0][2]


def test_disabled_observation_does_not_end_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path))
    monkeypatch.setenv("TH_SKIP_COORD_LOG", "1")
    row = {
        "pid": 42,
        "start": "identity",
        "task": None,
        "session_id": None,
        "claude_pid": None,
        "workspace": None,
        "kind": "job",
        "origin": "user",
        "worker_task": None,
        "cmd": "job",
    }
    state_index.update([row])
    monkeypatch.setenv("TH_OBSERVE", "0")
    state_index.update(maintenance=True)
    with sqlite3.connect(tmp_path / "th.db") as db:
        assert db.execute("SELECT ended_at FROM processes WHERE pid=42").fetchone() == (
            None,
        )


def test_worktree_routing_and_coord_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta
    import subprocess

    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "-b", "other", str(worktree)],
        check=True,
    )
    journal = main / ".claude"
    journal.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.delenv("TH_ROLE", raising=False)
    monkeypatch.delenv("TH_SKIP_COORD_LOG", raising=False)
    now = datetime.now(UTC)
    db = sqlite3.connect(":memory:")
    db.executescript(state_index.SCHEMA)
    db.execute(
        "INSERT INTO processes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            99999999,
            "start",
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
            "job",
            "worker",
            "s",
            str(worktree),
            None,
            None,
            "run command",
        ),
    )
    observation_events.process_events(db, set(), now.isoformat())
    line = (journal / "inbox.md").read_text().strip()
    assert (
        line
        == f"{now.astimezone().strftime('%Y-%m-%d %H:%M')} | 관찰:run command | 관찰 종료 | 결과 표식 없음 | -"
    )
    assert not (worktree / ".claude").exists()
    stamp = (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    (journal / "work_log.md").write_text(f"{stamp} | 결정 | test | recorded\n")
    row = {
        "pid": 99999999,
        "start": "start",
        "task": None,
        "session_id": "s",
        "claude_pid": None,
        "workspace": str(worktree),
        "kind": "job",
        "origin": "worker",
        "worker_task": None,
        "cmd": "run command",
    }
    state_index.update([row])
    with sqlite3.connect(tmp_path / "watch" / "th.db") as index:
        assert index.execute("SELECT workspace FROM processes").fetchone() == (
            str(worktree),
        )
        assert index.execute("SELECT workspace FROM coord_log").fetchone() == (
            str(main),
        )


def test_missing_journal_and_explicit_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess
    import sys

    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    observation_events._append(str(tmp_path), "s", "message")
    assert not (tmp_path / ".claude").exists()
    owner = tmp_path / "owner"
    owner.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print(1, flush=True); time.sleep(60)"],
        env={**os.environ, "TH_WORKSPACE": str(owner)},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "1"
        assert observation_events.coordinator_workspace(
            str(tmp_path), child.pid
        ) == str(owner)
    finally:
        child.terminate()
        child.wait()
