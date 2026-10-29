"""Canary quick checks and pre-push enforcement."""

import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_quick_accepts_candidate_and_rejects_broken_report(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    shutil.copytree(ROOT / "src/team_harness", candidate / "src/team_harness")
    shutil.copytree(ROOT / "src/tests/data", candidate / "src/tests/data")
    (candidate / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
    command = [str(ROOT / "scripts/th-canary.sh"), str(candidate), "--quick"]
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    report = candidate / "src/team_harness/loss_report.py"
    report.write_text(report.read_text().replace('errors="replace"', 'errors="strict"'))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "FAIL report" in result.stdout


def test_pre_push_rejects_and_records_skip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, text=True, capture_output=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Canary test")
    git("config", "user.email", "canary@example.invalid")
    (repo / "scripts").mkdir()
    (repo / "scripts/a").write_text("base")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "scripts/a").write_text("changed")
    git("commit", "-qam", "change")
    head = git("rev-parse", "HEAD")
    canary = repo / "scripts/th-canary.sh"
    canary.write_text("#!/bin/sh\nexit 1\n")
    canary.chmod(0o755)
    input_line = f"refs/heads/main {head} refs/heads/main {base}\n"
    env = dict(os.environ, TH_WATCH_DIR=str(tmp_path / "watch"))
    env.pop("TH_CANARY_SKIP", None)
    command = [str(ROOT / ".githooks/pre-push"), "origin", "url"]
    result = subprocess.run(command, cwd=repo, input=input_line, text=True, env=env)
    assert result.returncode != 0
    env["TH_CANARY_SKIP"] = "test reason"
    result = subprocess.run(command, cwd=repo, input=input_line, text=True, env=env)
    assert result.returncode == 0
    assert "test reason" in (tmp_path / "watch/events.log").read_text()
