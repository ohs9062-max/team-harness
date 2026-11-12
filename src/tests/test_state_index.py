from datetime import datetime
from datetime import timedelta
from datetime import UTC
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from team_harness import maintenance
from team_harness import state_index


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(state_index.proc_observer, "observe", lambda: [])
    wd = tmp_path / "watch"
    wd.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(wd))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return wd


def rows(wd: Path, sql: str):
    with sqlite3.connect(wd / "th.db") as db:
        return db.execute(sql).fetchall()


def test_incremental_and_truncated(env: Path):
    log = env / "events.log"
    log.write_text("2000-01-01 00:00:00 old\n")
    state_index.update()
    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log.open("a") as out:
        out.write(f"{future} " + "x" * 200 + "\n")
    state_index.update()
    state_index.update()
    assert len(rows(env, "select * from events")) == 1
    offset = rows(env, "select offset from ingest_state")[0][0]
    assert offset == log.stat().st_size
    log.write_text(f"{future} replacement\n")
    state_index.update()
    assert len(rows(env, "select * from events")) == 2


def test_failures_and_adoption(env: Path, tmp_path: Path):
    state_index.update()
    task = tmp_path / "task"
    task.mkdir()
    (env / "tasks.txt").write_text(str(task) + "\n")
    (task / "task.md").write_text("kind: code\ntask_key: hello\n")
    (task / "FAILED").write_text("quota exceeded")
    folder = tmp_path / "crosscheck"
    folder.mkdir()
    (folder / "crosscheck.json").write_text(
        json.dumps({"names": ["terra"], "claim": "claim"})
    )
    verifier = folder / "terra"
    verifier.mkdir()
    (verifier / "VERDICT.md").write_text("반례: 없음\nVERDICT: PASS\n")
    (env / "tasks.txt").write_text(str(task) + "\n" + str(verifier) + "\n")
    (verifier / "task.md").write_text("kind: code\n")
    state_index.update()
    state_index.update()
    assert rows(env, "select category from failures") == [("쿼터",)]
    assert rows(env, "select verdict, counterexample from adoptions") == [
        ("PASS", "없음")
    ]


def test_cli(env: Path):
    script = Path(__file__).resolve().parents[2] / "scripts/th-db.sh"
    runenv = dict(
        os.environ, TH_WATCH_DIR=str(env), HOME=str(env.parent), PYTHONPATH=""
    )
    assert subprocess.run(["bash", str(script), "update"], env=runenv).returncode == 0
    assert (
        subprocess.run(
            ["bash", str(script), "sql", "SELECT value FROM meta"],
            env=runenv,
            capture_output=True,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["bash", str(script), "sql", "DELETE FROM meta"],
            env=runenv,
            capture_output=True,
        ).returncode
        != 0
    )
    assert (
        subprocess.run(
            ["bash", str(script), "sql", "WITH x AS (SELECT 1) DELETE FROM meta"],
            env=runenv,
            capture_output=True,
        ).returncode
        != 0
    )


def test_maintenance_isolated(env: Path, monkeypatch: pytest.MonkeyPatch):
    from team_harness import deployment
    from team_harness import docs_migrate
    from team_harness import loss_report
    from team_harness import workspace_archive

    called = []

    def fail():
        raise RuntimeError("failure")

    monkeypatch.setattr(state_index, "update", fail)
    monkeypatch.setattr(deployment, "auto_sync", lambda: called.append("deployment"))
    monkeypatch.setattr(loss_report, "daily", lambda now: called.append("report"))
    monkeypatch.setattr(
        workspace_archive, "archive_daily", lambda now: called.append("archive")
    )
    monkeypatch.setattr(docs_migrate, "watch", lambda **kw: called.append("documents"))
    maintenance.run_once()
    assert len(called) == 4
    monkeypatch.setenv("TH_STATE_DB", "0")
    monkeypatch.setattr(state_index, "update", lambda: pytest.fail("disabled"))
    maintenance.run_once()
    assert len(called) == 8
