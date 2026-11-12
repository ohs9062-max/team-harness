"""Human-readable explanations from the disposable state index."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from team_harness.state_index import update
from team_harness.workspace_archive import watch_dir


def connect() -> sqlite3.Connection:
    path = watch_dir() / "th.db"
    if not path.exists() or time.time() - path.stat().st_mtime > 30:
        update()
    db = sqlite3.connect(path, timeout=1)
    present = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='processes'"
    ).fetchone()
    if not present:
        db.close()
        update()
        db = sqlite3.connect(path, timeout=1)
    return db


def why(target: str, limit: int = 30) -> str:
    with connect() as db:
        if target.isdigit():
            row = db.execute(
                "SELECT task FROM processes WHERE pid=? ORDER BY last_seen DESC LIMIT 1",
                (int(target),),
            ).fetchone()
            task = row[0] if row else None
        else:
            task = str(Path(target).expanduser().resolve())
        if not task:
            return "작업을 찾지 못했습니다."
        lines = [f"작업: {task}"]
        row = db.execute(
            "SELECT state,last_step FROM tasks WHERE task=?", (task,)
        ).fetchone()
        if row:
            lines.append(f"상태: {row[0]} · 마지막 단계: {row[1] or '없음'}")
        processes = db.execute(
            "SELECT pid,ended_at,origin,session_id,workspace,worker_task FROM processes WHERE task=? ORDER BY last_seen DESC LIMIT 3",
            (task,),
        ).fetchall()
        for pid, ended, origin, session, workspace, worker in processes:
            lines.append(
                f"프로세스: {pid} {'종료' if ended else '관찰 중'} · 출처 {origin} · 세션 {session or '없음'} · 워커 {worker or '없음'} · {workspace or ''}"
            )
        for model, result, tokens in db.execute(
            "SELECT model,result,tokens FROM attempts WHERE task=? ORDER BY attempt DESC LIMIT 3",
            (task,),
        ):
            lines.append(
                f"시도: {model or '미상'} · {result or '미상'} · 토큰 {tokens if tokens is not None else '미상'}"
            )
        failure = db.execute(
            "SELECT category,detail FROM failures WHERE task=? ORDER BY at DESC LIMIT 1",
            (task,),
        ).fetchone()
        if failure:
            lines.append(f"실패: {failure[0]} · {' '.join(failure[1].splitlines())}")
            for other, detail in db.execute(
                "SELECT task,detail FROM failures WHERE category=? AND task!=? AND datetime(at)>=datetime('now','-7 days') GROUP BY task ORDER BY at DESC LIMIT 3",
                (failure[0], task),
            ):
                lines.append(f"유사 실패: {other} · {detail}")
                for (text,) in db.execute(
                    "SELECT text FROM coord_log WHERE task=? AND kind IN ('진단','결정','실패') ORDER BY at DESC LIMIT 1",
                    (other,),
                ):
                    lines.append(f"  관련 기록: {text}")
        try:
            failed_text = (Path(task) / "FAILED").read_text(errors="replace")[:1000]
            if failed_text:
                lines.append("FAILED: " + " ".join(failed_text.splitlines()))
        except OSError:
            pass
        for (text,) in db.execute(
            "SELECT text FROM events WHERE task=? OR text LIKE ? ORDER BY at DESC LIMIT 3",
            (task, f"%{Path(task).name}%"),
        ):
            lines.append(f"사건: {text}")
        for (text,) in db.execute(
            "SELECT text FROM coord_log WHERE task=? ORDER BY at DESC LIMIT 3", (task,)
        ):
            lines.append(f"코디네이터: {text}")
        for name in ("worker.log", "codex.log", "claude.log", "agy.log"):
            try:
                with (Path(task) / name).open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 65536))
                    tail = stream.read().decode(errors="replace").splitlines()[-15:]
            except OSError:
                continue
            lines.extend(
                f"로그: {line[:300]}"
                for line in tail
                if re.search(r"error|fail|exception|오류|실패", line, re.I)
            )
        return "\n".join(lines[:limit])


def main() -> None:
    if (
        os.environ.get("TH_AUTO_RECALL") == "0"
        and len(sys.argv) > 1
        and sys.argv[1] == "recent"
    ):
        return
    if len(sys.argv) > 1 and sys.argv[1] == "recent":
        workspace = str(Path(sys.argv[2]).resolve())
        with connect() as db:
            rows = db.execute(
                "SELECT text FROM coord_log WHERE workspace=? AND kind='결정' AND datetime(at)>=datetime('now','-1 day') ORDER BY at DESC LIMIT 5",
                (workspace,),
            ).fetchall()
            failures = db.execute(
                "SELECT t.task,f.category FROM failures f JOIN tasks t ON t.task=f.task WHERE t.owner=? AND datetime(f.at)>=datetime('now','-1 day') AND t.state NOT IN ('완료','DONE') ORDER BY f.at DESC LIMIT 4",
                (workspace,),
            ).fetchall()
        if rows or failures:
            print("## 미해결 실패·최근 결정")
            for task, category in failures:
                print(f"실패: {task} · {category}")
            for (text,) in rows:
                print(f"결정: {text}")
    else:
        print(why(sys.argv[1]))


if __name__ == "__main__":
    main()
