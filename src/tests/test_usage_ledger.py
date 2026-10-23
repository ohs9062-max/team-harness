"""Focused, isolated coverage for the file-backed usage ledger."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from team_harness import usage_ledger


def _task(root: Path, name: str = "attempt") -> Path:
    task = root / name
    task.mkdir()
    (task / "task.md").write_text("# task\n", encoding="utf-8")
    (task / "STATUS").write_text(
        "2026-10-22 10:00:00 LAUNCH\n2026-10-22 10:30:00 DONE\n", encoding="utf-8"
    )
    (task / "DONE").write_text("ok\n", encoding="utf-8")
    return task


def _usage(watch: Path, record: dict) -> None:
    watch.mkdir(exist_ok=True)
    (watch / "usage.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_codex_token_parsing_including_prior_attempt_and_null_for_agy(tmp_path: Path):
    task = _task(tmp_path)
    (task / "codex.log").write_text("tokens used\n144,691\n", encoding="utf-8")
    prior = task / "prev_attempt1"
    prior.mkdir()
    (prior / "codex.log").write_text("tokens used\n2,003\n", encoding="utf-8")
    assert usage_ledger.codex_tokens(task, 1) == 144691
    assert usage_ledger.codex_tokens(task, 2) == 2003
    assert usage_ledger.codex_tokens(task, 3) is None


def test_usage_and_journal_are_deduplicated_and_attempt_results_classified(
    tmp_path: Path,
):
    watch = tmp_path / "watch"
    task = _task(tmp_path)
    (task / "codex.log").write_text("tokens used\n1,234\n", encoding="utf-8")
    _usage(
        watch,
        {
            "v": 1,
            "at": "2026-10-22T10:00:00Z",
            "task": str(task),
            "attempt": 1,
            "worker": "codex",
            "model": "m",
            "effort": "high",
            "owner": str(tmp_path),
            "override": None,
        },
    )
    journal = tmp_path / ".claude"
    journal.mkdir()
    (journal / "work_log.md").write_text(
        f"2026-10-22 10:00 | 지시 | {task} | worker=codex model=m task.md={task}/task.md | {tmp_path}\n",
        encoding="utf-8",
    )
    rows = usage_ledger.build_attempts(watch, tmp_path)
    assert len(rows) == 1
    assert rows[0].attempt == 1 and rows[0].tokens == 1234
    assert rows[0].result == "done" and rows[0].effort == "high"


def test_journal_parses_override_and_second_launch_is_retry(tmp_path: Path):
    watch = tmp_path / "watch"
    task = _task(tmp_path)
    journal = tmp_path / ".claude"
    journal.mkdir()
    (journal / "work_log.md").write_text(
        f"2026-10-22 09:00 | 지시 | {task} | worker=agy model=flash task.md={task}/task.md | {tmp_path} | override: reason\n"
        f"2026-10-22 10:00 | 지시 | {task} | worker=codex model=terra task.md={task}/task.md | {tmp_path}\n",
        encoding="utf-8",
    )
    rows = usage_ledger.build_attempts(watch, tmp_path)
    assert [row.attempt for row in rows] == [2, 1]
    assert rows[1].override == "override: reason"
    assert rows[1].tokens is None and rows[1].result == "interrupted"


def test_verify_markers_take_precedence_and_claude_jsonl_aggregates(tmp_path: Path):
    task = _task(tmp_path)
    (task / "VERIFIED").write_text("ok", encoding="utf-8")
    assert usage_ledger.task_result(task) == "verified"
    home = tmp_path / "home"
    session = home / ".claude/projects/project/session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-10-22T11:00:00Z",
                "message": {
                    "usage": {
                        "input_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 5,
                        "output_tokens": 7,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    items = usage_ledger.claude_usage(home, usage_ledger.parse_time("2026-10-22"))
    assert items[0]["calls"] == 1 and items[0]["cache_creation_input_tokens"] == 5


def test_json_cli_schema_preserves_unknown_tokens(tmp_path: Path):
    watch = tmp_path / "watch"
    task = _task(tmp_path)
    _usage(
        watch,
        {
            "v": 1,
            "at": "2026-10-22T10:00:00Z",
            "task": str(task),
            "attempt": 1,
            "worker": "agy",
            "model": "flash",
            "effort": None,
            "owner": str(tmp_path),
            "override": None,
        },
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "team_harness.usage_ledger",
            "--watch-dir",
            str(watch),
            "--since",
            "2026-10-21",
            "--json",
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert set(payload) == {"since", "attempts", "claude"}
    assert payload["attempts"][0]["tokens"] is None
