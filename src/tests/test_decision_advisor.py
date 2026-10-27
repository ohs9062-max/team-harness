# pyright: reportMissingParameterType=false
"""No-network coverage of shared advice and Python launch boundaries."""

import asyncio
import json
import time

import pytest

from team_harness.agents.template import AgentTemplate
from team_harness.config import Config
from team_harness.decisions import advisor
from team_harness.decisions.jev_backend import JevAnswer
from team_harness.decisions.models import DecisionRouterSettings
from team_harness.protocol.mode_c import TeamHarnessAgentRunner
from team_harness.tools import agent_tools
from team_harness.tracking.run_log import RunLogWriter


@pytest.fixture(autouse=True)
def isolated_advice(tmp_path, monkeypatch):
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("TEAM_HARNESS_JEV", "1")
    monkeypatch.setenv("TH_TEST_MOCK_JEV", "1")
    monkeypatch.delenv("TH_TEST_JEV_CHOICE", raising=False)
    monkeypatch.delenv("TEAM_HARNESS_JEV_TIMEOUT", raising=False)
    monkeypatch.setattr(advisor, "_secrets", lambda: None)
    monkeypatch.setattr(
        advisor, "_settings", lambda *args: DecisionRouterSettings(enabled=True)
    )


def records(tmp_path):
    return [
        json.loads(line)
        for line in (tmp_path / "watch/jev_decisions.log").read_text().splitlines()
    ]


def test_tier_and_failure_are_bounded_and_audited(tmp_path, monkeypatch):
    seen = []

    async def decide(self, request):
        seen.append(request.state)
        return JevAnswer(next(iter(request.options)), 0.96, {}, {}, 1)

    monkeypatch.setattr(advisor.MockWatchBackend, "decide", decide)
    assert advisor.advise_model_tier("x" * 8000, "codex", "terra", "medium")
    (tmp_path / "FAILED").write_text("bad environment")
    (tmp_path / "STATUS").write_text("old\n" * 20 + "last\n")
    (tmp_path / "task.md").write_text("task summary")
    assert advisor.advise_failure(tmp_path)
    assert all(len(text) <= 4000 for text in seen)
    assert "FAILED: bad environment" in seen[1]
    assert "task summary" in seen[1]
    assert len(records(tmp_path)) == 2
    assert records(tmp_path)[0]["path"] == "python"


@pytest.mark.parametrize(
    "failure", ["timeout", "missing_key", "exception", "low", "invalid"]
)
def test_fallback_never_interrupts_execution(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("TEAM_HARNESS_JEV_TIMEOUT", "0.03")

    async def decide(self, request):
        if failure == "timeout":
            await asyncio.sleep(0.5)
        if failure == "exception":
            raise RuntimeError("fake backend failed")
        return JevAnswer(
            "unknown" if failure == "invalid" else "TERRA_HIGH",
            0.1 if failure == "low" else 0.96,
            {},
            {},
            1,
        )

    monkeypatch.setattr(advisor.MockWatchBackend, "decide", decide)
    if failure == "missing_key":
        monkeypatch.setenv("TH_TEST_MOCK_JEV", "0")
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("JEV_API_KEY", raising=False)
    started = time.monotonic()
    assert advisor.advise_model_tier("task", "codex", "terra", "medium") is None
    assert time.monotonic() - started < 0.4
    assert records(tmp_path)[0]["reason"]


def test_disabled_and_other_workers_do_not_call_or_log(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("disabled advisor touched settings")

    monkeypatch.setattr(advisor, "_settings", unexpected)
    monkeypatch.setenv("TEAM_HARNESS_JEV", "0")
    assert advisor.advise_model_tier("task", "codex", None, None) is None
    assert advisor.advise_failure(tmp_path) is None
    monkeypatch.setenv("TEAM_HARNESS_JEV", "1")
    assert advisor.advise_model_tier("task", "agy", None, None) is None
    assert not (tmp_path / "watch/jev_decisions.log").exists()


def test_callback_failure_is_audited(tmp_path):
    def broken(record):
        raise RuntimeError("record failed")

    assert advisor.advise_model_tier("task", "codex", "terra", "medium", record=broken)
    assert (
        "callback_error: RuntimeError: record failed" in records(tmp_path)[-1]["reason"]
    )


def worker_config(tmp_path):
    script = tmp_path / "worker.sh"
    script.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 1\n')
    return Config(
        cwd=str(tmp_path),
        run_dir=tmp_path,
        worker_suffix="",
        decision_router=DecisionRouterSettings(enabled=True),
        agent_templates={
            "codex": AgentTemplate(
                command=("sh", str(script)),
                model_flag="--model",
                reasoning_effort_flag=("--effort", "{effort}"),
                reasoning_effort="medium",
            )
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_spawn_bindings_record_and_wait_exposes_failure(
    tmp_path, manager, ui, bound, monkeypatch
):
    config = worker_config(tmp_path)
    # No task markers: built-in workers must supply actual runtime evidence.
    log = RunLogWriter(
        run_id="test",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    if bound:
        bindings = agent_tools.build_agent_tool_bindings(
            manager=manager, run_log=log, config=config, ui=ui, allowed_types=["codex"]
        )
        funcs = {schema["function"]["name"]: func for schema, func in bindings}
        spawn, wait = funcs["spawn_agent"], funcs["wait_for_agents"]
    else:
        agent_tools.setup(manager=manager, run_log=log, config=config, ui=ui)
        spawn, wait = agent_tools.spawn_agent, agent_tools.wait_for_agents
    aid = await spawn(
        type="codex",
        prompt="bounded task",
        cwd=str(tmp_path),
        model="explicit-model",
        effort="high",
    )
    await asyncio.wait_for(manager.wait_one(aid), 2)
    result = json.loads(await wait(agent_ids=[aid]))
    assert result["agents"][aid] == "failed (exit 1)"
    assert "jev:" in result["jev"][aid]
    saved = json.loads((tmp_path / "run.json").read_text())
    assert saved["decisions"][0]["kind"] == "jev_tier"
    assert saved["decisions"][0]["applied"] is False
    assert (
        next(a for a in saved["agents"] if a["id"] == aid)["requested_model"]
        == "explicit-model"
    )
    assert (
        next(a for a in saved["agents"] if a["id"] == aid)["requested_effort"] == "high"
    )
    assert records(tmp_path)[0]["path"] == "agent_tools.spawn_agent"


@pytest.mark.asyncio
async def test_protocol_common_role_spawn_records_advice(tmp_path):
    config = worker_config(tmp_path)
    runner = TeamHarnessAgentRunner(
        config=config, log_dir=tmp_path / "roles", verbose=False
    )
    await runner.run_agent(
        agent_type="codex",
        prompt="bounded task",
        cwd=str(tmp_path),
        timeout_sec=2,
        model="explicit-model",
        effort="high",
    )
    saved = json.loads((tmp_path / "roles/run.json").read_text())
    assert saved["decisions"][0]["kind"] == "jev_tier"
    assert saved["decisions"][0]["applied"] is False
    assert records(tmp_path)[0]["path"] == "protocol.role_spawn"


@pytest.mark.asyncio
async def test_default_protocol_loads_target_settings_and_preserves_existing_log(
    tmp_path, monkeypatch
):
    config = worker_config(tmp_path)
    seen = []
    monkeypatch.delenv("TEAM_HARNESS_JEV")

    def settings(workspace=""):
        seen.append(workspace)
        return DecisionRouterSettings(enabled=True)

    monkeypatch.setattr(advisor, "_settings", settings)
    runner = TeamHarnessAgentRunner(
        log_dir=tmp_path / "roles", verbose=False, advice_workspace=str(tmp_path)
    )
    # Retain default-constructor semantics while replacing only subprocess command.
    runner.config.agent_templates = config.agent_templates
    await runner.run_agent(
        agent_type="codex", prompt="task", cwd=str(tmp_path), timeout_sec=2
    )
    log = tmp_path / "roles/run.json"
    saved = json.loads(log.read_text())
    saved["existing_metadata"] = "preserve me"
    log.write_text(json.dumps(saved))
    second = TeamHarnessAgentRunner(
        log_dir=tmp_path / "roles", verbose=False, advice_workspace=str(tmp_path)
    )
    second.config.agent_templates = config.agent_templates
    await second.run_agent(
        agent_type="codex", prompt="task", cwd=str(tmp_path), timeout_sec=2
    )
    saved = json.loads(log.read_text())
    assert len(saved["decisions"]) == 2
    assert saved["existing_metadata"] == "preserve me"
    assert set(seen) == {str(tmp_path)}


def test_builtin_failure_supplies_exit_prompt_and_logs(tmp_path, monkeypatch):
    seen = []

    async def decide(self, request):
        seen.append(request.state)
        return JevAnswer("BRIEF_ERROR", 0.96, {}, {}, 1)

    monkeypatch.setattr(advisor.MockWatchBackend, "decide", decide)
    stderr = tmp_path / "stderr.log"
    stderr.write_text("actual worker error")
    assert advisor.advise_failure(
        tmp_path,
        failure_context="exit=1",
        task_text="original assignment",
        stderr_log=stderr,
    )
    assert "actual worker error" in seen[0]
    assert "exit=1" in seen[0]
    assert "original assignment" in seen[0]
