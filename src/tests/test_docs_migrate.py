from datetime import datetime
from datetime import timedelta
import json
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
    proposal = Path(json.loads((task / "attempt.json").read_text())["worktree"])
    lines = (proposal / "AGENTS.md").read_text().splitlines(keepends=True)
    (proposal / "AGENTS.md").write_text(
        "".join(lines[:100])
        + f"(경위: docs/archive/AGENTS_이관_{datetime.now():%Y%m%d}.md §이관)\n"
    )
    archive = proposal / "docs/archive" / f"AGENTS_이관_{datetime.now():%Y%m%d}.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("".join(lines[100:]))
    dm.finish(repo, task)
    assert len(git(repo, "log", "--oneline").splitlines()) == 2
    assert "문서 이관 project" in (repo / ".claude/inbox.md").read_text()


def test_missing_line_rolls_back(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = prepare(repo, monkeypatch)
    original = (repo / "AGENTS.md").read_text()
    proposal = Path(json.loads((task / "attempt.json").read_text())["worktree"])
    (proposal / "AGENTS.md").write_text("short\n")
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


def test_registered_worktrees_launch_and_commit_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktrees = [tmp_path / "worktree1", tmp_path / "worktree2"]
    for i, worktree in enumerate(worktrees):
        git(repo, "worktree", "add", "-qb", f"branch{i}", str(worktree))
    watch = tmp_path / "watch"
    watch.mkdir()
    tasks = []
    for i, owner in enumerate([repo, *worktrees]):
        task = tmp_path / f"registered{i}"
        task.mkdir()
        (task / ".th_owner").write_text(str(owner))
        tasks.append(str(task))
    (watch / "tasks.txt").write_text("\n".join(tasks))
    now = datetime.now().astimezone()
    rows = dm.manual(dm.owners(), now, dry_run=True)
    assert len(rows) == 1
    assert f"{repo} (워크트리 2개에서)" in rows[0]
    result = CliRunner().invoke(
        main, ["archive", "--docs", "--dry-run", "--owner", str(worktrees[0])]
    )
    assert result.exit_code == 0 and str(repo) in result.output
    real = subprocess.run

    def fake_launcher(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if str(cmd[0]).endswith("th-launch.sh"):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(dm.subprocess, "run", fake_launcher)
    assert len(dm.manual(dm.owners(), now, dry_run=False)) == 1
    task = next((repo / ".th_docs").glob("docs_migrate_*"))
    proposal = Path(json.loads((task / "attempt.json").read_text())["worktree"])
    lines = (proposal / "AGENTS.md").read_text().splitlines(keepends=True)
    (proposal / "AGENTS.md").write_text(
        "".join(lines[:100])
        + f"(경위: docs/archive/AGENTS_이관_{datetime.now():%Y%m%d}.md §이관)\n"
    )
    archive = proposal / "docs/archive" / f"AGENTS_이관_{now:%Y%m%d}.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("".join(lines[100:]))
    dm.finish(repo, task)
    assert len(git(repo, "log", "--oneline").splitlines()) == 2
    for worktree in worktrees:
        assert len(git(worktree, "log", "--oneline").splitlines()) == 1
        assert not (worktree / ".th_docs").exists()


def test_invalid_and_bare_repositories_are_skipped(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    rows = dm.scan(
        {bare, tmp_path / "missing"}, datetime.now(), execute=False, log=False
    )
    assert any("bare repository" in row for row in rows)
    assert any("missing" in row for row in rows)


def test_concurrent_document_edit_is_preserved(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = prepare(repo, monkeypatch)
    proposal = Path(json.loads((task / "attempt.json").read_text())["worktree"])
    lines = (proposal / "AGENTS.md").read_text().splitlines(keepends=True)
    (proposal / "AGENTS.md").write_text(
        "".join(lines[:100])
        + f"(경위: docs/archive/AGENTS_이관_{datetime.now():%Y%m%d}.md §이관)\n"
    )
    archive = proposal / "docs/archive" / f"AGENTS_이관_{datetime.now():%Y%m%d}.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("".join(lines[100:]))
    concurrent = (repo / "AGENTS.md").read_text() + "new user rule\n"
    (repo / "AGENTS.md").write_text(concurrent)
    dm.finish(repo, task)
    assert (repo / "AGENTS.md").read_text() == concurrent
    assert "changed during migration" in (task / "finished").read_text()
    assert archive.is_file()


def test_failed_legacy_attempt_does_not_restore_over_editor(repo: Path) -> None:
    task = repo / "legacy"
    task.mkdir()
    (task / "attempt.json").write_text(
        json.dumps(
            {
                "original": {"AGENTS.md": "old\n"},
                "time": datetime.now().timestamp(),
                "archive": "docs/archive/old.md",
            }
        )
    )
    (task / "FAILED").touch()
    edited = (repo / "AGENTS.md").read_text() + "keep this edit\n"
    (repo / "AGENTS.md").write_text(edited)
    dm.finish(repo, task)
    assert (repo / "AGENTS.md").read_text() == edited


def test_claude_only_migration(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "AGENTS.md").write_text("active rule\n")
    (repo / "CLAUDE.md").write_text("".join(f"history {i}\n" for i in range(305)))
    git(repo, "add", "AGENTS.md", "CLAUDE.md")
    git(repo, "commit", "-qm", "claude reference")
    task = prepare(repo, monkeypatch)
    proposal = Path(json.loads((task / "attempt.json").read_text())["worktree"])
    lines = (proposal / "CLAUDE.md").read_text().splitlines(keepends=True)
    (proposal / "CLAUDE.md").write_text(
        "".join(lines[:100])
        + f"(경위: docs/archive/AGENTS_이관_{datetime.now():%Y%m%d}.md §이관)\n"
    )
    archive = proposal / "docs/archive" / f"AGENTS_이관_{datetime.now():%Y%m%d}.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("".join(lines[100:]))
    dm.finish(repo, task)
    assert "문서 이관 실패" not in (task / "finished").read_text()
    assert len((repo / "CLAUDE.md").read_text().splitlines()) == 101
    assert (repo / "AGENTS.md").read_text() == "active rule\n"


def test_missing_archive_reference_preserves_original(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = prepare(repo, monkeypatch)
    original = (repo / "AGENTS.md").read_text()
    proposal = Path(json.loads((task / "attempt.json").read_text())["worktree"])
    lines = original.splitlines(keepends=True)
    (proposal / "AGENTS.md").write_text("".join(lines[:100]))
    archive = proposal / "docs/archive" / f"AGENTS_이관_{datetime.now():%Y%m%d}.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("".join(lines[100:]))
    dm.finish(repo, task)
    assert "archive reference missing" in (task / "finished").read_text()
    assert (repo / "AGENTS.md").read_text() == original
    assert archive.is_file()
