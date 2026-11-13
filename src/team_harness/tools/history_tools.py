"""Read-only access to the state index for coordinators."""

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
import re
import sqlite3

from team_harness.state_recall import why
from team_harness.workspace_archive import watch_dir

SCHEMA = {
    "type": "function",
    "function": {
        "name": "task_history",
        "description": "Look up a task's attempts and failures, or query the state index with read-only SQL (up to 200 rows).",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Task folder, task key, or PID",
                },
                "sql": {
                    "type": "string",
                    "description": "Read-only SELECT or WITH query",
                },
            },
        },
    },
}


def build_history_tool_bindings() -> list[tuple[dict, Callable[..., Awaitable[str]]]]:
    async def task_history(target: str = "", sql: str = "") -> str:
        def query() -> str:
            path = watch_dir() / "th.db"
            if not path.exists():
                return "상태 DB가 없습니다. th-db.sh update로 생성하십시오."
            if bool(target) == bool(sql):
                return "ERROR: target 또는 sql 중 하나를 지정하십시오."
            try:
                if target:
                    return why(target, readonly=True)
                if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
                    return "ERROR: SELECT or WITH required"
                with sqlite3.connect(
                    f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1
                ) as db:
                    db.execute("PRAGMA query_only=ON")
                    cursor = db.execute(sql)
                    if cursor.description is None:
                        return "ERROR: query must return rows"
                    lines = ["\t".join(col[0] for col in cursor.description)]
                    lines.extend(
                        "\t".join("" if v is None else str(v) for v in row)
                        for row in cursor.fetchmany(200)
                    )
                    return "\n".join(lines)
            except (sqlite3.Error, OSError, ValueError) as exc:
                return f"ERROR: {exc}"

        return await asyncio.to_thread(query)

    return [(SCHEMA, task_history)]
