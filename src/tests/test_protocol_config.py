from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

import pytest

from team_harness.agents.registry import build_command
from team_harness.config import Config
from team_harness.protocol.config import load_protocol_config
from team_harness.protocol.config import ProtocolAgentSpec
from team_harness.protocol.config import ProtocolConfig
from team_harness.protocol.mode_c import run_mode_c
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
            )
        if "MODE C — REVIEW" in prompt:
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.REVIEW.value,
                success=True,
                output_text="VERDICT: PASS\nLooks good.",
                model=model,
            )
        return AgentResult(
            agent=agent_type,
            agent_type=agent_type,
            stage="UNKNOWN",
            success=True,
            output_text="ok",
            model=model,
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

    with pytest.raises(ValueError, match="Unknown logical agent"):
        ProtocolAgentSpec(agent_type="invalid_agent")


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
