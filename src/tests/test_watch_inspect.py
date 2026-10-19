from __future__ import annotations

import json
import os
from pathlib import Path

from team_harness import watch_inspect
from team_harness.worker_context import ContextMeasurement
from team_harness.worker_context import WorkerContextSettings


def test_watcher_requests_rollover_from_current_codex_context(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text(
        "---\ntask_key: current/task\nfollow_up: normal\ncontext_budget: 60000\n---\n",
        encoding="utf-8",
    )
    (task / "worker.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (task / ".th_context.json").write_text(
        json.dumps(
            {"workspace": str(tmp_path), "session_id": "sid", "agent_type": "codex"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(watch_inspect, "pid_alive", lambda pid: pid == os.getpid())
    monkeypatch.setattr(
        watch_inspect,
        "measure_codex_rollout",
        lambda *, session_id: ContextMeasurement(60001, "measured"),
    )
    monkeypatch.setattr(
        watch_inspect,
        "load_worker_context_settings",
        lambda workspace: WorkerContextSettings(),
    )

    watch_inspect.main(task)

    payload = json.loads((task / "context_exit_requested.json").read_text())
    assert payload["reason"] == "context_cap_exceeded"
    assert payload["input_tokens"] == 60001
    assert payload["basis"] == "current_input_context_not_cumulative_tokens_used"
    assert capsys.readouterr().out.startswith("none\t")
