from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from team_harness.agents.registry import build_command
from team_harness.agents.registry import resolve_template
from team_harness.config import AgentTemplate
from team_harness.config import Config
from team_harness.protocol.config import load_protocol_config
from team_harness.protocol.config import ProtocolAgentSpec
from team_harness.protocol.config import ProtocolConfig
from team_harness.protocol.config import validate_protocol_config
from team_harness.protocol.mode_a import resume_mode_a
from team_harness.protocol.mode_a import run_mode_a
from team_harness.protocol.mode_c import run_mode_c
from team_harness.protocol.mode_c import TeamHarnessAgentRunner
from team_harness.protocol.models import AgentResult
from team_harness.protocol.models import resolve_agent_type
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus


def _init_git_repo(path: Path) -> str:
    """Initialize a git repo with an initial commit and return head hash."""
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Runner"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    readme = path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class FakeAgentRunner:
    """In-memory runner recording calls with agent_type and model."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_agent(
        self,
        *,
        agent_type: str,
        prompt: str,
        cwd: str,
        timeout_sec: int,
        model: str | None = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "agent_type": agent_type,
                "prompt": prompt,
                "cwd": cwd,
                "timeout_sec": timeout_sec,
                "model": model,
            }
        )
        if "MODE C — DESIGN" in prompt:
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.DESIGN.value,
                success=True,
                output_text="# Architecture Design\nPlan: create file.py",
                model=model,
                requested_model=model,
                effective_model=model,
            )
        if "MODE C — IMPLEMENT" in prompt:
            (Path(cwd) / "file.py").write_text("def test(): pass\n", encoding="utf-8")
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.IMPLEMENT.value,
                success=True,
                output_text="Implemented file.py",
                model=model,
                requested_model=model,
                effective_model=model,
            )
        if "MODE C — REVIEW" in prompt:
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.REVIEW.value,
                success=True,
                output_text="VERDICT: PASS\nLooks good.",
                model=model,
                requested_model=model,
                effective_model=model,
            )
        if "MODE A — INDEPENDENT_WORK" in prompt:
            (Path(cwd) / "solution.py").write_text("print('work')\n", encoding="utf-8")
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.INDEPENDENT_WORK.value,
                success=True,
                output_text="Independent work completed",
                model=model,
                requested_model=model,
                effective_model=model,
            )
        if "MODE A — CROSS_REVIEW" in prompt:
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.CROSS_REVIEW.value,
                success=True,
                output_text="Review: looks solid",
                model=model,
                requested_model=model,
                effective_model=model,
            )
        if "MODE A — RESPONSE" in prompt:
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.RESPONSE.value,
                success=True,
                output_text="ACCEPT findings and proceed",
                model=model,
                requested_model=model,
                effective_model=model,
            )
        if "MODE A — CODEX_MERGE" in prompt:
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.CODEX_MERGE.value,
                success=True,
                output_text="Merged successfully",
                model=model,
                requested_model=model,
                effective_model=model,
            )
        return AgentResult(
            agent=agent_type,
            agent_type=agent_type,
            stage="UNKNOWN",
            success=True,
            output_text="ok",
            model=model,
            requested_model=model,
            effective_model=model,
        )


# 1. env empty -> defaults maintained
def test_1_env_empty_defaults_maintained():
    cfg = load_protocol_config(environ={})
    assert cfg.mode_a_worker_1.agent_type == "codex"
    assert cfg.mode_a_worker_1.model is None
    assert cfg.mode_a_worker_2.agent_type == "antigravity"
    assert cfg.mode_a_worker_2.model is None
    assert cfg.mode_a_final.agent_type == "codex"
    assert cfg.mode_a_final.model is None

    assert cfg.mode_b_default.agent_type == "codex"
    assert cfg.mode_b_default.model is None

    assert cfg.mode_c_design.agent_type == "claude"
    assert cfg.mode_c_design.model is None
    assert cfg.mode_c_implement.agent_type == "codex"
    assert cfg.mode_c_implement.model is None
    assert cfg.mode_c_review.agent_type == "antigravity"
    assert cfg.mode_c_review.model is None


# 2. MODE C DESIGN agent override: claude -> antigravity (or agy)
def test_2_mode_c_design_agent_override():
    cfg = load_protocol_config(environ={"HARNESS_MODE_C_DESIGN_AGENT": "antigravity"})
    assert cfg.mode_c_design.agent_type == "antigravity"

    cfg2 = load_protocol_config(environ={"HARNESS_MODE_C_DESIGN_AGENT": "agy"})
    assert cfg2.mode_c_design.agent_type == "antigravity"


# 3. MODE C DESIGN model override
def test_3_mode_c_design_model_override():
    cfg = load_protocol_config(
        environ={"HARNESS_MODE_C_DESIGN_MODEL": "claude-3-7-sonnet-20250219"}
    )
    assert cfg.mode_c_design.agent_type == "claude"
    assert cfg.mode_c_design.model == "claude-3-7-sonnet-20250219"


# 4. MODE C IMPLEMENT model override
def test_4_mode_c_implement_model_override():
    cfg = load_protocol_config(
        environ={"HARNESS_MODE_C_IMPLEMENT_MODEL": "gpt-5.6-sol"}
    )
    assert cfg.mode_c_implement.agent_type == "codex"
    assert cfg.mode_c_implement.model == "gpt-5.6-sol"


# 5. MODE C REVIEW agent and model override
def test_5_mode_c_review_agent_and_model_override():
    cfg = load_protocol_config(
        environ={
            "HARNESS_MODE_C_REVIEW_AGENT": "claude",
            "HARNESS_MODE_C_REVIEW_MODEL": "claude-3-5-haiku-20241022",
        }
    )
    assert cfg.mode_c_review.agent_type == "claude"
    assert cfg.mode_c_review.model == "claude-3-5-haiku-20241022"


# 6. Empty model string normalized to None, no --model '' flag passed
def test_6_empty_model_string_normalized_to_none():
    spec = ProtocolAgentSpec("claude", "")
    assert spec.model is None

    cfg = load_protocol_config(environ={"HARNESS_MODE_C_DESIGN_MODEL": ""})
    assert cfg.mode_c_design.model is None

    # Verify build_command does NOT include --model ''
    cmd = build_command(
        agent_type=spec.agent_type, prompt="hello", config=Config(), model=spec.model
    )
    assert "--model" not in cmd
    assert "''" not in cmd


# 7. MODE A worker1 override
def test_7_mode_a_worker_1_override():
    cfg = load_protocol_config(
        environ={
            "HARNESS_MODE_A_WORKER_1_AGENT": "claude",
            "HARNESS_MODE_A_WORKER_1_MODEL": "opus-4",
        }
    )
    assert cfg.mode_a_worker_1.agent_type == "claude"
    assert cfg.mode_a_worker_1.model == "opus-4"


# 8. MODE A worker2 override
def test_8_mode_a_worker_2_override():
    cfg = load_protocol_config(
        environ={
            "HARNESS_MODE_A_WORKER_2_AGENT": "codex",
            "HARNESS_MODE_A_WORKER_2_MODEL": "gpt-5",
        }
    )
    assert cfg.mode_a_worker_2.agent_type == "codex"
    assert cfg.mode_a_worker_2.model == "gpt-5"


# 9. MODE A final override
def test_9_mode_a_final_override():
    cfg = load_protocol_config(
        environ={
            "HARNESS_MODE_A_FINAL_AGENT": "antigravity",
            "HARNESS_MODE_A_FINAL_MODEL": "gemini-ultra",
        }
    )
    assert cfg.mode_a_final.agent_type == "antigravity"
    assert cfg.mode_a_final.model == "gemini-ultra"


# 10. MODE B default and runtime override
def test_10_mode_b_default_and_runtime_override():
    cfg = load_protocol_config(
        environ={
            "HARNESS_MODE_B_DEFAULT_AGENT": "gemini",
            "HARNESS_MODE_B_DEFAULT_MODEL": "flash",
        }
    )
    assert cfg.mode_b_default.agent_type == "antigravity"
    assert cfg.mode_b_default.model == "flash"

    # Runtime override
    cfg2 = load_protocol_config(
        mode_b_default="claude", environ={"HARNESS_MODE_B_DEFAULT_AGENT": "gemini"}
    )
    assert cfg2.mode_b_default.agent_type == "claude"


# 11. Runtime argument > env precedence
def test_11_runtime_argument_precedence():
    cfg = load_protocol_config(
        mode_c_design="antigravity",
        mode_c_design_model="custom-gemini",
        environ={
            "HARNESS_MODE_C_DESIGN_AGENT": "codex",
            "HARNESS_MODE_C_DESIGN_MODEL": "gpt-5",
        },
    )
    assert cfg.mode_c_design.agent_type == "antigravity"
    assert cfg.mode_c_design.model == "custom-gemini"


# 12. Invalid / unregistered agent handling
def test_12_invalid_unregistered_agent_handling():
    with pytest.raises(ValueError, match="Unknown logical agent"):
        resolve_agent_type("nonexistent_agent_xyz")

    # Empty agent is rejected by ProtocolAgentSpec
    with pytest.raises(ValueError, match="agent_type must not be empty"):
        ProtocolAgentSpec(agent_type="")

    # Unregistered agent is rejected by validate_protocol_config
    cfg = ProtocolConfig(mode_c_design=ProtocolAgentSpec(agent_type="invalid_agent"))
    with pytest.raises(ValueError, match="Unknown agent type"):
        validate_protocol_config(cfg, Config())


# 13. Fake AgentRunner receives agent_type and model accurately
@pytest.mark.asyncio
async def test_13_fake_agent_runner_receives_agent_type_and_model():
    runner = FakeAgentRunner()
    spec = ProtocolAgentSpec(agent_type="antigravity", model="gemini-3.5-pro")
    result = await runner.run_agent(
        agent_type=spec.agent_type,
        prompt="MODE C — DESIGN prompt",
        cwd="/tmp",
        timeout_sec=100,
        model=spec.model,
    )
    assert len(runner.calls) == 1
    assert runner.calls[0]["agent_type"] == "antigravity"
    assert runner.calls[0]["model"] == "gemini-3.5-pro"
    assert result.model == "gemini-3.5-pro"


# 14. MODE C test with configured DESIGN / IMPLEMENT / REVIEW agent called
@pytest.mark.asyncio
async def test_14_mode_c_with_configured_agents(tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    run_dir = tmp_path / "run"

    cfg = ProtocolConfig(
        mode_c_design=ProtocolAgentSpec("codex", "codex-model"),
        mode_c_implement=ProtocolAgentSpec("antigravity", "agy-model"),
        mode_c_review=ProtocolAgentSpec("claude", "claude-model"),
    )
    runner = FakeAgentRunner()
    state = await run_mode_c(
        task_id="task-custom-cfg",
        user_request="Build something",
        target_repo=str(repo_path),
        run_dir=str(run_dir),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )

    assert state.status == StageStatus.DONE.value
    # Calls: 1. DESIGN (codex/codex-model), 2. IMPLEMENT (antigravity/agy-model), 3. REVIEW (claude/claude-model)
    assert len(runner.calls) == 3
    assert runner.calls[0]["agent_type"] == "codex"
    assert runner.calls[0]["model"] == "codex-model"
    assert runner.calls[1]["agent_type"] == "antigravity"
    assert runner.calls[1]["model"] == "agy-model"
    assert runner.calls[2]["agent_type"] == "claude"
    assert runner.calls[2]["model"] == "claude-model"


# 15. state / event records actual agent and model
@pytest.mark.asyncio
async def test_15_state_and_events_record_actual_agent_and_model(tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    run_dir = tmp_path / "run"

    cfg = ProtocolConfig(
        mode_c_design=ProtocolAgentSpec("antigravity", "gemini-exp"),
        mode_c_implement=ProtocolAgentSpec("codex", "codex-mini"),
        mode_c_review=ProtocolAgentSpec("claude", "haiku-3.5"),
    )
    runner = FakeAgentRunner()
    state = await run_mode_c(
        task_id="task-record-test",
        user_request="Add tests",
        target_repo=str(repo_path),
        run_dir=str(run_dir),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )

    # Check handoffs
    design_handoff = next(h for h in state.handoffs if h["stage"] == Stage.DESIGN.value)
    assert design_handoff["agent"] == "antigravity"
    assert design_handoff["model"] == "gemini-exp"

    impl_handoff = next(
        h for h in state.handoffs if h["stage"] == Stage.IMPLEMENT.value
    )
    assert impl_handoff["agent"] == "codex"
    assert impl_handoff["model"] == "codex-mini"

    review_handoff = next(h for h in state.handoffs if h["stage"] == Stage.REVIEW.value)
    assert review_handoff["agent"] == "claude"
    assert review_handoff["model"] == "haiku-3.5"


# 16. Default Antigravity fresh command
def test_16_antigravity_fresh_command():
    cmd = build_command(agent_type="antigravity", prompt="do thing", config=Config())
    assert cmd == [
        "agy",
        "--dangerously-skip-permissions",
        "--print-timeout",
        "60m",
        "--print",
        "do thing",
    ]


# 17. Antigravity model override command
def test_17_antigravity_model_override_command():
    cmd = build_command(
        agent_type="antigravity",
        prompt="do thing",
        config=Config(),
        model="Gemini 3.5 Flash (High)",
    )
    assert cmd == [
        "agy",
        "--dangerously-skip-permissions",
        "--print-timeout",
        "60m",
        "--model",
        "Gemini 3.5 Flash (High)",
        "--print",
        "do thing",
    ]


# 18. Antigravity resume command
def test_18_antigravity_resume_command():
    cmd = build_command(
        agent_type="antigravity",
        prompt="continue",
        config=Config(),
        mode="resume",
        resume_session_id="conv-456",
    )
    assert cmd == [
        "agy",
        "--dangerously-skip-permissions",
        "--print-timeout",
        "60m",
        "--conversation",
        "conv-456",
        "--print",
        "continue",
    ]


# 19. Antigravity: --print does NOT immediately precede --print-timeout
def test_19_antigravity_print_does_not_precede_print_timeout():
    for mode in ["fresh", "resume"]:
        cmd = build_command(
            agent_type="antigravity",
            prompt="my prompt",
            config=Config(),
            mode=mode,
            resume_session_id="conv-1" if mode == "resume" else None,
            model="some-model",
        )
        print_idx = cmd.index("--print")
        timeout_idx = cmd.index("--print-timeout")
        # --print-timeout MUST appear BEFORE --print
        assert timeout_idx < print_idx
        # --print must be immediately followed by the prompt
        assert cmd[print_idx + 1] == "my prompt"
        # --print-timeout must be immediately followed by 60m
        assert cmd[timeout_idx + 1] == "60m"


# ===========================================================================
# Section 20 Regression Tests: Custom Agent, MODE A Lanes, Effective Model
# ===========================================================================


# 1. Config.agent_templates에 "astral" 등록 -> ProtocolAgentSpec / ProtocolConfig에서 astral 사용 가능
def test_20_1_custom_agent_astral_in_spec_and_config():
    spec = ProtocolAgentSpec(agent_type="astral", model="astral-v1")
    assert spec.agent_type == "astral"
    assert spec.model == "astral-v1"

    cfg = ProtocolConfig(
        mode_c_design=spec, mode_b_default=ProtocolAgentSpec(agent_type="astral")
    )
    assert cfg.mode_c_design.agent_type == "astral"
    assert cfg.mode_b_default.agent_type == "astral"


# 2. 실제 Team Harness config 기준 registered custom agent validation PASS
def test_20_2_registered_custom_agent_validation_pass():
    config = Config()
    config.agent_templates["astral"] = AgentTemplate(
        command=("astral-cli", "run"), default_model="astral-base"
    )
    cfg = ProtocolConfig(
        mode_c_design=ProtocolAgentSpec("astral"),
        mode_b_default=ProtocolAgentSpec("astral"),
    )
    # validate_protocol_config passes without raising ValueError
    validate_protocol_config(cfg, config)


# 3. unregistered bogus agent validation FAIL
@pytest.mark.asyncio
async def test_20_3_unregistered_bogus_agent_validation_fail(tmp_path: Path):
    config = Config()
    cfg = ProtocolConfig(mode_c_design=ProtocolAgentSpec("bogus_unregistered_xyz"))
    with pytest.raises(ValueError, match="Unknown agent type"):
        validate_protocol_config(cfg, config)

    # Also TeamHarnessAgentRunner fails validation on execution
    runner = TeamHarnessAgentRunner(config=config, log_dir=tmp_path)
    with pytest.raises(ValueError, match="Unknown agent type"):
        await runner.run_agent(
            agent_type="bogus_unregistered_xyz",
            prompt="do something",
            cwd=str(tmp_path),
            timeout_sec=10,
        )


# 4~9. MODE A Lane Architecture Tests
@pytest.fixture
def mode_a_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "mode_a_repo"
    repo.mkdir()
    _init_git_repo(repo)
    return repo


@pytest.mark.asyncio
async def test_20_4_through_9_mode_a_same_agent_different_models(
    mode_a_repo: Path, tmp_path: Path
):
    run_dir = tmp_path / "mode_a_run"
    runner = FakeAgentRunner()

    cfg = ProtocolConfig(
        mode_a_worker_1=ProtocolAgentSpec(agent_type="codex", model="model-A"),
        mode_a_worker_2=ProtocolAgentSpec(agent_type="codex", model="model-B"),
        mode_a_final=ProtocolAgentSpec(agent_type="codex", model="model-Final"),
    )

    state = await run_mode_a(
        task_id="task-lane-test",
        user_request="Implement parallel search",
        target_repo=str(mode_a_repo),
        run_dir=str(run_dir),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )

    # 4. worker_1=codex/model-A, worker_2=codex/model-B 두 worker가 서로 다른 lane으로 생성되는지
    assert "worker_1" in state.worktrees
    assert "worker_2" in state.worktrees
    assert state.worker_branches["worker_1"] != state.worker_branches["worker_2"]
    assert state.worker_branches["worker_1"] == "task/task-lane-test/worker_1"
    assert state.worker_branches["worker_2"] == "task/task-lane-test/worker_2"

    # 5. worktree key가 worker_1, worker_2인지
    assert set(state.worktrees.keys()) == {"worker_1", "worker_2"}

    # 6. checkpoint/status/tests key가 agent 이름이 아니라 lane인지
    assert set(state.checkpoints.keys()) == {"worker_1", "worker_2"}
    assert set(state.worker_status.keys()) == {"worker_1", "worker_2"}
    assert set(state.worker_tests.keys()) == {"worker_1", "worker_2"}
    assert "codex" not in state.worktrees
    assert "codex" not in state.checkpoints
    assert "codex" not in state.worker_status

    # 7. 동일 backend 두 개가 둘 다 실제 FakeRunner에 호출되는지
    indep_calls = [
        c for c in runner.calls if "MODE A — INDEPENDENT_WORK" in c["prompt"]
    ]
    assert len(indep_calls) == 2
    assert indep_calls[0]["agent_type"] == "codex"
    assert indep_calls[1]["agent_type"] == "codex"

    # 8. 각 호출 model: model-A, model-B 가 보존되는지
    assert indep_calls[0]["model"] == "model-A"
    assert indep_calls[1]["model"] == "model-B"

    # 9. Cross Review도 lane identity를 유지하는지
    assert "worker_2_reviews_worker_1" in state.cross_reviews
    assert "worker_1_reviews_worker_2" in state.cross_reviews
    cr21 = state.cross_reviews["worker_2_reviews_worker_1"]
    assert cr21["reviewer"] == "worker_2"
    assert cr21["target"] == "worker_1"
    assert cr21["reviewer_agent"] == "codex"
    assert cr21["model"] == "model-B"


# 10. SELECT_WORKER_1 -> worker_1 선택
@pytest.mark.asyncio
async def test_20_10_mode_a_select_worker_1(mode_a_repo: Path, tmp_path: Path):
    run_dir = tmp_path / "mode_a_run_1"
    runner = FakeAgentRunner()
    cfg = ProtocolConfig(
        mode_a_worker_1=ProtocolAgentSpec("codex", "model-A"),
        mode_a_worker_2=ProtocolAgentSpec("codex", "model-B"),
    )
    await run_mode_a(
        task_id="task-sel-1",
        user_request="req",
        target_repo=str(mode_a_repo),
        run_dir=str(run_dir),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )
    resumed = await resume_mode_a(
        task_id="task-sel-1",
        selection="SELECT_WORKER_1",
        user_instruction="merge worker 1",
        run_dir=str(run_dir),
        target_repo=str(mode_a_repo),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )
    assert resumed.user_selection == "SELECT_WORKER_1"
    assert resumed.status == StageStatus.DONE.value
    assert resumed.merge_status == "INTEGRATED"


# 11. SELECT_WORKER_2 -> worker_2 선택
@pytest.mark.asyncio
async def test_20_11_mode_a_select_worker_2(mode_a_repo: Path, tmp_path: Path):
    run_dir = tmp_path / "mode_a_run_2"
    runner = FakeAgentRunner()
    cfg = ProtocolConfig(
        mode_a_worker_1=ProtocolAgentSpec("codex", "model-A"),
        mode_a_worker_2=ProtocolAgentSpec("codex", "model-B"),
    )
    await run_mode_a(
        task_id="task-sel-2",
        user_request="req",
        target_repo=str(mode_a_repo),
        run_dir=str(run_dir),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )
    resumed = await resume_mode_a(
        task_id="task-sel-2",
        selection="SELECT_WORKER_2",
        user_instruction="merge worker 2",
        run_dir=str(run_dir),
        target_repo=str(mode_a_repo),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )
    assert resumed.user_selection == "SELECT_WORKER_2"
    assert resumed.status == StageStatus.DONE.value
    assert resumed.merge_status == "INTEGRATED"


# 12. legacy SELECT_CODEX / SELECT_GEMINI 유지 시 각각 worker_1 / worker_2 alias로 동작
@pytest.mark.asyncio
async def test_20_12_mode_a_legacy_selection_aliases(mode_a_repo: Path, tmp_path: Path):
    runner = FakeAgentRunner()
    cfg = ProtocolConfig(
        mode_a_worker_1=ProtocolAgentSpec("claude", "opus"),
        mode_a_worker_2=ProtocolAgentSpec("antigravity", "flash"),
    )

    # SELECT_CODEX -> maps to worker_1
    run_dir_codex = tmp_path / "mode_a_run_codex"
    await run_mode_a(
        task_id="task-legacy-1",
        user_request="req",
        target_repo=str(mode_a_repo),
        run_dir=str(run_dir_codex),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )
    resumed_codex = await resume_mode_a(
        task_id="task-legacy-1",
        selection="SELECT_CODEX",
        user_instruction="legacy codex alias",
        run_dir=str(run_dir_codex),
        target_repo=str(mode_a_repo),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )
    assert resumed_codex.user_selection == "SELECT_CODEX"
    assert resumed_codex.merge_status == "INTEGRATED"

    # SELECT_GEMINI -> maps to worker_2
    run_dir_gemini = tmp_path / "mode_a_run_gemini"
    await run_mode_a(
        task_id="task-legacy-2",
        user_request="req",
        target_repo=str(mode_a_repo),
        run_dir=str(run_dir_gemini),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )
    resumed_gemini = await resume_mode_a(
        task_id="task-legacy-2",
        selection="SELECT_GEMINI",
        user_instruction="legacy gemini alias",
        run_dir=str(run_dir_gemini),
        target_repo=str(mode_a_repo),
        agent_runner=runner,
        auto_discover_checks=False,
        protocol_config=cfg,
    )
    assert resumed_gemini.user_selection == "SELECT_GEMINI"
    assert resumed_gemini.merge_status == "INTEGRATED"


# 13. requested model 명시: requested == effective
def test_20_13_effective_model_explicit_requested():
    template = resolve_template("codex", Config())
    requested = "gpt-custom-model"
    effective = requested if requested is not None else template.default_model
    assert requested == "gpt-custom-model"
    assert effective == "gpt-custom-model"
    assert requested == effective


# 14. requested None + Template default_model 존재: effective == template default
def test_20_14_effective_model_requested_none_template_has_default():
    template = resolve_template("codex", Config())
    assert template.default_model == "gpt-5.6-sol"
    requested = None
    effective = requested if requested is not None else template.default_model
    assert requested is None
    assert effective == "gpt-5.6-sol"
    assert effective == template.default_model


# 15. Template default_model도 None: effective remains None
def test_20_15_effective_model_template_default_none():
    template = resolve_template("antigravity", Config())
    assert template.default_model is None
    requested = None
    effective = requested if requested is not None else template.default_model
    assert requested is None
    assert effective is None


# 16. handoff/event에 requested_model / effective_model이 정확히 기록되는지
@pytest.mark.asyncio
async def test_20_16_handoff_and_events_record_requested_and_effective(tmp_path: Path):
    repo = tmp_path / "repo_models"
    repo.mkdir()
    _init_git_repo(repo)
    run_dir = tmp_path / "run_models"

    class EffectiveModelFakeRunner:
        async def run_agent(
            self,
            *,
            agent_type: str,
            prompt: str,
            cwd: str,
            timeout_sec: int,
            model: str | None = None,
        ) -> AgentResult:
            eff = model or ("gpt-5.6-sol" if agent_type == "codex" else None)
            if "MODE C — IMPLEMENT" in prompt:
                (Path(cwd) / "file.py").write_text("x = 1\n", encoding="utf-8")
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage="",
                success=True,
                output_text="VERDICT: PASS\nDone",
                model=eff,
                requested_model=model,
                effective_model=eff,
            )

    cfg = ProtocolConfig(
        mode_c_design=ProtocolAgentSpec("claude", "claude-3-7-sonnet"),
        mode_c_implement=ProtocolAgentSpec("codex", None),  # requested None
        mode_c_review=ProtocolAgentSpec(
            "antigravity", None
        ),  # requested None, template None
    )

    state = await run_mode_c(
        task_id="task-models-record",
        user_request="Verify models in handoffs",
        target_repo=str(repo),
        run_dir=str(run_dir),
        agent_runner=EffectiveModelFakeRunner(),
        auto_discover_checks=False,
        protocol_config=cfg,
    )

    # Check handoffs
    design_h = next(h for h in state.handoffs if h["stage"] == Stage.DESIGN.value)
    assert design_h["requested_model"] == "claude-3-7-sonnet"
    assert design_h["effective_model"] == "claude-3-7-sonnet"
    assert design_h["model"] == "claude-3-7-sonnet"

    impl_h = next(h for h in state.handoffs if h["stage"] == Stage.IMPLEMENT.value)
    assert impl_h["requested_model"] is None
    assert impl_h["effective_model"] == "gpt-5.6-sol"
    assert impl_h["model"] == "gpt-5.6-sol"

    review_h = next(h for h in state.handoffs if h["stage"] == Stage.REVIEW.value)
    assert review_h["requested_model"] is None
    assert review_h["effective_model"] is None
    assert review_h["model"] is None

    # Check event log file
    events_file = run_dir / "protocol_events.jsonl"
    assert events_file.exists()
    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    design_started = next(e for e in events if e.get("type") == "DESIGN_STARTED")
    assert design_started["requested_model"] == "claude-3-7-sonnet"

    impl_finished = next(e for e in events if e.get("type") == "IMPLEMENT_FINISHED")
    assert impl_finished["requested_model"] is None
    assert impl_finished["effective_model"] == "gpt-5.6-sol"
