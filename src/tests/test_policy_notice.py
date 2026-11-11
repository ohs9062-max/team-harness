import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest

from team_harness.policy_notice import notice


def test_deployment_policy_is_delivered_once_per_session_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".team-harness"
    directory.mkdir()
    policy = directory / "operating_policy.md"
    manifest = directory / "deployment.json"

    def deploy(text: str):
        policy.write_text(text)
        manifest.write_text(
            json.dumps(
                {
                    "revision": "abcdef123456",
                    "files": {str(policy): hashlib.sha256(text.encode()).hexdigest()},
                }
            )
        )

    deploy("first policy")
    workspace = tmp_path / "workspace"
    assert "first policy" in notice(workspace, {"session_id": "one"})
    assert notice(workspace, {"session_id": "one"}) == ""
    assert "first policy" in notice(workspace, {"session_id": "two"})
    deploy("second policy")
    assert "second policy" in notice(workspace, {"session_id": "one"})
    assert notice(workspace, {}) == ""
    monkeypatch.setenv("TH_ROLE", "worker")
    assert notice(workspace, {"session_id": "worker"}) == ""


def test_session_start_initializes_policy_but_resume_preserves_pending_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".team-harness"
    directory.mkdir()
    policy = directory / "operating_policy.md"
    manifest = directory / "deployment.json"

    def deploy(text: str):
        policy.write_text(text)
        manifest.write_text(
            json.dumps(
                {
                    "revision": "abcdef123456",
                    "files": {str(policy): hashlib.sha256(text.encode()).hexdigest()},
                }
            )
        )

    workspace = tmp_path / "workspace"
    payload = {"session_id": "new", "source": "startup"}
    deploy("first policy")
    assert notice(workspace, payload, session_start=True) == ""
    assert notice(workspace, payload) == ""
    deploy("updated policy")
    for source in ("resume", "compact"):
        assert (
            notice(workspace, {**payload, "source": source}, session_start=True) == ""
        )
    assert "updated policy" in notice(workspace, payload)
    assert notice(workspace, payload) == ""
    for index in range(140):
        assert (
            notice(
                workspace,
                {"session_id": str(index), "source": "startup"},
                session_start=True,
            )
            == ""
        )
    state = workspace / ".claude/.policy_versions.json"
    assert len(json.loads(state.read_text())) == 128
    assert list((workspace / ".claude").glob(".policy_version.*")) == []


def test_journal_session_start_hook_initializes_policy_cursor(tmp_path: Path):
    home = tmp_path / "home"
    directory = home / ".team-harness"
    directory.mkdir(parents=True)
    policy = directory / "operating_policy.md"
    policy.write_text("already loaded on startup")
    (directory / "deployment.json").write_text(
        json.dumps(
            {
                "revision": "abcdef123456",
                "files": {str(policy): hashlib.sha256(policy.read_bytes()).hexdigest()},
            }
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = Path(__file__).resolve().parents[2] / "scripts/journal-hook.sh"
    env = {
        **os.environ,
        "HOME": str(home),
        "TH_ROLE": "",
        "CLAUDE_PROJECT_DIR": str(workspace),
    }
    payload = {"source": "startup", "session_id": "session-start"}
    for args in ([], ["prompt"]):
        result = subprocess.run(
            [str(script), *args],
            cwd=workspace,
            env=env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "정책 업데이트" not in result.stdout
    state = json.loads((workspace / ".claude/.policy_versions.json").read_text())
    assert list(state.values()) == [hashlib.sha256(policy.read_bytes()).hexdigest()]


def test_installed_session_hook_migrates_old_matcher_and_initializes_clear(
    tmp_path: Path,
):
    home = tmp_path / "home"
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup|resume|compact",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'"{scripts / "journal-hook.sh"}"',
                                },
                                {"type": "command", "command": "echo user-hook"},
                            ],
                        }
                    ]
                }
            }
        )
    )
    directory = home / ".team-harness"
    directory.mkdir()
    policy = directory / "operating_policy.md"
    policy.write_text("current policy already loaded on clear")
    (directory / "deployment.json").write_text(
        json.dumps(
            {
                "revision": "abcdef123456",
                "files": {str(policy): hashlib.sha256(policy.read_bytes()).hexdigest()},
            }
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    env = {
        **os.environ,
        "HOME": str(home),
        "TH_ROLE": "",
        "CLAUDE_PROJECT_DIR": str(workspace),
    }
    subprocess.run(
        [str(scripts / "install-journal-rule.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    installed = json.loads(settings.read_text())["hooks"]
    session = installed["SessionStart"]
    managed = [
        (entry, hook)
        for entry in session
        for hook in entry["hooks"]
        if "journal-hook.sh" in hook["command"]
    ]
    assert len(managed) == 1
    entry, hook = managed[0]
    assert entry["matcher"] == "startup|resume|clear|compact"
    assert re.fullmatch(entry["matcher"], "clear")
    assert any(
        entry["matcher"] == "startup|resume|compact"
        and entry["hooks"] == [{"type": "command", "command": "echo user-hook"}]
        for entry in session
    )
    payload = {"source": "clear", "session_id": "cleared-session"}
    result = subprocess.run(
        shlex.split(hook["command"]),
        cwd=workspace,
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "정책 업데이트" not in result.stdout
    prompt = next(
        hook
        for entry in installed["UserPromptSubmit"]
        for hook in entry["hooks"]
        if "journal-hook.sh" in hook["command"]
    )
    result = subprocess.run(
        shlex.split(prompt["command"]),
        cwd=workspace,
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "정책 업데이트" not in result.stdout
    assert "current policy already loaded on clear" not in result.stdout
    state = json.loads((workspace / ".claude/.policy_versions.json").read_text())
    assert list(state.values()) == [hashlib.sha256(policy.read_bytes()).hexdigest()]
