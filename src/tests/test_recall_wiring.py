import asyncio
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from team_harness import harness
from team_harness import state_index
from team_harness import state_recall


def test_recall_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path))
    db_path = tmp_path / "th.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(state_index.SCHEMA)
        for name, state in [
            ("bad", "failed"),
            ("ok", "verified"),
            ("later", "verify_failed"),
        ]:
            task = str(tmp_path / name)
            db.execute(
                "INSERT INTO tasks(task,owner,task_key,state) VALUES(?,?,?,?)",
                (task, str(tmp_path), name, state),
            )
            db.execute(
                "INSERT INTO attempts(task,attempt,result) VALUES(?,1,'failed')",
                (task,),
            )
            db.execute(
                "INSERT INTO failures(task,attempt,at,category,detail) VALUES(?,1,datetime('now'),'error','cause')",
                (task,),
            )
        db.execute(
            "INSERT INTO attempts(task,attempt,result) VALUES(?,2,'verified')",
            (str(tmp_path / "later"),),
        )
        db.execute(
            "INSERT INTO processes(pid,task,last_seen) VALUES(123,?,datetime('now'))",
            (str(tmp_path / "bad"),),
        )
    monkeypatch.setattr(sys, "argv", ["recall", "recent", str(tmp_path)])
    state_recall.main()
    output = capsys.readouterr().out
    assert "/bad" in output and "/ok" not in output and "/later" not in output
    assert "cause" in state_recall.launch_recall(str(tmp_path / "bad"))
    monkeypatch.setattr(harness, "build_agent_tool_bindings", lambda **kwargs: [])
    monkeypatch.setattr(harness, "build_fs_tool_bindings", lambda **kwargs: [])
    monkeypatch.setattr(harness, "build_todo_tool_bindings", lambda **kwargs: [])
    placeholder: Any = None
    config: Any = SimpleNamespace(cwd=str(tmp_path))
    registry = harness._build_registry(
        allowed_types=[],
        manager=placeholder,
        run_log=placeholder,
        config=config,
        ui=placeholder,
        run_dir=tmp_path,
    )

    async def check():
        assert "cause" in await registry.execute("task_history", {"target": "bad"})
        assert "cause" in await registry.execute("task_history", {"target": "123"})
        assert "bad" in await registry.execute(
            "task_history", {"sql": "SELECT task_key FROM tasks"}
        )
        assert "ERROR" in await registry.execute(
            "task_history", {"sql": "DELETE FROM tasks"}
        )
        result = await registry.execute(
            "task_history",
            {
                "sql": "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<300) SELECT x FROM n"
            },
        )
        assert len(result.splitlines()) == 201
        db_path.unlink()
        assert "없습니다" in await registry.execute("task_history", {"target": "bad"})

    asyncio.run(check())


def test_worktree_normalization(tmp_path: Path) -> None:
    root = tmp_path / "main"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
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
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-q", "--detach", str(worktree)],
        check=True,
    )
    (worktree / "sub").mkdir()
    assert state_recall.normalize_workspace(str(worktree / "sub")) == str(root)
    assert state_recall.normalize_workspace(str(tmp_path)) == str(tmp_path)
