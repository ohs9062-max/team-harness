from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from team_harness.protocol.mode_c import run_mode_c
from team_harness.protocol.models import AgentResult
from team_harness.protocol.models import CheckStatus
from team_harness.protocol.models import ReviewVerdict
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


class MockAgentRunner:
    """Programmable in-memory AgentRunner for deterministic MODE C tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.design_output = "# Architecture Design\nPlan: create feature.py."
        self.design_success = True
        self.design_error: str | None = None

        self.implement_output = "Implemented feature.py."
        self.implement_success = True
        self.implement_error: str | None = None
        self.implement_file_to_write: tuple[str, str] | None = (
            "feature.py",
            "def answer(): return 42\n",
        )

        # Review responses can be a queue of outputs
        self.review_outputs: list[str] = ["Analysis looks solid.\nVERDICT: PASS"]
        self.review_success = True
        self.review_error: str | None = None

        # Fix responses
        self.fix_output = "Fixed issues reported by reviewer."
        self.fix_success = True
        self.fix_error: str | None = None
        self.fix_file_to_write: tuple[str, str] | None = (
            "feature.py",
            "def answer(): return 42  # fixed\n",
        )

    async def run_agent(
        self,
        *,
        agent_type: str,
        prompt: str,
        cwd: str,
        timeout_sec: int,
        model: str | None = None,
        label: str | None = None,
    ) -> AgentResult:
        call_record = {
            "agent_type": agent_type,
            "prompt": prompt,
            "cwd": cwd,
            "timeout_sec": timeout_sec,
            "model": model,
            "label": label,
        }
        self.calls.append(call_record)

        worktree_path = Path(cwd)

        if "MODE C — DESIGN" in prompt:
            if not self.design_success:
                return AgentResult(
                    agent="claude",
                    agent_type=agent_type,
                    stage=Stage.DESIGN.value,
                    success=False,
                    error_message=self.design_error or "Claude failed",
                )
            return AgentResult(
                agent="claude",
                agent_type=agent_type,
                stage=Stage.DESIGN.value,
                success=True,
                output_text=self.design_output,
            )

        if "MODE C — IMPLEMENT" in prompt:
            if not self.implement_success:
                return AgentResult(
                    agent="codex",
                    agent_type=agent_type,
                    stage=Stage.IMPLEMENT.value,
                    success=False,
                    error_message=self.implement_error or "Codex failed",
                )
            if self.implement_file_to_write:
                rel_path, content = self.implement_file_to_write
                (worktree_path / rel_path).write_text(content, encoding="utf-8")
            return AgentResult(
                agent="codex",
                agent_type=agent_type,
                stage=Stage.IMPLEMENT.value,
                success=True,
                output_text=self.implement_output,
            )

        if "MODE C — REVIEW" in prompt:
            if not self.review_success:
                return AgentResult(
                    agent="gemini",
                    agent_type=agent_type,
                    stage=Stage.REVIEW.value,
                    success=False,
                    error_message=self.review_error or "Reviewer failed",
                )
            output = (
                self.review_outputs.pop(0) if self.review_outputs else "VERDICT: PASS"
            )
            return AgentResult(
                agent="gemini",
                agent_type=agent_type,
                stage=Stage.REVIEW.value,
                success=True,
                output_text=output,
            )

        if "MODE C — FIX" in prompt:
            if not self.fix_success:
                return AgentResult(
                    agent="codex",
                    agent_type=agent_type,
                    stage=Stage.FIX.value,
                    success=False,
                    error_message=self.fix_error or "Codex fix failed",
                )
            if self.fix_file_to_write:
                rel_path, content = self.fix_file_to_write
                (worktree_path / rel_path).write_text(content, encoding="utf-8")
            return AgentResult(
                agent="codex",
                agent_type=agent_type,
                stage=Stage.FIX.value,
                success=True,
                output_text=self.fix_output,
            )

        raise ValueError(f"Unrecognized prompt in MockAgentRunner: {prompt[:80]}")


@pytest.mark.asyncio
async def test_mode_c_happy_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    run_dir = tmp_path / "run_happy"
    wt_base = tmp_path / "worktrees"
    runner = MockAgentRunner()

    state = await run_mode_c(
        task_id="TASK-HAPPY",
        user_request="Implement answer() function",
        target_repo=str(repo),
        run_dir=run_dir,
        agent_runner=runner,
        worktrees_base_dir=wt_base,
        check_commands=[[sys.executable, "-c", "print('check pass')"]],
        auto_discover_checks=False,
    )

    assert state.stage == Stage.FINAL.value
    assert state.status == StageStatus.DONE.value
    assert state.review_verdict == ReviewVerdict.PASS.value
    assert state.test_status == CheckStatus.PASS.value
    assert state.checkpoint is not None
    assert len(state.checkpoint) == 40
    assert "pipeline" in state.worktrees

    # Verify calls
    assert len(runner.calls) == 3
    claude_call, codex_call, agy_call = runner.calls
    assert claude_call["agent_type"] == "claude"
    assert codex_call["agent_type"] == "codex"
    assert (
        agy_call["agent_type"] == "antigravity"
    )  # logical gemini -> backend antigravity

    # Verify worktree cwd isolation (no call in base repo)
    for call in runner.calls:
        assert call["cwd"] != str(repo.resolve())
        assert "pipeline" in call["cwd"]


@pytest.mark.asyncio
async def test_mode_c_fix_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    run_dir = tmp_path / "run_fix"
    wt_base = tmp_path / "worktrees"
    runner = MockAgentRunner()
    # First review FIX_REQUIRED, second review PASS
    runner.review_outputs = [
        "Finding: needs comment.\nVERDICT: FIX_REQUIRED",
        "Looks good now.\nVERDICT: PASS",
    ]

    state = await run_mode_c(
        task_id="TASK-FIX",
        user_request="Add feature",
        target_repo=str(repo),
        run_dir=run_dir,
        agent_runner=runner,
        worktrees_base_dir=wt_base,
        auto_discover_checks=False,
    )

    assert state.stage == Stage.FINAL.value
    assert state.status == StageStatus.DONE.value
    assert state.review_verdict == ReviewVerdict.PASS.value
    assert state.review_cycle == 1
    assert state.checkpoint is not None

    # Expected calls: DESIGN -> IMPLEMENT -> REVIEW(1) -> FIX -> REVIEW(2)
    assert len(runner.calls) == 5
    assert runner.calls[0]["agent_type"] == "claude"
    assert runner.calls[1]["agent_type"] == "codex"
    assert runner.calls[2]["agent_type"] == "antigravity"
    assert runner.calls[3]["agent_type"] == "codex"
    assert runner.calls[4]["agent_type"] == "antigravity"


@pytest.mark.asyncio
async def test_mode_c_review_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    run_dir = tmp_path / "run_blocked"
    runner = MockAgentRunner()
    runner.review_outputs = ["Fatal security issue.\nVERDICT: BLOCKED"]

    state = await run_mode_c(
        task_id="TASK-BLOCKED",
        user_request="Add dangerous code",
        target_repo=str(repo),
        run_dir=run_dir,
        agent_runner=runner,
        auto_discover_checks=False,
    )

    assert state.stage == Stage.REVIEW.value
    assert state.status == StageStatus.BLOCKED.value
    assert state.review_verdict == ReviewVerdict.BLOCKED.value
    assert state.checkpoint is None


@pytest.mark.asyncio
async def test_mode_c_max_review_cycles_exceeded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    run_dir = tmp_path / "run_max_cycles"
    runner = MockAgentRunner()
    # Keep returning FIX_REQUIRED indefinitely
    runner.review_outputs = [
        "Issue 1.\nVERDICT: FIX_REQUIRED",
        "Issue 2.\nVERDICT: FIX_REQUIRED",
        "Issue 3.\nVERDICT: FIX_REQUIRED",
    ]

    state = await run_mode_c(
        task_id="TASK-CYCLE-LIMIT",
        user_request="Endless fixes",
        target_repo=str(repo),
        run_dir=run_dir,
        agent_runner=runner,
        max_review_cycles=2,
        auto_discover_checks=False,
    )

    assert state.stage == Stage.REVIEW.value
    assert state.status == StageStatus.BLOCKED.value
    assert "Max review cycles (2) exceeded" in (state.blocker or "")
    assert state.checkpoint is None


@pytest.mark.asyncio
async def test_mode_c_worker_failures(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # 1. Claude failure
    r1 = MockAgentRunner()
    r1.design_success = False
    r1.design_error = "Claude crashed"
    s1 = await run_mode_c(
        task_id="T-FAIL-CLAUDE",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_fc",
        agent_runner=r1,
        auto_discover_checks=False,
    )
    assert s1.stage == Stage.DESIGN.value
    assert s1.status == StageStatus.BLOCKED.value
    assert "Claude crashed" in (s1.blocker or "")

    # 2. Codex failure
    r2 = MockAgentRunner()
    r2.implement_success = False
    r2.implement_error = "Codex syntax error"
    s2 = await run_mode_c(
        task_id="T-FAIL-CODEX",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_fco",
        agent_runner=r2,
        auto_discover_checks=False,
    )
    assert s2.stage == Stage.IMPLEMENT.value
    assert s2.status == StageStatus.BLOCKED.value
    assert "Codex syntax error" in (s2.blocker or "")

    # 3. agy failure
    r3 = MockAgentRunner()
    r3.review_success = False
    r3.review_error = "agy API error"
    s3 = await run_mode_c(
        task_id="T-FAIL-AGY",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_fa",
        agent_runner=r3,
        auto_discover_checks=False,
    )
    assert s3.stage == Stage.REVIEW.value
    assert s3.status == StageStatus.BLOCKED.value
    assert "agy API error" in (s3.blocker or "")


@pytest.mark.asyncio
async def test_mode_c_output_failures(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # Claude empty output
    r1 = MockAgentRunner()
    r1.design_output = ""
    s1 = await run_mode_c(
        task_id="T-EMPTY-DESIGN",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_ed",
        agent_runner=r1,
        auto_discover_checks=False,
    )
    assert s1.stage == Stage.DESIGN.value
    assert s1.status == StageStatus.BLOCKED.value
    assert "no design output" in (s1.blocker or "")
    # Codex must NOT have been called
    assert len(r1.calls) == 1

    # agy unclear verdict
    r2 = MockAgentRunner()
    r2.review_outputs = ["Everything is great, no bugs."]  # Missing VERDICT: line
    s2 = await run_mode_c(
        task_id="T-UNCLEAR-VERDICT",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_uv",
        agent_runner=r2,
        auto_discover_checks=False,
    )
    assert s2.stage == Stage.REVIEW.value
    assert s2.status == StageStatus.BLOCKED.value
    assert "unclear verdict" in (s2.blocker or "")


@pytest.mark.asyncio
async def test_mode_c_git_safety_dirty_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # Make repo dirty
    dirty_file = repo / "uncommitted.py"
    dirty_file.write_text("# work in progress", encoding="utf-8")

    runner = MockAgentRunner()
    state = await run_mode_c(
        task_id="T-DIRTY",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_dirty",
        agent_runner=runner,
        auto_discover_checks=False,
    )

    assert state.stage == Stage.GIT_PREFLIGHT.value
    assert state.status == StageStatus.BLOCKED.value
    assert "uncommitted changes" in (state.blocker or "")
    # No workers should have been spawned
    assert len(runner.calls) == 0
    # Dirty file must remain untouched (no reset/clean/stash)
    assert dirty_file.exists()


@pytest.mark.asyncio
async def test_mode_c_git_safety_non_git(tmp_path: Path) -> None:
    non_git = tmp_path / "empty_dir"
    non_git.mkdir()

    runner = MockAgentRunner()
    state = await run_mode_c(
        task_id="T-NONGIT",
        user_request="x",
        target_repo=str(non_git),
        run_dir=tmp_path / "run_nongit",
        agent_runner=runner,
        auto_discover_checks=False,
    )

    assert state.stage == Stage.GIT_PREFLIGHT.value
    assert state.status == StageStatus.BLOCKED.value
    assert len(runner.calls) == 0


@pytest.mark.asyncio
async def test_mode_c_check_fail_blocks_final_despite_review_pass(
    tmp_path: Path,
) -> None:
    """When System CHECK fails, cannot proceed to FINAL even if agy says PASS."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    runner = MockAgentRunner()
    runner.review_outputs = ["VERDICT: PASS"]

    failing_check = [sys.executable, "-c", "import sys; sys.exit(1)"]

    state = await run_mode_c(
        task_id="T-CHECK-FAIL",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_cf",
        agent_runner=runner,
        max_review_cycles=0,  # No retry cycle allowed
        check_commands=[failing_check],
        auto_discover_checks=False,
    )

    assert state.stage == Stage.CHECK.value
    assert state.status == StageStatus.BLOCKED.value
    assert state.checkpoint is None
    assert "System CHECK failed" in (state.blocker or "")


@pytest.mark.asyncio
async def test_mode_c_check_fail_triggers_fix_cycle(tmp_path: Path) -> None:
    """When System CHECK fails but cycles remain, FIX loop is triggered even if agy says PASS."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    flag_file = tmp_path / "check_should_pass"

    # Dynamic check command: fails until flag_file exists
    check_script = (
        f"import os, sys; sys.exit(0 if os.path.exists('{flag_file}') else 1)"
    )
    dynamic_check = [sys.executable, "-c", check_script]

    runner = MockAgentRunner()
    # Reviewer says PASS both times
    runner.review_outputs = ["VERDICT: PASS", "VERDICT: PASS"]

    # When fix runs, create the flag_file so second check passes
    original_run_agent = runner.run_agent

    async def patched_run_agent(*args: Any, **kwargs: Any) -> AgentResult:
        res = await original_run_agent(*args, **kwargs)
        if "MODE C — FIX" in kwargs.get("prompt", ""):
            flag_file.write_text("ok", encoding="utf-8")
        return res

    runner.run_agent = patched_run_agent  # type: ignore[method-assign]

    state = await run_mode_c(
        task_id="T-CHECK-FIX-CYCLE",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_cf_cycle",
        agent_runner=runner,
        max_review_cycles=2,
        check_commands=[dynamic_check],
        auto_discover_checks=False,
    )

    assert state.stage == Stage.FINAL.value
    assert state.status == StageStatus.DONE.value
    assert state.review_cycle == 1
    assert state.test_status == CheckStatus.PASS.value
    assert state.checkpoint is not None


@pytest.mark.asyncio
async def test_mode_c_waived_checks_allows_final(tmp_path: Path) -> None:
    """When no checks exist, CHECK status is WAIVED, allowing normal FINAL."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    runner = MockAgentRunner()
    runner.review_outputs = ["VERDICT: PASS"]

    state = await run_mode_c(
        task_id="T-WAIVED",
        user_request="x",
        target_repo=str(repo),
        run_dir=tmp_path / "run_waived",
        agent_runner=runner,
        check_commands=None,
        auto_discover_checks=False,
    )

    assert state.stage == Stage.FINAL.value
    assert state.status == StageStatus.DONE.value
    assert state.test_status == CheckStatus.WAIVED.value
    assert state.checkpoint is not None
