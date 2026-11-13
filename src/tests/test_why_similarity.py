from pathlib import Path
import sqlite3
import subprocess

import pytest

from team_harness import proc_observer
from team_harness import state_index
from team_harness import state_recall


def test_failure_similarity_and_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    dbpath = watch / "th.db"
    verify = "- 상태: [FAIL] 실패 STATUS: END step=verify_with_git rc=1\n- verify: [FAIL] verify.sh 실패\n- hook: [SKIP] 없음\nTraceback (most recent call last):"
    details = {
        "target": verify,
        "match": verify,
        "quota": "- 상태: [FAIL] STATUS: END step=search rc=429",
        "stopped": "- 상태: [FAIL] STOPPED_BY_USER\n- verify: [FAIL] 실패",
        "lost": "- 상태: [FAIL] QUEUED\n- 사건: [FAIL] 프로세스 소실",
    }
    with sqlite3.connect(dbpath) as db:
        db.executescript(state_index.SCHEMA)
        for name, detail in details.items():
            db.execute(
                "INSERT INTO tasks(task,owner,task_key,state) VALUES (?, ?, ?, ?)",
                (str(tmp_path / name), "owner", name, "failed"),
            )
            db.execute(
                "INSERT INTO failures VALUES (?,1,datetime('now'),'검증 실패',?,'test')",
                (str(tmp_path / name), detail),
            )
    result = state_recall.why(str(tmp_path / "target"), readonly=True)
    similar = [line for line in result.splitlines() if line.startswith("유사 실패:")]
    assert len(similar) == 1 and str(tmp_path / "match") in similar[0]
    assert "Traceback" not in result and "[SKIP]" not in result
    assert all(len(line.split(" · ", 1)[-1]) <= 200 for line in similar)
    with sqlite3.connect(dbpath) as db:
        db.execute(
            "UPDATE tasks SET state='verified' WHERE task=?",
            (str(tmp_path / "target"),),
        )
    result = state_recall.why(str(tmp_path / "target"), readonly=True)
    assert "지난 실패(해결됨):" in result and "유사 실패" not in result


def test_query_argument_does_not_attribute_task(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "STATUS").write_text("running")
    child = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)", str(task)], cwd=tmp_path
    )
    try:
        rows = proc_observer.observe()
        assert not any(
            row["pid"] == child.pid and row["task"] == str(task) for row in rows
        )
    finally:
        child.terminate()
        child.wait()
