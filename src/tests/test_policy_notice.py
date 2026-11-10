import hashlib
import json
from pathlib import Path

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
