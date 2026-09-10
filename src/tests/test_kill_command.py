# pyright: reportMissingParameterType=false

"""Tests for the human-facing `/kill` REPL command (cli.py).

Unlike `tools/agent_tools.py`'s coordinator-facing `kill_agent` tool, this
command is triggered directly by a person watching a worker's live output
(e.g. in a tmux window) and bypasses the "don't kill too eagerly" heuristics
entirely — there is no LLM judgment to second-guess.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from team_harness.agents.manager import AgentState
from team_harness.cli import _handle_kill_command


def _register(manager, ui, *, agent_id, proc, status="running"):
    state = AgentState(
        id=agent_id,
        agent_type="codex",
        prompt="p",
        cwd=".",
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=Path("stdout.log"),
        stderr_log=Path("stderr.log"),
    )
    state.status = status
    manager.register(state)
    return state


@pytest.mark.asyncio
async def test_kill_with_no_arg_and_no_running_agents_prints_hint(manager, ui):
    _handle_kill_command(arg="", manager=manager, ui=ui)

    assert ui.messages == ["실행 중인 worker가 없습니다."]


@pytest.mark.asyncio
async def test_kill_with_no_arg_lists_running_agent_ids(manager, ui, sleep_process):
    _register(manager, ui, agent_id="codex_abc123", proc=sleep_process)

    _handle_kill_command(arg="", manager=manager, ui=ui)

    assert ui.messages == ["사용법: /kill <agent_id>. 실행 중: codex_abc123"]
    assert sleep_process.returncode is None


@pytest.mark.asyncio
async def test_kill_with_unknown_id_reports_not_found(manager, ui):
    _handle_kill_command(arg="nope", manager=manager, ui=ui)

    assert ui.messages == ["'nope'에 해당하는 worker를 찾을 수 없습니다."]


@pytest.mark.asyncio
async def test_kill_with_ambiguous_prefix_lists_matches(manager, ui, sleep_process):
    proc2 = sleep_process
    _register(manager, ui, agent_id="codex_aaa", proc=proc2)
    # A second running state sharing the prefix but reusing the same real
    # process is fine here — the test only exercises the ambiguity branch,
    # which returns before ever touching the process.
    _register(manager, ui, agent_id="codex_aab", proc=proc2)

    _handle_kill_command(arg="codex_aa", manager=manager, ui=ui)

    assert ui.messages == [
        "'codex_aa'가 여러 worker와 일치합니다: codex_aaa, codex_aab"
    ]


@pytest.mark.asyncio
async def test_kill_exact_id_terminates_process_and_updates_state(
    manager, ui, sleep_process
):
    state = _register(manager, ui, agent_id="codex_abc123", proc=sleep_process)

    _handle_kill_command(arg="codex_abc123", manager=manager, ui=ui)

    assert state.status == "killed"
    assert state.finished_at is not None
    assert ("killed", "codex_abc123") in ui.agent_events
    assert ui.messages == ["codex_abc123 (codex)를 종료했습니다."]
    await sleep_process.wait()
    assert sleep_process.returncode is not None


@pytest.mark.asyncio
async def test_kill_by_unique_prefix_matches_full_id(manager, ui, sleep_process):
    state = _register(manager, ui, agent_id="codex_abc123", proc=sleep_process)

    _handle_kill_command(arg="codex_abc", manager=manager, ui=ui)

    assert state.status == "killed"


@pytest.mark.asyncio
async def test_kill_already_finished_agent_is_a_no_op(manager, ui, sleep_process):
    state = _register(
        manager, ui, agent_id="codex_done", proc=sleep_process, status="killed"
    )

    _handle_kill_command(arg="codex_done", manager=manager, ui=ui)

    assert ui.messages == ["codex_done는 이미 'killed' 상태입니다."]
    assert state.status == "killed"
