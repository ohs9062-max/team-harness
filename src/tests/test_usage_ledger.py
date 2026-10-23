"""Focused, isolated coverage for the file-backed usage ledger."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from pytest import CaptureFixture
from pytest import MonkeyPatch

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
    tmp_path: Path, monkeypatch: MonkeyPatch
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
        f"2026-10-22 19:00 | 지시 | {task} | worker=codex model=m task.md={task}/task.md | {tmp_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_ledger, "orca_workspace_roots", lambda _home: set())
    rows = usage_ledger.build_attempts(watch, tmp_path)
    assert len(rows) == 1
    assert rows[0].attempt == 1 and rows[0].tokens == 1234
    assert rows[0].result == "done" and rows[0].effort == "high"


def test_journal_parses_override_and_second_launch_is_retry(
    tmp_path: Path, monkeypatch: MonkeyPatch
):
    watch = tmp_path / "watch"
    task = _task(tmp_path)
    journal = tmp_path / ".claude"
    journal.mkdir()
    (journal / "work_log.md").write_text(
        f"2026-10-22 09:00 | 지시 | {task} | worker=agy model=flash task.md={task}/task.md | {tmp_path} | override: reason\n"
        f"2026-10-22 10:00 | 지시 | {task} | worker=codex model=terra task.md={task}/task.md | {tmp_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_ledger, "orca_workspace_roots", lambda _home: set())
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
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert set(payload) == {"since", "attempts", "claude"}
    assert payload["attempts"][0]["tokens"] is None


def test_summary_allows_unknown_model(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
):
    watch = tmp_path / "watch"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    task = _task(tmp_path)
    _usage(
        watch,
        {
            "v": 1,
            "at": "2026-10-22T10:00:00Z",
            "task": str(task),
            "attempt": 1,
            "worker": "agy",
            "model": None,
            "effort": None,
            "owner": str(tmp_path),
            "override": None,
        },
    )
    assert usage_ledger.main(["--watch-dir", str(watch), "--since", "2026-10-21"]) == 0
    summary, _top_tokens = capsys.readouterr().out.split("\n토큰 상위 10개 시도", 1)
    assert "없음" not in summary


def test_worktree_journal_discovers_deleted_task_and_resolves_relative_path(
    tmp_path: Path, monkeypatch: MonkeyPatch
):
    workspace = tmp_path / "main"
    worktree = tmp_path / "worker-tree"
    task = worktree / "_outputs" / "only-in-journal"
    workspace.mkdir()
    task.mkdir(parents=True)
    (task / "STATUS").write_text("2026-10-22T01:30:00Z DONE\n", encoding="utf-8")
    (task / "codex.log").write_text("tokens used\n12,345\n", encoding="utf-8")
    journal = worktree / ".claude"
    journal.mkdir()
    (journal / "work_log_20261022.md").write_text(
        "2026-10-22 10:00 | 지시 | _outputs/only-in-journal | "
        f"worker=codex model=test task.md=_outputs/only-in-journal/task.md | {workspace}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_ledger, "worktree_roots", lambda _workspace: {worktree})
    monkeypatch.setattr(usage_ledger, "orca_workspace_roots", lambda _home: set())

    rows = usage_ledger.build_attempts(tmp_path / "watch", workspace)

    assert len(rows) == 1
    assert rows[0].task == str(task.resolve())
    assert rows[0].tokens == 12345
    assert rows[0].wall_min == 30.0


def test_ended_before_started_has_no_negative_duration(
    tmp_path: Path, monkeypatch: MonkeyPatch
):
    task = tmp_path / "task"
    task.mkdir()
    (task / "STATUS").write_text("2026-10-22T00:30:00Z DONE\n", encoding="utf-8")
    journal = tmp_path / ".claude"
    journal.mkdir()
    (journal / "work_log.md").write_text(
        f"2026-10-22 10:00 | 지시 | {task} | worker=agy model=m task.md={task}/task.md | {tmp_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_ledger, "worktree_roots", lambda _workspace: set())
    monkeypatch.setattr(usage_ledger, "orca_workspace_roots", lambda _home: set())

    row = usage_ledger.build_attempts(tmp_path / "watch", tmp_path)[0]

    assert row.wall_min is None
    assert "duration_invalid(ended_before_started)" in row.source


def test_empty_top_token_table_uses_single_not_applicable_row(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(usage_ledger, "worktree_roots", lambda _workspace: set())
    monkeypatch.setattr(usage_ledger, "orca_workspace_roots", lambda _home: set())

    assert (
        usage_ledger.main(
            ["--watch-dir", str(tmp_path / "watch"), "--since", "2026-10-21"]
        )
        == 0
    )

    top = capsys.readouterr().out.split("토큰 상위 10개 시도\n", 1)[1]
    assert "(해당 없음)" in top
    assert "없음 | 없음" not in top


def test_orca_worktree_paths_group_under_the_project_name():
    task = Path("/home/example/orca/workspaces/team-harness/worker/_outputs/task")
    assert usage_ledger.project_name(str(task.parent), task) == "team-harness"
