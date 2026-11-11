import json
from pathlib import Path

import pytest

from team_harness import maintenance


def test_owner_registration_is_deduplicated_and_tolerates_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.delenv("TH_NO_MAINTENANCE")
    monkeypatch.delenv("TH_ONCE", raising=False)
    monkeypatch.setattr(maintenance.subprocess, "Popen", lambda *a, **kw: None)
    owner = tmp_path / "owner"
    registry = watch / "maintenance_owners.jsonl"
    # A truncated final row must not swallow the following valid registration.
    registry.write_text('\n{broken\nnull\n42\n{}\n""\n"\\u0000"\n"truncated')
    maintenance.start(owner)
    first = registry.read_text()
    maintenance.start(owner / ".")
    assert registry.read_text() == first
    assert maintenance.registered_owners() == {owner}
    assert first.splitlines()[-1] == json.dumps(str(owner))


def test_registered_owners_uses_requested_watch_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    owner = tmp_path / "owner"
    (other / "maintenance_owners.jsonl").write_text(json.dumps(str(owner)) + "\n")
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "default"))
    assert maintenance.registered_owners(other) == {owner}
    assert maintenance.registered_owners() == set()


def test_maintenance_error_does_not_skip_other_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from team_harness import deployment
    from team_harness import docs_migrate
    from team_harness import loss_report
    from team_harness import workspace_archive

    directory = tmp_path / "watch"
    directory.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(directory))
    calls = []

    def fail():
        raise OSError("unavailable")

    monkeypatch.setattr(deployment, "auto_sync", fail)
    monkeypatch.setattr(loss_report, "daily", lambda now: calls.append("report"))
    monkeypatch.setattr(
        workspace_archive, "archive_daily", lambda now: calls.append("archive")
    )
    monkeypatch.setattr(docs_migrate, "watch", lambda **kw: calls.append("documents"))
    maintenance.run_once()
    assert calls == ["report", "archive", "documents"]
    assert "deployment failed: unavailable" in (directory / "events.log").read_text()
