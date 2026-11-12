import json
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


@pytest.fixture
def deployable_repository(repository: Path) -> Path:
    import shutil

    source = Path(__file__).resolve().parents[2]
    files = [
        "design/designs/operating_policy.md",
        "design/user_config/worker_suffix.md",
        "templates/journal_rule_block.md",
        "skills/team-harness/SKILL.md",
        "skills/orca-harness-protocol/SKILL.md",
        "src/team_harness/hook_identity.py",
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
    return repository


def test_full_deployment_installs_hooks_and_reports_drift(
    deployable_repository: Path, tmp_path: Path
):
    repository = deployable_repository
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


def test_status_reports_committed_source_ahead_of_deployment(
    repository: Path, tmp_path: Path
):
    home = tmp_path / "home"
    directory = home / ".team-harness"
    directory.mkdir(parents=True)
    old = git(repository, "rev-parse", "HEAD")
    (directory / "deployment.json").write_text(
        json.dumps(
            {"source": str(repository), "revision": old, "files": {}, "worktrees": []}
        )
    )
    assert dp.status(home)["state"] == "current"
    (repository / "tracked").write_text("committed candidate\n")
    git(repository, "commit", "-qam", "candidate not deployed")
    state = dp.status(home)
    assert state["state"] == "drift"
    assert state["source_dirty"] is False
    assert state["source_matches_deployment"] is False
    assert state["source_head"] != old


def test_config_migration_does_not_rewrite_other_tables_prompt(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[unrelated]\nsystem_prompt="""Custom instructions"""\n'
        '[coordinator]\nsystem_prompt="""\n## Coordinator policy: minimize tokens\n'
        '- old managed rule\n"""\n'
    )
    dp.merge_config(path, tmp_path)
    data = tomllib.loads(path.read_text())
    assert data["unrelated"]["system_prompt"] == "Custom instructions"
    assert data["coordinator"]["system_prompt"] == ""
    assert data["coordinator"]["operating_policy_file"] == "operating_policy.md"


def test_config_table_examples_and_custom_suffix_are_preserved(tmp_path: Path):
    path = tmp_path / "config.toml"
    original_prompt = '\n[coordinator]\noperating_policy_file="example"\n[other]\n'
    path.write_text(
        '[unrelated]\nsystem_prompt="""' + original_prompt + '"""\n'
        '[coordinator] # actual configuration\nworker_suffix_file="custom.md"\n'
        'system_prompt="""\n[example]\noperating_policy_file="also an example"\n"""\n'
        "[after]\nvalue=42\n"
    )
    original = tomllib.loads(path.read_text())
    dp.merge_config(path, tmp_path)
    data = tomllib.loads(path.read_text())
    assert data["unrelated"] == original["unrelated"]
    assert (
        data["coordinator"]["system_prompt"] == original["coordinator"]["system_prompt"]
    )
    assert data["coordinator"]["worker_suffix_file"] == "custom.md"
    assert data["coordinator"]["operating_policy_file"] == "operating_policy.md"
    assert data["after"] == {"value": 42}


def test_managed_prompt_with_escaped_delimiter_does_not_change_other_settings(
    tmp_path: Path,
):
    path = tmp_path / "config.toml"
    path.write_text(r'''[coordinator]
system_prompt = """
## Coordinator policy: minimize tokens
example delimiter: \"""
- Experiment design check before long runs: keep baseline
"""
[unrelated]
value = "preserve"
''')
    original = tomllib.loads(path.read_text())
    dp.merge_config(path, tmp_path)
    updated = tomllib.loads(path.read_text())
    assert updated["unrelated"] == original["unrelated"]
    assert (
        updated["coordinator"]["system_prompt"]
        == "- Experiment design check before long runs: keep baseline"
    )


def test_document_migration_worktrees_are_excluded_from_sync_and_status(
    repository: Path, tmp_path: Path
):
    artifact = repository / ".th_docs/docs_migrate_20261111/worktree"
    git(repository, "worktree", "add", "--detach", str(artifact))
    original = git(artifact, "rev-parse", "HEAD")
    (artifact / "tracked").write_text("staged migration proposal\n")
    git(artifact, "add", "tracked")
    (repository / "tracked").write_text("new revision\n")
    git(repository, "commit", "-qam", "update")
    revision = git(repository, "rev-parse", "HEAD")
    rows = dp.sync_worktrees(repository, revision)
    row = next(row for row in rows if row["path"] == str(artifact))
    assert row["state"] == "excluded"
    assert "documentation migration artifact" in row["reason"]
    assert git(artifact, "rev-parse", "HEAD") == original
    assert (artifact / "tracked").read_text() == "staged migration proposal\n"
    home = tmp_path / "home"
    manifest = home / ".team-harness/deployment.json"
    manifest.parent.mkdir(parents=True)
    # An old manifest's blocked artifact must also be classified as excluded.
    row["state"] = "blocked"
    manifest.write_text(
        json.dumps(
            {
                "source": str(repository),
                "revision": revision,
                "files": {},
                "worktrees": rows,
            }
        )
    )
    state = dp.status(home)
    row = next(row for row in state["worktrees"] if row["path"] == str(artifact))
    assert row["state"] == "excluded"
    assert "tracked_dirty" not in row
    assert state["state"] == "current"


def test_deployment_backs_up_changed_assets_and_is_idempotent(
    deployable_repository: Path, tmp_path: Path
):
    home = tmp_path / "home"
    originals = {
        ".local/bin/ths": "#!/bin/sh\necho custom-status\n",
        ".agy/AGENTS.md": "keep agy instructions\n",
        ".claude/hooks/image_guard.py": "# old image guard\n",
        ".claude/hooks/coordinator_zone_guard.py": "# old zone guard\n",
        ".claude/CLAUDE.md": (
            "2. 다시 읽는 것은 정말 필요할 때뿐이다(파일이 바뀌었거나, 컨텍스트 압축 등으로 내용을 잊었을 때). "
            "그때도 **먼저 사용자에게 이유를 말하고 허락을 받은 뒤** 한 번만 읽는다.\n"
        ),
        ".codex/hooks.json": '{"hooks":{},"custom":"keep codex"}\n',
        ".claude/settings.json": json.dumps(
            {
                "env": {"PRIVATE_SETTING": "example-secret"},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read|Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 ~/.claude/hooks/image_guard.py",
                                },
                                {"type": "command", "command": "echo keep-user-hook"},
                            ],
                        },
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 ~/.claude/hooks/image_guard.py",
                                }
                            ],
                        },
                    ]
                },
            }
        )
        + "\n",
    }
    for relative, content in originals.items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    result = dp.apply(deployable_repository, home)
    backups = home / ".team-harness/deployment-backups"
    backed_contents = [path.read_text() for path in backups.iterdir() if path.is_file()]
    for content in originals.values():
        assert content in backed_contents
    for relative in originals:
        target = home / relative
        if relative != ".local/bin/ths":
            assert result["files"][str(target)] == dp.digest(target)
    assert originals[".claude/CLAUDE.md"] in (home / ".claude/CLAUDE.md").read_text()
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert settings["env"]["PRIVATE_SETTING"] == "example-secret"
    pretool = settings["hooks"]["PreToolUse"]
    image_entries = [
        entry
        for entry in pretool
        if any("image_guard.py" in hook["command"] for hook in entry["hooks"])
    ]
    assert [entry["matcher"] for entry in image_entries] == ["Read", "Bash"]
    assert any(
        hook["command"] == "echo keep-user-hook"
        for entry in pretool
        for hook in entry["hooks"]
    )
    snapshot = {path: path.stat().st_mtime_ns for path in backups.iterdir()}
    mtimes = {Path(path): Path(path).stat().st_mtime_ns for path in result["files"]}
    dp.apply(deployable_repository, home)
    assert {path: path.stat().st_mtime_ns for path in backups.iterdir()} == snapshot
    assert {path: path.stat().st_mtime_ns for path in mtimes} == mtimes
    assert "example-secret" not in json.dumps(dp.status(home))
    for relative in (
        ".claude/settings.json",
        ".codex/hooks.json",
        ".claude/hooks/image_guard.py",
        ".claude/hooks/coordinator_zone_guard.py",
        ".agy/AGENTS.md",
    ):
        path = home / relative
        path.write_text(path.read_text() + "\n")
        assert str(path) in dp.status(home)["drift"]


def test_replaced_symlink_is_backed_up_without_reading_its_target(tmp_path: Path):
    home = tmp_path / "home"
    target = home / "ths"
    home.mkdir()
    target.symlink_to("missing-custom-status")
    source = tmp_path / "source"
    source.write_text("installed")
    dp.install_file(source, target, home)
    backups = list((home / ".team-harness/deployment-backups").iterdir())
    assert len(backups) == 1
    assert backups[0].is_symlink()
    assert str(backups[0].readlink()) == "missing-custom-status"
    assert target.is_file() and not target.is_symlink()


def test_auto_sync_uses_only_clean_pushed_source(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    manifest = Path.home() / ".team-harness/deployment.json"
    manifest.parent.mkdir(parents=True)
    old = git(repository, "rev-parse", "HEAD")
    manifest.write_text(json.dumps({"source": str(repository), "revision": old}))
    calls = []
    monkeypatch.setattr(dp, "apply", lambda root: calls.append(root))
    dp.auto_sync()
    assert calls == []
    (repository / "tracked").write_text("new revision\n")
    git(repository, "commit", "-qam", "update")
    dp.auto_sync()
    assert calls == []
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repository / "tracked").write_text("dirty\n")
    dp.auto_sync()
    assert calls == []
    (repository / "tracked").write_text("new revision\n")
    dp.auto_sync()
    assert calls == [repository]


def test_shim_opt_in_lifecycle(
    deployable_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import sys

    home = tmp_path / "home"
    marker = home / ".team-harness/shim/enabled"
    monkeypatch.setattr(dp, "ROOT", deployable_repository)
    dp.apply(deployable_repository, home)
    assert "꺼짐" in dp.status(home)["shim"]
    assert not (home / ".team-harness/shim/bin").exists()
    for name in (".bashrc", ".profile"):
        assert (
            not (home / name).exists()
            or "team-harness shim >>>" not in (home / name).read_text()
        )
    monkeypatch.setattr(
        sys, "argv", ["deployment", "--enable-shim", "--home", str(home)]
    )
    assert dp.main() == 0
    assert marker.is_file()
    assert dp.shim_status(deployable_repository, home) == "installed"
    dp.apply(deployable_repository, home)
    assert "켜짐" in dp.status(home)["shim"]
    monkeypatch.setattr(
        sys, "argv", ["deployment", "--remove-shim", "--home", str(home)]
    )
    assert dp.main() == 0
    assert not marker.exists()
    assert "꺼짐" in dp.status(home)["shim"]
    for name in (".bashrc", ".profile"):
        assert "team-harness shim >>>" not in (home / name).read_text()
