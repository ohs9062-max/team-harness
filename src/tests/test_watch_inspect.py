from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from team_harness import watch_inspect
from team_harness.worker_context import ContextMeasurement
from team_harness.worker_context import WorkerContextRegistry
from team_harness.worker_context import WorkerContextSettings


def test_watcher_requests_rollover_from_current_codex_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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


@pytest.mark.parametrize(
    ("tokens", "requested"),
    [(0, False), (59999, False), (60000, False), (60001, True), (113090, True)],
)
def test_live_watcher_resolves_session_and_measures_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: int, requested: bool
) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text(
        "---\ntask_key: current/task\nfollow_up: normal\ncontext_budget: 60000\n---\n"
    )
    (task / "worker.pid").write_text(f"{os.getpid()}\n")
    (task / "codex.log").write_text(
        json.dumps({"type": "thread.started", "thread_id": "real-session"}) + "\n"
    )
    registry_path = tmp_path / "registry.json"
    registry = WorkerContextRegistry(registry_path)
    registry.register_active(
        {"task_key": "current/task", "session_id": "placeholder", "agent_type": "codex"}
    )
    (task / ".th_context.json").write_text(
        json.dumps(
            {
                "workspace": str(tmp_path),
                "registry_path": str(registry_path),
                "session_id": "placeholder",
                "agent_type": "codex",
            }
        )
    )
    rollout = tmp_path / ".codex" / "sessions" / "rollout-real-session.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": tokens}},
                },
            }
        )
        + "\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(watch_inspect, "pid_alive", lambda pid: pid == os.getpid())
    monkeypatch.setattr(
        watch_inspect, "load_worker_context_settings", lambda _: WorkerContextSettings()
    )

    watch_inspect.main(task)

    assert (task / "context_exit_requested.json").is_file() is requested
    assert (
        json.loads((task / ".th_context.json").read_text())["session_id"]
        == "real-session"
    )
    assert registry.sessions()[0]["session_id"] == "real-session"
