from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

from team_harness import harness
from team_harness import workspace_archive
from team_harness.protocol import mode_a
from team_harness.protocol import mode_b
from team_harness.protocol import mode_c


class StopRun(Exception):
    pass


@pytest.mark.asyncio
async def test_run_start_calls_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    calls = []
    monkeypatch.setattr(
        workspace_archive, "archive_daily", lambda now, owner: calls.append(owner)
    )
    monkeypatch.setattr(
        harness, "load_config", lambda **kwargs: (_ for _ in ()).throw(StopRun())
    )
    with pytest.raises(StopRun):
        await harness.TeamHarness(cwd=str(tmp_path)).run("task")
    assert calls == [tmp_path]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [mode_a, mode_b, mode_c])
async def test_protocol_start_calls_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: ModuleType
):
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    calls = []
    monkeypatch.setattr(
        workspace_archive, "archive_daily", lambda now, owner: calls.append(owner)
    )
    monkeypatch.setattr(
        module, "load_protocol_config", lambda: (_ for _ in ()).throw(StopRun())
    )
    kwargs = dict(
        task_id="task", target_repo=str(tmp_path), run_dir=tmp_path, agent_runner=None
    )
    if module is not mode_b:
        kwargs["user_request"] = "task"
    with pytest.raises(StopRun):
        await getattr(module, f"run_mode_{module.__name__[-1]}")(**kwargs)
    assert calls == [tmp_path]


def test_recent_archive_does_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    now = datetime.now().astimezone()
    (watch / "archive.last").write_text(str(now.timestamp()))
    monkeypatch.setattr(
        workspace_archive,
        "archive_workspace",
        lambda *args: pytest.fail("unexpected archive"),
    )
    assert workspace_archive.archive_daily(now, tmp_path) == []


@pytest.mark.asyncio
async def test_archive_failure_logs_and_run_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    watch = tmp_path / "watch"
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))

    def fail(now: datetime, owner: Path) -> None:
        raise RuntimeError("archive error")

    monkeypatch.setattr(workspace_archive, "archive_daily", fail)
    monkeypatch.setattr(
        harness, "load_config", lambda **kwargs: (_ for _ in ()).throw(StopRun())
    )
    with pytest.raises(StopRun):
        await harness.TeamHarness(cwd=str(tmp_path)).run("task")
    assert "archive failed: archive error" in (watch / "events.log").read_text()
