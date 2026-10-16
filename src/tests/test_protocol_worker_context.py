"""Tests for TH-D32 worker context rollover wiring in Harness Protocol (stage 3c)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from team_harness.agents.template import AgentTemplate
from team_harness.config import Config
from team_harness.protocol.mode_c import TeamHarnessAgentRunner
from team_harness.protocol.models import ProtocolState
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.usage import combine_usages
from team_harness.protocol.usage import WorkerUsage
from team_harness.worker_context import ContextMeasurement
from team_harness.worker_context import ContextRequest
from team_harness.worker_context import decide
from team_harness.worker_context import WorkerContextRegistry
from team_harness.worker_context import WorkerContextSettings


def test_combine_usages_accumulates_spend_without_distorting_context():
    """Stage usage rolls up actual spend across processes without treating active context as cost."""
    u1 = WorkerUsage(
        input_tokens=50000,
        output_tokens=2000,
        cached_input_tokens=10000,
        total_tokens=52000,
        cost_usd=0.50,
        source_event_type="result",
    )
    u2 = WorkerUsage(
        input_tokens=40000,
        output_tokens=1000,
        cached_input_tokens=5000,
        total_tokens=41000,
        cost_usd=0.40,
        source_event_type="result",
    )

    combined = combine_usages([u1, u2])
    assert combined is not None
    assert combined["input_tokens"] == 90000
    assert combined["output_tokens"] == 3000
    assert combined["cached_input_tokens"] == 15000
    assert combined["total_tokens"] == 93000
    assert pytest.approx(combined["cost_usd"]) == 0.90
    assert "result,result" in str(combined.get("source_event_type"))


def test_combine_usages_empty_and_single_process_preservation():
    assert combine_usages([]) is None
    assert combine_usages([None]) is None

    single = WorkerUsage(input_tokens=1234, output_tokens=56, cost_usd=0.05)
    res = combine_usages([single])
    assert res == {"input_tokens": 1234, "output_tokens": 56, "cost_usd": 0.05}


def test_mode_a_lane_isolation_prevents_cross_lane_resume(tmp_path: Path):
    """MODE A parallel lanes use isolated keys and never resume each other's sessions."""
    settings = WorkerContextSettings(soft_cap_tokens=60000, resume_max_tokens=90000)
    registry = WorkerContextRegistry(
        tmp_path / ".team-harness" / "worker_context" / "registry.json",
        workspace_id=str(tmp_path),
    )

    # Worker 1 parks a session
    registry.register_active(
        {
            "task_key": "task-mode-a/lane-worker_1/independent_work",
            "session_id": "sid-w1-lane",
            "agent_type": "codex",
            "model": "gpt-5.6-terra",
            "worktree": str(tmp_path),
        }
    )
    registry.update_measurement_and_park(
        "sid-w1-lane",
        ContextMeasurement(input_tokens=40000, measurement_status="measured"),
    )

    # Worker 2 attempts dispatch with short follow_up under its own key
    w2_req = ContextRequest(
        task_key="task-mode-a/lane-worker_2/independent_work",
        agent_type="codex",
        follow_up="short",
        phase="dispatch",
    )
    decision = decide(
        w2_req, snapshot={"candidates": registry.sessions()}, settings=settings
    )
    # Different lane key must yield fresh, NOT resume
    assert decision.action == "fresh"
    assert decision.reason == "no_eligible_parked_session"

    # Worker 1 under same key with short follow-up resumes
    w1_req = ContextRequest(
        task_key="task-mode-a/lane-worker_1/independent_work",
        agent_type="codex",
        follow_up="short",
        phase="dispatch",
    )
    decision_w1 = decide(
        w1_req, snapshot={"candidates": registry.sessions()}, settings=settings
    )
    assert decision_w1.action == "resume"
    assert decision_w1.session_id == "sid-w1-lane"


@pytest.mark.asyncio
async def test_protocol_stage_rollover_resumes_and_records_events(tmp_path: Path):
    """Stage in TeamHarnessAgentRunner rolls over on cap exceeded when progress is made."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=worktree, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True
    )
    (worktree / "init.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "init.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "init commit"], cwd=worktree, check=True)

    fake_agent_sh = tmp_path / "fake_worker.sh"
    fake_agent_sh.write_text(
        "#!/bin/sh\n"
        'COUNT_FILE="$WORKTREE/.run_count"\n'
        'if [ ! -f "$COUNT_FILE" ]; then\n'
        '  echo 1 > "$COUNT_FILE"\n'
        '  echo "progress step 1" >> "$WORKTREE/init.txt"\n'
        '  echo \'{"type":"result","is_error":false,"usage":{"input_tokens":65000,"output_tokens":500,"total_tokens":65500,"cost_usd":0.30}}\'\n'
        "  exit 0\n"
        "else\n"
        '  echo 2 > "$COUNT_FILE"\n'
        '  echo "progress step 2" >> "$WORKTREE/init.txt"\n'
        '  echo \'{"type":"result","is_error":false,"usage":{"input_tokens":35000,"output_tokens":300,"total_tokens":35300,"cost_usd":0.15}}\'\n'
        "  exit 0\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_agent_sh.chmod(0o755)

    config = Config()
    config.worker_context = WorkerContextSettings(
        enabled=True, soft_cap_tokens=60000, resume_max_tokens=90000, disabled_agents=()
    )
    config.agent_templates["codex"] = AgentTemplate(
        command=(str(fake_agent_sh),),
        shared_flags=(),
        resume_prefix=(),
        resume_flags=(),
        model_flag=None,
    )

    state_mgr = ProtocolStateManager(tmp_path / "run")
    state = ProtocolState(task_id="task-proto-roll", mode="C")
    runner = TeamHarnessAgentRunner(
        config=config, log_dir=tmp_path / "logs", verbose=False
    )

    import os

    os.environ["WORKTREE"] = str(worktree)

    result = await runner.run_agent(
        agent_type="codex",
        prompt="do stage work",
        cwd=str(worktree),
        timeout_sec=10,
        task_key="task-proto-roll/IMPLEMENT",
        stage="IMPLEMENT",
        state_mgr=state_mgr,
        state=state,
    )

    assert result.success is True
    assert "IMPLEMENT" in state.worker_context
    ctx_info = state.worker_context["IMPLEMENT"]
    assert ctx_info["action"] == "continue"
    assert ctx_info["generation"] == 1

    # Events in protocol_events.jsonl
    events = [
        json.loads(line)
        for line in state_mgr.events_path.read_text(encoding="utf-8").splitlines()
    ]
    types = [e["type"] for e in events]
    assert "WORKER_CONTEXT_DISPATCH" in types
    assert "WORKER_CONTEXT_BOUNDARY" in types
    assert "WORKER_CONTEXT_ROLLOVER" in types

    # Usage combined from both subprocess runs
    assert result.usage is not None
    assert result.usage["input_tokens"] == 100000
    assert result.usage["output_tokens"] == 800
    assert pytest.approx(result.usage["cost_usd"]) == 0.45


@pytest.mark.asyncio
async def test_protocol_stage_stalled_progress_stops_and_records_event(tmp_path: Path):
    """If a worker exceeds context cap without any git progress, rollover halts as stalled."""
    worktree = tmp_path / "repo_stalled"
    worktree.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=worktree, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True
    )
    (worktree / "init.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "init.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "init commit"], cwd=worktree, check=True)

    fake_agent_sh = tmp_path / "fake_stalled_worker.sh"
    fake_agent_sh.write_text(
        "#!/bin/sh\n"
        'COUNT_FILE="$WORKTREE/.stalled_count"\n'
        'if [ ! -f "$COUNT_FILE" ]; then\n'
        '  echo 1 > "$COUNT_FILE"\n'
        '  echo "first progress" >> "$WORKTREE/init.txt"\n'
        '  echo \'{"type":"result","is_error":false,"usage":{"input_tokens":65000,"output_tokens":500}}\'\n'
        "  exit 0\n"
        "else\n"
        "  # Second run makes NO progress at all\n"
        '  echo \'{"type":"result","is_error":false,"usage":{"input_tokens":65000,"output_tokens":500}}\'\n'
        "  exit 0\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_agent_sh.chmod(0o755)

    config = Config()
    config.worker_context = WorkerContextSettings(
        enabled=True, soft_cap_tokens=60000, resume_max_tokens=90000, disabled_agents=()
    )
    config.agent_templates["codex"] = AgentTemplate(
        command=(str(fake_agent_sh),),
        shared_flags=(),
        resume_prefix=(),
        resume_flags=(),
        model_flag=None,
    )

    state_mgr = ProtocolStateManager(tmp_path / "run_stalled")
    state = ProtocolState(task_id="task-stalled", mode="C")
    runner = TeamHarnessAgentRunner(
        config=config, log_dir=tmp_path / "logs_stalled", verbose=False
    )

    import os

    os.environ["WORKTREE"] = str(worktree)

    result = await runner.run_agent(
        agent_type="codex",
        prompt="do work",
        cwd=str(worktree),
        timeout_sec=10,
        task_key="task-stalled/IMPLEMENT",
        stage="IMPLEMENT",
        state_mgr=state_mgr,
        state=state,
    )

    assert result.success is False
    assert "without progress" in (result.error_message or "")

    events = [
        json.loads(line)
        for line in state_mgr.events_path.read_text(encoding="utf-8").splitlines()
    ]
    types = [e["type"] for e in events]
    assert "WORKER_CONTEXT_PROGRESS_STALLED" in types
