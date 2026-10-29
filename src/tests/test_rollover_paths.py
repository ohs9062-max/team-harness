import asyncio
from datetime import datetime
from datetime import timezone
import os
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from team_harness import rollover_paths
from team_harness.agents import spawner
from team_harness.agents.manager import AgentManager
from team_harness.agents.manager import AgentState
from team_harness.agents.template import AgentTemplate
from team_harness.config import Config
from team_harness.protocol import mode_c
from team_harness.tools import agent_tools
from team_harness.worker_context import ContextMeasurement
from team_harness.worker_context import rollover_decision


class Process:
    returncode = None


def state_for(tmp_path: Path) -> AgentState:
    return AgentState(
        id="a",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=cast(asyncio.subprocess.Process, Process()),
        spawn_time=datetime.now(timezone.utc),
        stdout_log=tmp_path / "out",
        stderr_log=tmp_path / "err",
        session_id="sid",
    )  # type: ignore[arg-type]


def boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: int
) -> tuple[rollover_paths.RolloverMonitor, AgentState]:
    monitor = rollover_paths.RolloverMonitor(tmp_path, 100, rollover_decision)
    monkeypatch.setattr(
        rollover_paths,
        "measure_agent_context",
        lambda **kwargs: ContextMeasurement(tokens, "measured"),
    )
    (tmp_path / "WORKLOG.md").write_text("progress")
    os.utime(tmp_path / "WORKLOG.md", (monitor.last_check + 1, monitor.last_check + 1))
    return monitor, state_for(tmp_path)


def test_boundary_over_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, state = boundary(tmp_path, monkeypatch, 101)
    assert monitor.check(state) == "rollover"
    assert "## 롤오버 (1/3)" in monitor.prompt("p")


def test_at_cap_keeps_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, state = boundary(tmp_path, monkeypatch, 100)
    assert monitor.check(state) == "continue"


def test_no_new_boundary_keeps_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor, state = boundary(tmp_path, monkeypatch, 101)
    monitor.last_check += 2
    assert monitor.check(state) == "continue"


def test_no_progress_only_marks_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monitor, state = boundary(tmp_path, monkeypatch, 101)
    assert monitor.check(state) == "rollover"
    os.utime(tmp_path / "WORKLOG.md", (monitor.last_check + 1, monitor.last_check + 1))
    assert monitor.check(state) == "split_needed"
    assert state.status == "running" and "작업 분할 필요" in capsys.readouterr().out


def test_rollover_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, state = boundary(tmp_path, monkeypatch, 101)
    monitor.count = 3
    assert monitor.check(state) == "split_needed"
    assert state.status == "running"


@pytest.mark.asyncio
async def test_agent_tools_relaunch_and_independent_auto_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "worker.sh"
    script.write_text('echo "FAILED: INCOMPLETE"\n')
    config = Config()
    config.agent_templates = {
        "codex": AgentTemplate(command=("sh", str(script)), model_flag=None)
    }
    result = await spawner.spawn(
        agent_id="a",
        agent_type="codex",
        prompt="p",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path,
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
    )
    state = AgentState(
        id="a",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=result.proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=tmp_path / "out",
        stderr_log=tmp_path / "err",
        template=result.template,
    )
    manager = AgentManager()
    manager.register(state)
    calls = []
    original = spawner.spawn

    async def spawn(**kwargs: Any) -> Any:
        calls.append(kwargs["prompt"])
        return await original(**kwargs)

    monkeypatch.setattr(spawner, "spawn", spawn)
    agent_tools._install_continuation(
        state,
        config=config,
        run_dir=tmp_path,
        full_prompt="p",
        extra_env={},
        model=None,
        effort=None,
        flags=None,
        agents=None,
        manager=manager,
        task_key="t",
    )
    await state.proc.wait()
    state.rollover_triggered = True
    assert state.continuation and await state.continuation(state)
    assert "롤오버" in calls[0]
    await manager.wait_one("a")
    assert len(calls) == 3 and state.continuation_exhausted


@pytest.mark.asyncio
async def test_protocol_rollover_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "worker.sh"
    script.write_text('echo "DONE: complete"\n')
    config = Config()
    config.agent_templates = {
        "codex": AgentTemplate(command=("sh", str(script)), model_flag=None)
    }
    calls = []
    original = spawner.spawn

    async def spawn(**kwargs: Any) -> Any:
        calls.append(kwargs["prompt"])
        return await original(**kwargs)

    async def wait(manager: Any, state: Any, monitor: Any, timeout: Any) -> bool:
        await state.proc.wait()
        if len(calls) == 1:
            monitor.count += 1
            return True
        return False

    monkeypatch.setattr(spawner, "spawn", spawn)
    monkeypatch.setattr(mode_c, "wait_with_rollover", wait)
    runner = mode_c.TeamHarnessAgentRunner(config=config, log_dir=tmp_path / "logs")
    result = await runner.run_agent(
        agent_type="codex", prompt="p", cwd=str(tmp_path), timeout_sec=10, task_key="t"
    )
    assert result.success and len(calls) == 2 and "롤오버" in calls[1]


def live_script(tmp_path: Path) -> Path:
    script = tmp_path / "live.sh"
    marker = tmp_path / "marker"
    worklog = tmp_path / "WORKLOG.md"
    script.write_text(f'''if [ ! -f "{marker}" ]; then
  touch "{marker}"
  sleep 0.2
  echo progress > "{worklog}"
  sleep 20
else
  echo "DONE: complete"
fi
''')
    return script


@pytest.mark.asyncio
async def test_run_live_boundary_terminates_then_respawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config.agent_templates = {
        "codex": AgentTemplate(
            command=("sh", str(live_script(tmp_path))), model_flag=None
        )
    }
    monkeypatch.setattr(
        rollover_paths,
        "measure_agent_context",
        lambda **kwargs: ContextMeasurement(60001, "measured"),
    )
    result = await spawner.spawn(
        agent_id="a",
        agent_type="codex",
        prompt="p",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path,
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
    )
    state = AgentState(
        id="a",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=result.proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=tmp_path / "out",
        stderr_log=tmp_path / "err",
        template=result.template,
        pgid=result.pgid,
    )
    manager = AgentManager()
    manager.register(state)
    killed = []
    original_kill = manager.kill

    def kill(agent_id: str) -> None:
        killed.append(agent_id)
        original_kill(agent_id)

    monkeypatch.setattr(manager, "kill", kill)
    agent_tools._install_continuation(
        state,
        config=config,
        run_dir=tmp_path,
        full_prompt="p",
        extra_env={},
        model=None,
        effort=None,
        flags=None,
        agents=None,
        manager=manager,
        task_key="t",
    )
    assert await manager.wait_one("a") == 0
    assert killed == ["a"] and state.status == "done"


@pytest.mark.asyncio
async def test_protocol_live_boundary_terminates_then_respawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config()
    config.agent_templates = {
        "codex": AgentTemplate(
            command=("sh", str(live_script(tmp_path))), model_flag=None
        )
    }
    monkeypatch.setattr(
        rollover_paths,
        "measure_agent_context",
        lambda **kwargs: ContextMeasurement(60001, "measured"),
    )
    runner = mode_c.TeamHarnessAgentRunner(config=config, log_dir=tmp_path / "logs")
    killed = []
    original_kill = runner.manager.kill

    def kill(agent_id: str) -> None:
        killed.append(agent_id)
        original_kill(agent_id)

    monkeypatch.setattr(runner.manager, "kill", kill)
    result = await runner.run_agent(
        agent_type="codex", prompt="p", cwd=str(tmp_path), timeout_sec=10, task_key="t"
    )
    assert result.success and len(killed) == 1
