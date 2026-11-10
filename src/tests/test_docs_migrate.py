from datetime import datetime
from datetime import timedelta
from pathlib import Path
import subprocess

from click.testing import CliRunner
import pytest

from team_harness import docs_migrate as dm
from team_harness.cli import main


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "AGENTS.md").write_text("".join(f"line {i}\n" for i in range(305)))
    git(root, "add", "AGENTS.md")
    git(root, "commit", "-qm", "initial")
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    return root


def test_detect_and_skips(repo: Path, tmp_path: Path) -> None:
    now = datetime.now().astimezone()
    assert dm.detect(repo, now)[1] == "eligible"
    (repo / "AGENTS.md").write_text((repo / "AGENTS.md").read_text() + "changed\n")
    assert dm.detect(repo, now)[1] == "uncommitted document changes"
    assert dm.detect(tmp_path / "snapshot", now)[1] == "copy path"
    git(repo, "restore", "AGENTS.md")
    attempt = repo / ".th_docs/docs_migrate_20260101"
    attempt.mkdir(parents=True)
    (attempt / "attempt.json").write_text('{"time": %f}' % now.timestamp())
    (attempt / "finished").touch()
    assert "24 hours" in dm.detect(repo, now + timedelta(hours=1))[1]


def prepare(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    real = subprocess.run
    monkeypatch.setattr(
        dm.subprocess,
        "run",
        lambda cmd, **kwargs: (
            Result() if str(cmd[0]).endswith("th-launch.sh") else real(cmd, **kwargs)
        ),
    )
    dm.launch(repo, datetime.now().astimezone())
    task = next((repo / ".th_docs").glob("docs_migrate_*"))
    assert (task / "task.md").is_file()
    assert ".th_docs/" in (repo / ".git/info/exclude").read_text()
    return task


def test_preserve_and_commit(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = prepare(repo, monkeypatch)
    lines = (repo / "AGENTS.md").read_text().splitlines(keepends=True)
    (repo / "AGENTS.md").write_text("".join(lines[:100]))
    archive = repo / "docs/archive" / f"AGENTS_이관_{datetime.now():%Y%m%d}.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("".join(lines[100:]))
    dm.finish(repo, task)
    assert len(git(repo, "log", "--oneline").splitlines()) == 2
    assert "문서 이관 project" in (repo / ".claude/inbox.md").read_text()


def test_missing_line_rolls_back(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = prepare(repo, monkeypatch)
    original = (repo / "AGENTS.md").read_text()
    (repo / "AGENTS.md").write_text("short\n")
    dm.finish(repo, task)
    assert (repo / "AGENTS.md").read_text() == original
    assert "문서 이관 실패" in (repo / ".claude/inbox.md").read_text()
    assert len(git(repo, "log", "--oneline").splitlines()) == 1


def test_dry_run_never_launches(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm, "owners", lambda: {repo})
    monkeypatch.setattr(dm, "launch", lambda *args: pytest.fail("launched"))
    result = CliRunner().invoke(main, ["archive", "--docs", "--dry-run"])
    assert result.exit_code == 0
    assert "AGENTS.md=305" in result.output
