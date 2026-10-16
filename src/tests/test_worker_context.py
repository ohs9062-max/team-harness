# pyright: reportMissingParameterType=false

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

from click.testing import CliRunner
import pytest

from team_harness import config as config_module
from team_harness.cli import main
from team_harness.config import load_config
from team_harness.tracking.models import WorkerContextReference
from team_harness.worker_context import ContextRequest
from team_harness.worker_context import decide
from team_harness.worker_context import measure_agent_context
from team_harness.worker_context import measure_codex_rollout
from team_harness.worker_context import RegistryConflictError
from team_harness.worker_context import utc_now
from team_harness.worker_context import utc_timestamp
from team_harness.worker_context import WorkerContextRegistry
from team_harness.worker_context import WorkerContextSettings
from team_harness.worker_context import write_minimal_worklog

SETTINGS = WorkerContextSettings()


@pytest.mark.parametrize(
    ("tokens", "action"),
    [(59999, "continue"), (60000, "continue"), (60001, "rollover")],
)
def test_normal_boundary_soft_cap_is_strictly_greater(tokens, action):
    decision = decide(
        ContextRequest("task/a", "codex", phase="boundary"),
        {"input_tokens": tokens, "measurement_status": "measured"},
        SETTINGS,
    )
    assert decision.action == action


@pytest.mark.parametrize(
    ("tokens", "action"),
    [(89999, "continue"), (90000, "continue"), (90001, "rollover")],
)
def test_short_follow_up_boundary_resume_cap_is_strictly_greater(tokens, action):
    decision = decide(
        ContextRequest("task/a", "codex", follow_up="short", phase="boundary"),
        {"input_tokens": tokens, "measurement_status": "measured"},
        SETTINGS,
    )
    assert decision.action == action


@pytest.mark.parametrize(
    ("task_key", "follow_up", "agent_type", "tokens", "action"),
    [
        ("task/a", "short", "codex", 90000, "resume"),
        ("task/b", "short", "codex", 90000, "fresh"),
        ("task/a", "normal", "codex", 90000, "fresh"),
        ("task/a", "short", "gemini", 90000, "fresh"),
        ("task/a", "short", "codex", 90001, "fresh"),
    ],
)
def test_dispatch_requires_same_key_short_agent_and_resume_cap(
    task_key, follow_up, agent_type, tokens, action
):
    now = utc_now()
    candidate = {
        "state": "parked",
        "task_key": "task/a",
        "agent_type": "codex",
        "lane": None,
        "stage": None,
        "session_id": "session-a",
        "input_tokens": tokens,
        "measurement_status": "measured",
        "parked_at": utc_timestamp(now),
        "last_used_at": utc_timestamp(now),
        "generation": 4,
    }
    decision = decide(
        ContextRequest(task_key, agent_type, follow_up=follow_up),
        {"candidates": [candidate]},
        SETTINGS,
        now,
    )
    assert decision.action == action


def test_unknown_never_causes_token_rollover_or_resume():
    boundary = decide(ContextRequest("task/a", "codex", phase="boundary"), {}, SETTINGS)
    dispatch = decide(
        ContextRequest("task/a", "codex", follow_up="short"),
        {
            "candidates": [
                {
                    "state": "parked",
                    "task_key": "task/a",
                    "agent_type": "codex",
                    "session_id": "unknown",
                    "input_tokens": None,
                    "measurement_status": "unknown",
                    "parked_at": utc_timestamp(),
                }
            ]
        },
        SETTINGS,
    )
    assert boundary.action == "continue"
    assert dispatch.action == "fresh"


def _register_and_park(
    registry: WorkerContextRegistry, session_id: str, task_key="task/a"
):
    registry.register_active(
        {"task_key": task_key, "session_id": session_id, "agent_type": "codex"}
    )
    registry.park(session_id)


def test_registry_cleanup_applies_ttl_and_keeps_two_newest_per_key(tmp_path):
    registry = WorkerContextRegistry(tmp_path / "registry.json")
    for session_id in ("old", "middle", "new"):
        _register_and_park(registry, session_id)
    data = registry.read()
    sessions = data["sessions"]
    assert isinstance(sessions, list)
    now = utc_now()
    for index, entry in enumerate(sessions):
        assert isinstance(entry, dict)
        entry["parked_at"] = utc_timestamp(now - timedelta(hours=23, minutes=index))
        entry["last_used_at"] = utc_timestamp(now - timedelta(minutes=index))
    registry.path.write_text(json.dumps(data), encoding="utf-8")
    assert registry.cleanup(SETTINGS, now=now) == ["new"]
    states = {entry["session_id"]: entry["state"] for entry in registry.sessions()}
    assert states == {"old": "parked", "middle": "parked", "new": "retired"}

    data = registry.read()
    sessions = data["sessions"]
    assert isinstance(sessions, list)
    sessions[0]["parked_at"] = utc_timestamp(now - timedelta(hours=24, seconds=1))
    registry.path.write_text(json.dumps(data), encoding="utf-8")
    assert registry.cleanup(SETTINGS, now=now) == ["old"]
    assert {entry["session_id"]: entry["state"] for entry in registry.sessions()}[
        "old"
    ] == "retired"


def test_registry_rejects_two_active_workers_concurrently(tmp_path):
    path = tmp_path / "registry.json"

    def register(session_id: str) -> str:
        try:
            WorkerContextRegistry(path).register_active(
                {"task_key": "task/a", "session_id": session_id, "agent_type": "codex"}
            )
        except RegistryConflictError:
            return "conflict"
        return "registered"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(register, ["one", "two"]))
    assert sorted(results) == ["conflict", "registered"]
    assert (
        len(
            [
                entry
                for entry in WorkerContextRegistry(path).sessions()
                if entry["state"] == "active"
            ]
        )
        == 1
    )


def test_codex_measurement_uses_matching_current_input_not_other_session(tmp_path):
    root = tmp_path / "sessions"
    rollout = root / "2026" / "10" / "rollout-same.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "event_msg",
                        "session_id": "other",
                        "payload": {
                            "info": {"last_token_usage": {"input_tokens": 999999}}
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "session_id": "same",
                        "payload": {
                            "call_id": "call-1",
                            "info": {"last_token_usage": {"input_tokens": 60001}},
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    measurement = measure_codex_rollout(session_id="same", sessions_root=root)
    assert measurement.input_tokens == 60001
    assert measurement.measurement_status == "measured"
    assert measurement.call_id == "call-1"
    assert measurement.source_offset is not None
    assert (
        measure_codex_rollout(session_id="absent", sessions_root=root).input_tokens
        is None
    )
    assert (
        measure_agent_context(
            agent_type="agy", session_id="anything"
        ).measurement_status
        == "estimated"
    )


def test_config_parses_worker_context_global_project_and_environment(
    tmp_path, monkeypatch
):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """[worker_context]
soft_cap_tokens = 60000
resume_max_tokens = 90000
max_parked_sessions = 2
parked_ttl_hours = 24
disabled_agents = ["agy"]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.toml")
    monkeypatch.setenv("TEAM_HARNESS_WORKER_CONTEXT_ENABLED", "false")
    settings = load_config(cwd=str(tmp_path)).worker_context
    assert settings.enabled is False
    assert settings.disabled_agents == ("agy",)
    assert settings.soft_cap_tokens == 60000
    assert settings.resume_max_tokens == 90000


def test_minimal_worklog_and_additive_manifest_context_reference(tmp_path):
    path = write_minimal_worklog(
        path=tmp_path / "WORKLOG.md",
        task_key="task/a",
        goal="recover",
        current_state="parked",
        checkpoint="abc",
        tests="not run",
        error="write failed",
        next_step="verify",
        reason="context cap",
        experiment_path="experiments/a/EXPERIMENT.md",
    )
    text = path.read_text(encoding="utf-8")
    assert "rollover reason: context cap" in text
    assert len(text.splitlines()) <= 60
    reference = WorkerContextReference(registry_path="/tmp/registry", session_id="s")
    assert reference.session_id == "s"


def test_worker_context_cli_check_is_explicit_and_never_launches(tmp_path, monkeypatch):
    task = tmp_path / "task.md"
    task.write_text(
        "---\ntask_key: task/a\nfollow_up: normal\n---\nwork\n", encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.toml")
    result = CliRunner().invoke(
        main,
        [
            "worker-ctx",
            "check",
            "--task-file",
            str(task),
            "--registry",
            str(tmp_path / "registry.json"),
            "--phase",
            "dispatch",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["action"] == "fresh"
    assert not (tmp_path / "registry.json").exists()
