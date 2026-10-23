"""Tests for the dependency-free STATUS step record parser."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from team_harness.step_records import parse_status_text

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_step_records_parse_final_status_and_cli_with_system_python(tmp_path: Path):
    """Step records survive LAUNCH boundaries without interpreting free text."""
    status = "\n".join(
        (
            "free-form status remains valid",
            "2026-10-23 10:00:00 START step=prepare replay=safe",
            "2026-10-23 10:01:00 END step=prepare rc=0 out=build/prepare",
            "2026-10-23 10:01:30 START step=archive replay=safe",
            "2026-10-23 10:01:45 END step=archive rc=0 out=out/archive",
            "2026-10-23 10:02:00 LAUNCH worker=codex model=test effort=low",
            "2026-10-23 10:03:00 START step=train replay=never",
            "2026-10-23 10:04:00 END step=train rc=7 out=models/latest",
            "2026-10-23 10:05:00 START step=eval",
            "2026-10-23 10:06:00 START step=prepare replay=inspect",
        )
    )
    records = parse_status_text(status)
    assert records["archive"].state == "completed"
    assert records["archive"].out == "out/archive"
    assert records["train"].state == "failed"
    assert records["train"].rc == 7
    assert records["train"].out == "models/latest"
    assert records["eval"].state == "open"
    assert records["eval"].replay == "inspect"
    assert records["prepare"].state == "open", "later START supersedes END"
    assert records["prepare"].replay == "inspect"

    task_dir = tmp_path / "step_records"
    task_dir.mkdir()
    (task_dir / "STATUS").write_text(status + "\n", encoding="utf-8")
    env = {"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": os.environ["PATH"]}
    summary = subprocess.run(
        [
            "/usr/bin/python3",
            "-m",
            "team_harness.step_records",
            "summary",
            str(task_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "완료(다시 하지 말 것): archive(out=out/archive)" in summary
    assert "실패: train(rc=7)" in summary
    assert "prepare(replay=inspect)" in summary
    assert len(summary.splitlines()) <= 20
    open_steps = subprocess.run(
        [
            "/usr/bin/python3",
            "-m",
            "team_harness.step_records",
            "open-steps",
            str(task_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert open_steps == [
        "eval\tinspect\t2026-10-23 10:05:00\t",
        "prepare\tinspect\t2026-10-23 10:06:00\t",
    ]
