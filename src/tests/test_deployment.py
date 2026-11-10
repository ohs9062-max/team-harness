from pathlib import Path
import subprocess
import tomllib

import pytest

from team_harness import deployment as dp


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "tracked").write_text("original\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


def test_worktrees_fast_forward_without_overwriting_user_changes(
    repository: Path, tmp_path: Path
):
    clean = tmp_path / "clean"
    dirty = tmp_path / "dirty"
    diverged = tmp_path / "diverged"
    for path in (clean, dirty, diverged):
        git(repository, "worktree", "add", "-qb", path.name, str(path))
    (dirty / "tracked").write_text("user edit\n")
    (diverged / "other").write_text("branch work\n")
    git(diverged, "add", ".")
    git(diverged, "commit", "-qm", "branch change")
    (repository / "tracked").write_text("new revision\n")
    git(repository, "commit", "-qam", "update")
    revision = git(repository, "rev-parse", "HEAD")
    rows = {row["path"]: row for row in dp.sync_worktrees(repository, revision)}
    assert rows[str(clean)]["state"] == "updated"
    assert git(clean, "rev-parse", "HEAD") == revision
    assert rows[str(dirty)]["state"] == "blocked"
    assert (dirty / "tracked").read_text() == "user edit\n"
    assert rows[str(diverged)]["state"] == "blocked"
    assert (diverged / "other").read_text() == "branch work\n"


def test_config_merge_preserves_credentials_provider_and_custom_prompt(tmp_path: Path):
    home = tmp_path / "home"
    path = home / ".team-harness/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[coordinator]\nprovider="custom"\napi_key="example-secret"\n'
        'system_prompt="keep my policy"\n[agents.custom]\ncommand=["custom"]\n'
    )
    dp.merge_config(path, home)
    data = tomllib.loads(path.read_text())
    assert data["coordinator"]["api_key"] == "example-secret"
    assert data["coordinator"]["provider"] == "custom"
    assert data["coordinator"]["system_prompt"] == "keep my policy"
    assert data["coordinator"]["operating_policy_file"] == "operating_policy.md"
    assert data["agents"]["custom"]["command"] == ["custom"]
    before = path.read_bytes()
    dp.merge_config(path, home)
    assert path.read_bytes() == before
    assert list((home / ".team-harness/deployment-backups").iterdir())


def test_config_merge_removes_only_recognized_managed_prompt(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[coordinator]\nsystem_prompt="""\n## Coordinator policy: minimize tokens and preserve task state\n'
        '- old managed rule\n- Experiment design check before long runs: keep baseline\n- other old rule\n"""\n'
    )
    dp.merge_config(path, tmp_path)
    config = tomllib.loads(path.read_text())
    assert (
        config["coordinator"]["system_prompt"]
        == "- Experiment design check before long runs: keep baseline"
    )


def test_unpushed_and_uncommitted_candidates_are_not_deployed(
    repository: Path, tmp_path: Path
):
    with pytest.raises(ValueError, match="push"):
        dp.apply(repository, tmp_path / "home")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repository / "tracked").write_text("uncommitted")
    with pytest.raises(ValueError, match="commit"):
        dp.apply(repository, tmp_path / "home")


def test_full_deployment_installs_hooks_and_reports_drift(
    repository: Path, tmp_path: Path
):
    import shutil

    source = Path(__file__).resolve().parents[2]
    files = [
        "design/designs/operating_policy.md",
        "design/user_config/worker_suffix.md",
        "templates/journal_rule_block.md",
        "skills/team-harness/SKILL.md",
        "skills/orca-harness-protocol/SKILL.md",
        "scripts/install-journal-rule.sh",
        "scripts/install-image-guard.sh",
        "scripts/install-coordinator-zone-guard.sh",
        "scripts/hooks/image_guard.py",
        "scripts/hooks/coordinator_zone_guard.py",
        "scripts/th-sync.sh",
        "scripts/th-status.sh",
        "scripts/journal-hook.sh",
    ]
    for relative in files:
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "deployable files")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    home = tmp_path / "home"
    result = dp.apply(repository, home)
    assert dp.status(home)["state"] == "current"
    assert result["revision"] == git(repository, "rev-parse", "HEAD")
    assert (home / ".local/bin/th-sync.sh").is_symlink()
    assert (home / ".claude/hooks/image_guard.py").is_file()
    assert (home / ".codex/hooks.json").is_file()
    assert (home / ".team-harness/config.toml").is_file()
    assert (home / ".team-harness/deployment.json").stat().st_mode & 0o777 == 0o600
    (home / ".local/bin/th-sync.sh").unlink()
    assert str(home / ".local/bin/th-sync.sh") in dp.status(home)["drift"]
    dp.apply(repository, home)
    assert dp.status(home)["state"] == "current"


def test_worktree_with_local_commits_already_contains_candidate(
    repository: Path, tmp_path: Path
):
    candidate = git(repository, "rev-parse", "HEAD")
    path = tmp_path / "ahead"
    git(repository, "worktree", "add", "-qb", "ahead", str(path))
    (path / "local").write_text("local commit")
    git(path, "add", ".")
    git(path, "commit", "-qm", "local work")
    rows = {row["path"]: row for row in dp.sync_worktrees(repository, candidate)}
    assert rows[str(path)]["state"] == "contains_revision"
    assert (path / "local").read_text() == "local commit"
