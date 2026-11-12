import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from team_harness import state_index
from team_harness.session_route import th_session_key

ROOT = Path(__file__).resolve().parents[2]


def test_session_routing_and_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = tmp_path / "owner"
    journal = owner / ".claude"
    journal.mkdir(parents=True)
    (journal / "inbox.md").write_text("")
    watch = tmp_path / "watch"
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.setenv("TH_SKIP_COORD_LOG", "1")
    monkeypatch.setenv("TH_OBSERVE", "1")
    row = dict(
        pid=987654321,
        start="one",
        task=None,
        session_id="claude-id",
        claude_pid=None,
        workspace=str(owner),
        kind="job",
        origin="worker",
        worker_task=None,
        cmd="test",
        route_key="pane-a",
    )
    state_index.update([row])
    state_index.update([], maintenance=True)
    state = (
        watch
        / "inbox_unread"
        / (hashlib.sha256(str(owner).encode()).hexdigest() + ".tsv")
    )
    assert state.read_text().split("\t")[4] == "pane-a"
    size = (journal / "inbox.md").stat().st_size
    cursors = journal / ".inbox_cursor.d"
    cursors.mkdir()
    for key in ("pane-a", "pane-b"):
        (cursors / key).write_text("0\n")

    def read(key: str, ack: int) -> subprocess.CompletedProcess[str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ORCA_PANE_KEY", "CLAUDE_CODE_SESSION_ID", "TH_SESSION_KEY")
        }
        env.update(ORCA_PANE_KEY=key, CLAUDE_CODE_SESSION_ID="claude-id")
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/th-inbox-read.py"),
                str(owner),
                str(ack),
                "peek",
            ],
            env=env,
            capture_output=True,
            text=True,
        )

    assert "관찰 종료" in read("pane-a", 0).stdout
    assert int((cursors / "pane-a").read_text()) < size
    assert not read("pane-b", 1).stdout
    assert int((cursors / "pane-b").read_text()) == 0
    assert int((cursors / "pane-a").read_text()) < size
    assert "관찰 종료" in read("pane-a", 1).stdout
    assert int((cursors / "pane-a").read_text()) == size
    row.update(pid=987654322, start="two", route_key="")
    state_index.update([row])
    state_index.update([], maintenance=True)
    assert (journal / "inbox.md").stat().st_size == size
    assert (
        th_session_key(
            {
                "ORCA_PANE_KEY": "pane-a",
                "CLAUDE_CODE_SESSION_ID": "claude-id",
                "TH_SESSION_KEY": "fallback",
            }
        )
        == "pane-a"
    )


def test_migrates_existing_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path))
    with sqlite3.connect(tmp_path / "th.db") as db:
        db.execute(
            "CREATE TABLE processes(pid INTEGER, start TEXT, first_seen TEXT, last_seen TEXT, ended_at TEXT, kind TEXT, origin TEXT, session_id TEXT, workspace TEXT, worker_task TEXT, task TEXT, cmd TEXT, PRIMARY KEY(pid,start))"
        )
        db.execute("INSERT INTO processes(pid,start) VALUES (1,'old')")
    state_index.update([])
    with sqlite3.connect(tmp_path / "th.db") as db:
        assert db.execute("SELECT route_key FROM processes WHERE pid=1").fetchone() == (
            None,
        )
        assert db.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone() == ("3",)
