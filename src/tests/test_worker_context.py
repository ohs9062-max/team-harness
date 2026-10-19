# pyright: reportMissingParameterType=false

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import time

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
from team_harness.worker_context import parse_task_allow_paths
from team_harness.worker_context import parse_task_metadata
from team_harness.worker_context import RegistryConflictError
from team_harness.worker_context import should_prepare_handoff
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


def test_handoff_requires_rollover_or_unfinished_same_key_not_stale_documents(tmp_path):
    assert not should_prepare_handoff(
        task_key="task/current",
        sessions=[],
        terminal=False,
        context_exit_requested=False,
    )
    assert not should_prepare_handoff(
        task_key="task/current",
        sessions=[
            {"task_key": "task/current", "state": "parked", "reason": "completed"}
        ],
        terminal=False,
        context_exit_requested=False,
    )
    assert not should_prepare_handoff(
        task_key="task/current",
        sessions=[{"task_key": "task/old", "state": "parked"}],
        terminal=False,
        context_exit_requested=False,
    )
    assert should_prepare_handoff(
        task_key="task/current",
        sessions=[],
        terminal=False,
        context_exit_requested=True,
    )
    assert should_prepare_handoff(
        task_key="task/current",
        sessions=[
            {
                "task_key": "task/current",
                "state": "parked",
                "reason": "context_cap_exceeded",
            }
        ],
        terminal=False,
        context_exit_requested=False,
    )
    task = tmp_path / "task.md"
    task.write_text(
        "---\ntask_key: task/current\nallow_paths:\n  - ~/.team-harness/worker_suffix.md\n---\n",
        encoding="utf-8",
    )
    assert parse_task_allow_paths(task) == ["~/.team-harness/worker_suffix.md"]


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


def _write_rollout(path, session_id, tokens, age_days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "session_id": session_id,
                "payload": {
                    "call_id": "call-1",
                    "info": {"last_token_usage": {"input_tokens": tokens}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    old = time.time() - age_days * 24 * 3600
    os.utime(path, (old, old))


def test_codex_measurement_skips_old_rollouts_without_session_id_in_name(tmp_path):
    root = tmp_path / "sessions"
    _write_rollout(root / "2026" / "10" / "rollout-old.jsonl", "same", 70000, 3)
    measurement = measure_codex_rollout(session_id="same", sessions_root=root)
    assert measurement.input_tokens is None
    recent = root / "2026" / "10" / "rollout-recent.jsonl"
    _write_rollout(recent, "same", 71000, 0)
    measurement = measure_codex_rollout(session_id="same", sessions_root=root)
    assert measurement.input_tokens == 71000


def test_codex_measurement_includes_old_rollout_named_with_session_id(tmp_path):
    root = tmp_path / "sessions"
    _write_rollout(
        root / "2026" / "10" / "rollout-2026-10-10T00-same.jsonl", "same", 72000, 5
    )
    measurement = measure_codex_rollout(session_id="same", sessions_root=root)
    assert measurement.input_tokens == 72000


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


@pytest.mark.asyncio
async def test_run_repl_spawn_agent_parks_and_resumes_only_same_key_short_follow_up(
    tmp_path, ui
):
    import asyncio

    from team_harness.agents.manager import AgentManager
    from team_harness.agents.template import AgentTemplate
    from team_harness.agents.template import SessionCapture
    from team_harness.config import Config
    from team_harness.tools import agent_tools
    from team_harness.tracking.run_log import RunLogWriter
    from team_harness.worker_context import registry_path_for_workspace

    capture_file = tmp_path / "args.txt"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$CAPTURE_FILE"\n'
        'if [ -n "$EMIT_THREAD_ID" ]; then\n'
        '  printf \'{"type":"thread.started","thread_id":"%s"}\\n\' "$EMIT_THREAD_ID"\n'
        "fi\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    rollout = tmp_path / ".codex" / "sessions" / "2026" / "10" / "rollout-sid-101.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "session_id": "sid-101",
                "payload": {
                    "call_id": "c-1",
                    "info": {"last_token_usage": {"input_tokens": 60000}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir,
        worker_suffix="",
        agent_templates={
            "codex": AgentTemplate(
                command=(str(fake_codex), "exec"),
                shared_flags=("--json",),
                resume_prefix=("resume",),
                resume_flags=("{session_id}",),
                model_flag=None,
                session_capture=SessionCapture(
                    strategy="stream_json_event",
                    match={"type": "thread.started"},
                    field_path=("thread_id",),
                ),
            )
        },
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_ctx_1",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    bindings = agent_tools.build_agent_tool_bindings(
        manager=manager, run_log=run_log, config=config, ui=ui, allowed_types=["codex"]
    )
    spawn_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "spawn_agent"
    )
    registry_path = registry_path_for_workspace(tmp_path)

    # Default (no task_key) leaves worker_context untouched.
    untracked_id = await spawn_fn(
        type="codex",
        prompt="untracked",
        cwd=str(tmp_path),
        env={"CAPTURE_FILE": str(capture_file), "HOME": str(tmp_path)},
    )
    await asyncio.wait_for(manager.wait_one(untracked_id), timeout=2)
    assert not registry_path.exists()

    # Tracked fresh run captures provider session_id, measures rollout tokens, and parks.
    first_id = await spawn_fn(
        type="codex",
        prompt="step 1",
        cwd=str(tmp_path),
        task_key="task/a",
        env={
            "CAPTURE_FILE": str(capture_file),
            "EMIT_THREAD_ID": "sid-101",
            "HOME": str(tmp_path),
        },
    )
    await asyncio.wait_for(manager.wait_one(first_id), timeout=2)
    await manager.await_finalization_tasks(timeout_s=2.0)

    sessions = WorkerContextRegistry(registry_path).sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sid-101"
    assert sessions[0]["state"] == "parked"
    assert sessions[0]["input_tokens"] == 60000
    assert sessions[0]["measurement_status"] == "measured"

    # Same-key short follow-up resumes sid-101.
    second_id = await spawn_fn(
        type="codex",
        prompt="fix typo",
        cwd=str(tmp_path),
        task_key="task/a",
        follow_up="short",
        env={
            "CAPTURE_FILE": str(capture_file),
            "EMIT_THREAD_ID": "sid-101",
            "HOME": str(tmp_path),
        },
    )
    await asyncio.wait_for(manager.wait_one(second_id), timeout=2)
    await manager.await_finalization_tasks(timeout_s=2.0)
    assert capture_file.read_text(encoding="utf-8").splitlines()[:4] == [
        "exec",
        "resume",
        "--json",
        "sid-101",
    ]
    assert WorkerContextRegistry(registry_path).sessions()[0]["generation"] == 2

    # Normal follow-up or a different key starts fresh instead of resuming.
    third_id = await spawn_fn(
        type="codex",
        prompt="new stage",
        cwd=str(tmp_path),
        task_key="task/a",
        follow_up="normal",
        env={
            "CAPTURE_FILE": str(capture_file),
            "EMIT_THREAD_ID": "sid-102",
            "HOME": str(tmp_path),
        },
    )
    await asyncio.wait_for(manager.wait_one(third_id), timeout=2)
    await manager.await_finalization_tasks(timeout_s=2.0)
    assert capture_file.read_text(encoding="utf-8").splitlines()[:2] == [
        "exec",
        "--json",
    ]


@pytest.mark.asyncio
async def test_run_repl_spawn_agent_rejects_active_conflict_and_preserves_parked_on_failed_launch(
    tmp_path, ui
):
    import asyncio

    from team_harness.agents.manager import AgentManager
    from team_harness.agents.template import AgentTemplate
    from team_harness.agents.template import SessionCapture
    from team_harness.config import Config
    from team_harness.tools import agent_tools
    from team_harness.tracking.run_log import RunLogWriter
    from team_harness.worker_context import registry_path_for_workspace

    gate_file = tmp_path / "release"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        'printf \'{"type":"thread.started","thread_id":"sid-201"}\\n\'\n'
        'while [ ! -f "$GATE_FILE" ]; do sleep 0.02; done\n',
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    rollout = tmp_path / ".codex" / "sessions" / "2026" / "10" / "rollout-sid-201.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "session_id": "sid-201",
                "payload": {"info": {"last_token_usage": {"input_tokens": 45000}}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir,
        worker_suffix="",
        agent_templates={
            "codex": AgentTemplate(
                command=(str(fake_codex), "exec"),
                shared_flags=("--json",),
                resume_prefix=("resume",),
                resume_flags=("{session_id}",),
                model_flag=None,
                session_capture=SessionCapture(
                    strategy="stream_json_event",
                    match={"type": "thread.started"},
                    field_path=("thread_id",),
                ),
            )
        },
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_ctx_2",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    first_id = await agent_tools.spawn_agent(
        type="codex",
        prompt="hold active",
        cwd=str(tmp_path),
        task_key="task/a",
        env={"GATE_FILE": str(gate_file), "HOME": str(tmp_path)},
    )
    duplicate = await agent_tools.spawn_agent(
        type="codex",
        prompt="duplicate active",
        cwd=str(tmp_path),
        task_key="task/a",
        env={"GATE_FILE": str(gate_file), "HOME": str(tmp_path)},
    )
    assert duplicate.startswith("ERROR: worker context:")
    assert "already has an active worker" in duplicate

    gate_file.write_text("go\n", encoding="utf-8")
    await asyncio.wait_for(manager.wait_one(first_id), timeout=2)
    await manager.await_finalization_tasks(timeout_s=2.0)

    registry_path = registry_path_for_workspace(tmp_path)
    parked = WorkerContextRegistry(registry_path).sessions()
    assert len(parked) == 1
    assert parked[0]["session_id"] == "sid-201"
    assert parked[0]["state"] == "parked"
    assert parked[0]["input_tokens"] == 45000
    assert parked[0]["generation"] == 1

    # Failed successor launch preserves existing parked session intact.
    config.agent_templates["codex"] = AgentTemplate(
        command=(str(tmp_path / "missing-binary"), "exec"),
        shared_flags=("--json",),
        resume_prefix=("resume",),
        resume_flags=("{session_id}",),
        model_flag=None,
    )
    with pytest.raises(FileNotFoundError):
        await agent_tools.spawn_agent(
            type="codex",
            prompt="failed resume",
            cwd=str(tmp_path),
            task_key="task/a",
            follow_up="short",
            env={"HOME": str(tmp_path)},
        )
    after_failed_resume = WorkerContextRegistry(registry_path).sessions()
    assert len(after_failed_resume) == 1
    assert after_failed_resume[0]["session_id"] == "sid-201"
    assert after_failed_resume[0]["state"] == "parked"
    assert after_failed_resume[0]["input_tokens"] == 45000
    assert after_failed_resume[0]["generation"] == 1
    assert after_failed_resume[0]["pending_launch"] is False


def test_context_budget_boundary_conditions_and_front_matter(tmp_path):
    from team_harness.worker_context import ContextRequest
    from team_harness.worker_context import decide
    from team_harness.worker_context import parse_task_front_matter
    from team_harness.worker_context import WorkerContextError
    from team_harness.worker_context import WorkerContextSettings

    settings = WorkerContextSettings(soft_cap_tokens=60000, resume_max_tokens=90000)

    # 1. Default budget (60000)
    req_default = ContextRequest(task_key="k1", agent_type="codex", phase="boundary")
    dec_59k = decide(
        req_default, {"input_tokens": 59999, "measurement_status": "measured"}, settings
    )
    assert dec_59k.action == "continue"
    assert dec_59k.reason == "within_context_cap"

    dec_60k = decide(
        req_default, {"input_tokens": 60000, "measurement_status": "measured"}, settings
    )
    assert dec_60k.action == "continue"
    assert dec_60k.reason == "within_context_cap"

    dec_60001 = decide(
        req_default, {"input_tokens": 60001, "measurement_status": "measured"}, settings
    )
    assert dec_60001.action == "rollover"
    assert dec_60001.reason == "context_cap_exceeded"

    # 2. Custom context_budget (120000)
    req_custom = ContextRequest(
        task_key="k1", agent_type="codex", phase="boundary", context_budget=120000
    )
    dec_119k = decide(
        req_custom, {"input_tokens": 119999, "measurement_status": "measured"}, settings
    )
    assert dec_119k.action == "continue"

    dec_120k = decide(
        req_custom, {"input_tokens": 120000, "measurement_status": "measured"}, settings
    )
    assert dec_120k.action == "continue"

    dec_120001 = decide(
        req_custom, {"input_tokens": 120001, "measurement_status": "measured"}, settings
    )
    assert dec_120001.action == "rollover"

    # 3. Validation: context_budget must be positive
    with pytest.raises(WorkerContextError, match="context_budget must be positive"):
        ContextRequest(task_key="k1", agent_type="codex", context_budget=0)
    with pytest.raises(WorkerContextError, match="context_budget must be positive"):
        ContextRequest(task_key="k1", agent_type="codex", context_budget=-10)

    # 4. Front matter parsing
    task_file = tmp_path / "task.md"
    task_file.write_text(
        "---\n"
        "task_key: exp/large-run\n"
        "follow_up: short\n"
        "context_budget: 120000\n"
        "---\n"
        "Task description\n",
        encoding="utf-8",
    )
    tkey, fup = parse_task_front_matter(task_file)
    assert tkey == "exp/large-run"
    assert fup == "short"

    meta = parse_task_metadata(task_file)
    assert meta == {
        "task_key": "exp/large-run",
        "follow_up": "short",
        "context_budget": 120000,
    }


def test_three_execution_paths_same_fixture_decision(tmp_path):
    import json

    from team_harness.worker_context import ContextMeasurement
    from team_harness.worker_context import ContextRequest
    from team_harness.worker_context import decide
    from team_harness.worker_context import dispatch
    from team_harness.worker_context import WorkerContextRegistry
    from team_harness.worker_context import WorkerContextSettings

    settings = WorkerContextSettings(soft_cap_tokens=60000, resume_max_tokens=90000)
    reg_path = tmp_path / ".team-harness" / "worker_context" / "registry.json"
    registry = WorkerContextRegistry(reg_path, workspace_id=str(tmp_path))
    registry.register_active(
        {
            "task_key": "bench/t1",
            "session_id": "sid-shared-1",
            "agent_type": "codex",
            "model": "gpt-5.6-terra",
            "worktree": str(tmp_path),
        }
    )
    registry.update_measurement_and_park(
        "sid-shared-1",
        ContextMeasurement(input_tokens=45000, measurement_status="measured"),
    )

    task_file = tmp_path / "task.md"
    task_file.write_text(
        "---\ntask_key: bench/t1\nfollow_up: short\ncontext_budget: 60000\n---\n",
        encoding="utf-8",
    )

    # Path 1: CLI / shell path (th worker-ctx check)
    runner = CliRunner()
    cli_result = runner.invoke(
        main,
        [
            "worker-ctx",
            "check",
            "--workspace",
            str(tmp_path),
            "--task-file",
            str(task_file),
            "--phase",
            "dispatch",
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output
    cli_decision = json.loads(cli_result.output.strip())
    assert cli_decision["action"] == "resume"
    assert cli_decision["session_id"] == "sid-shared-1"

    # Path 2: run/repl path (dispatch)
    r_req = ContextRequest(
        task_key="bench/t1",
        agent_type="codex",
        follow_up="short",
        phase="dispatch",
        context_budget=60000,
    )
    r_dec, _ = dispatch(workspace=tmp_path, request=r_req, settings=settings)
    assert r_dec.action == "resume"
    assert r_dec.session_id == "sid-shared-1"

    # Path 3: protocol path (decide with registry snapshot)
    p_req = ContextRequest(
        task_key="bench/t1",
        agent_type="codex",
        follow_up="short",
        phase="dispatch",
        context_budget=60000,
    )
    p_dec = decide(
        p_req, snapshot={"candidates": registry.sessions()}, settings=settings
    )
    assert p_dec.action == "resume"
    assert p_dec.session_id == "sid-shared-1"

    # Boundary comparison: 65k tokens with normal follow-up -> rollover across all
    req_b_normal = ContextRequest(
        task_key="bench/t1",
        agent_type="codex",
        follow_up="normal",
        phase="boundary",
        context_budget=60000,
    )
    snapshot_65k = {"input_tokens": 65000, "measurement_status": "measured"}
    dec_shared = decide(req_b_normal, snapshot_65k, settings)
    assert dec_shared.action == "rollover"
    assert dec_shared.reason == "context_cap_exceeded"
