from pathlib import Path

import pytest

from team_harness import maintenance


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
