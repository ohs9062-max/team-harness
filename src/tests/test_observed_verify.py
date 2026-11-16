import json
from pathlib import Path
from typing import Any

import pytest

from team_harness import observation_events
from team_harness import observed_verify
from team_harness import state_index


def test_direct_exit_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("TH_OBSERVE", "1")
    monkeypatch.setenv("TH_ROLE", "worker")
    monkeypatch.setenv("TH_SKIP_COORD_LOG", "1")
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("# Demo\n## 검수\n```bash\nprintf x >> count\n```\n")
    (task / "DONE").write_text("done\n")
    row = {
        "pid": 42,
        "start": "1",
        "task": str(task),
        "session_id": None,
        "claude_pid": None,
        "workspace": None,
        "kind": "job",
        "origin": "user",
        "worker_task": None,
        "cmd": "codex",
        "route_key": "s",
    }
    jobs = []
    notices = []
    original_popen = observed_verify.subprocess.Popen

    def capture(args: Any, **kw: Any) -> Any:
        if args[0] == "timeout" and "team_harness.observed_verify" in args:
            jobs.append(args)
            return None
        return original_popen(args, **kw)

    monkeypatch.setattr(observed_verify.subprocess, "Popen", capture)
    monkeypatch.setattr(
        observation_events, "_append", lambda *args: notices.append(args[2])
    )
    state_index.update([row], maintenance=True)
    state_index.update([], maintenance=True)
    state_index.update([], maintenance=True)
    assert len(jobs) == 1
    args = jobs[0]
    observed_verify.run(Path(args[6]), *args[7:])
    assert (task / "count").read_text() == "x"
    assert json.loads((task / "verify_result.json").read_text())["overall"] == "PASS"
    assert "검수 PASS" in notices[0]
    state_index.update([], maintenance=True)
    assert len(jobs) == 1


@pytest.mark.parametrize(
    "case", ["registered", "no_block", "disabled", "running", "worker"]
)
def test_exclusions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.setenv("TH_OBSERVE", "1")
    monkeypatch.setenv("TH_ROLE", "worker")
    monkeypatch.setenv("TH_SKIP_COORD_LOG", "1")
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text(
        "# Task\n" + ("" if case == "no_block" else "## 검수\n```bash\ntrue\n```\n")
    )
    if case == "registered":
        (watch / "tasks.txt").write_text(str(task) + "\n")
    if case == "disabled":
        monkeypatch.setenv("TH_OBSERVED_VERIFY", "0")
    jobs = []
    notices = []
    original_popen = observed_verify.subprocess.Popen

    def capture(args: Any, **kw: Any) -> Any:
        if args[0] == "timeout" and "team_harness.observed_verify" in args:
            jobs.append(args)
            return None
        return original_popen(args, **kw)

    monkeypatch.setattr(observed_verify.subprocess, "Popen", capture)
    monkeypatch.setattr(observation_events, "_append", lambda *a: notices.append(a[2]))
    row = {
        "pid": 42,
        "start": "1",
        "task": str(task),
        "session_id": None,
        "claude_pid": None,
        "workspace": str(tmp_path),
        "kind": "job",
        "origin": "worker" if case == "worker" else "coordinator",
        "worker_task": None,
        "cmd": "codex",
        "route_key": "s",
    }
    state_index.update([row], maintenance=True)
    live = [{**row, "pid": 43}] if case == "running" else []
    state_index.update(live, maintenance=True)
    assert not jobs
    if case in {"no_block", "disabled"}:
        assert "검수 없음" in notices[0]
    if case == "running":
        assert not notices
        state_index.update([], maintenance=True)
        assert len(jobs) == 1


def test_fresh_verification_reused_for_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime
    from datetime import timedelta
    from datetime import UTC

    from team_harness import worker_digest

    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("## 검수\n```bash\nexit 1\n```\n")
    (task / "REPORT.md").write_text("report\n")
    (task / "verify_result.json").write_text(json.dumps({"overall": "PASS"}))
    ended = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    collected = []
    notices = []
    monkeypatch.setattr(
        worker_digest,
        "digest",
        lambda *args, **kwargs: collected.append(kwargs["verification_result"]),
    )
    monkeypatch.setattr(
        worker_digest,
        "verify",
        lambda *args, **kwargs: pytest.fail("fresh verification reran"),
    )
    monkeypatch.setattr(
        observation_events, "_append", lambda *args: notices.append(args[2])
    )
    monkeypatch.setattr(state_index, "update", lambda **kwargs: None)
    observed_verify.run(task, ended, str(tmp_path), "s", "finished")
    assert collected[0][0]["overall"] == "PASS"
    assert "검수 PASS" in notices[0]
