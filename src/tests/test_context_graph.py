"""TH-D22: optional prebuilt code graph for workers.

A fake ``graft`` (a small Python script) stands in for the real CLI so the
tests need neither Node nor network access.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

from team_harness.agents.context_graph import build_worker_hint
from team_harness.agents.context_graph import ContextGraph
from team_harness.agents.context_graph import ContextGraphSettings
from team_harness.agents.context_graph import graph_dir_for
from team_harness.config import AgentTemplate
from team_harness.config import Config
from team_harness.config import load_config
from team_harness.config import load_context_graph_settings
from team_harness.protocol.mode_c import TeamHarnessAgentRunner

FAKE_GRAFT = """
import json, os, sys, time
log = os.environ["FAKE_GRAFT_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "dnt": os.environ.get("DO_NOT_TRACK")}) + "\\n")
mode = os.environ.get("FAKE_GRAFT_MODE", "ok")
if mode == "fail":
    print("boom")
    sys.exit(3)
if mode == "sleep":
    time.sleep(30)
graph = sys.argv[sys.argv.index("--dir") + 1]
os.makedirs(graph, exist_ok=True)
"""


@pytest.fixture
def fake_graft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    script = tmp_path / "fake_graft.py"
    script.write_text(FAKE_GRAFT, encoding="utf-8")
    monkeypatch.setenv("FAKE_GRAFT_LOG", str(tmp_path / "graft_calls.jsonl"))
    return (sys.executable, str(script))


def _calls(tmp_path: Path) -> list[dict]:
    log = tmp_path / "graft_calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _graph(
    tmp_path: Path, command: tuple[str, ...], **overrides: object
) -> ContextGraph:
    settings = ContextGraphSettings(
        enabled=True,
        command=command,
        graphs_dir=str(tmp_path / "graphs"),
        **overrides,  # type: ignore[arg-type]
    )
    return ContextGraph(settings)


def _workdir(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    (work / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return work


async def test_disabled_by_default_changes_nothing(tmp_path: Path) -> None:
    graph = ContextGraph()
    prompt, env = await graph.prepare_spawn(
        agent_type="codex", prompt="do it", cwd=tmp_path
    )
    assert (prompt, env) == ("do it", {})
    assert Config().context_graph.enabled is False


async def test_builds_outside_tree_and_prepends_hint(
    tmp_path: Path, fake_graft: tuple[str, ...]
) -> None:
    work = _workdir(tmp_path)
    graph = _graph(tmp_path, fake_graft)

    prompt, env = await graph.prepare_spawn(
        agent_type="codex", prompt="do it", cwd=work
    )

    expected_dir = graph_dir_for(graphs_dir=tmp_path / "graphs", root=work.resolve())
    [call] = _calls(tmp_path)
    assert call["argv"] == [
        "--dir",
        str(expected_dir),
        "build",
        "--no-gitignore",
        "--no-ignore",
        str(work.resolve()),
    ]
    assert call["dnt"] == "1"
    # Nothing is written into the worker's directory (TH-D16).
    assert sorted(p.name for p in work.iterdir()) == ["main.py"]
    # Fixed hint first, task last; every command pins --dir and the root.
    assert prompt.startswith("[Code graph]")
    assert prompt.endswith("\n\ndo it")
    assert prompt.count(f"--dir {expected_dir}") == 4
    assert env["DO_NOT_TRACK"] == "1"


async def test_one_build_per_directory_even_when_concurrent(
    tmp_path: Path, fake_graft: tuple[str, ...]
) -> None:
    work = _workdir(tmp_path)
    graph = _graph(tmp_path, fake_graft)

    await asyncio.gather(
        *(
            graph.prepare_spawn(agent_type="codex", prompt=f"p{i}", cwd=work)
            for i in range(3)
        )
    )
    await graph.prepare_spawn(agent_type="claude", prompt="later", cwd=work)

    assert len(_calls(tmp_path)) == 1


async def test_resume_gets_env_but_no_second_hint(
    tmp_path: Path, fake_graft: tuple[str, ...]
) -> None:
    work = _workdir(tmp_path)
    graph = _graph(tmp_path, fake_graft)

    prompt, env = await graph.prepare_spawn(
        agent_type="codex", prompt="continue", cwd=work, mode="resume"
    )

    assert prompt == "continue"
    assert env["DO_NOT_TRACK"] == "1"


async def test_harness_worker_is_left_alone(
    tmp_path: Path, fake_graft: tuple[str, ...]
) -> None:
    work = _workdir(tmp_path)
    graph = _graph(tmp_path, fake_graft)

    result = await graph.prepare_spawn(agent_type="harness", prompt="p", cwd=work)

    assert result == ("p", {})
    assert _calls(tmp_path) == []


async def test_missing_command_falls_back(tmp_path: Path) -> None:
    graph = _graph(tmp_path, ("definitely-not-a-real-graft-binary",))
    result = await graph.prepare_spawn(
        agent_type="codex", prompt="p", cwd=_workdir(tmp_path)
    )
    assert result == ("p", {})


async def test_failed_build_falls_back_and_is_not_retried(
    tmp_path: Path, fake_graft: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GRAFT_MODE", "fail")
    work = _workdir(tmp_path)
    graph = _graph(tmp_path, fake_graft)

    first = await graph.prepare_spawn(agent_type="codex", prompt="p", cwd=work)
    second = await graph.prepare_spawn(agent_type="codex", prompt="q", cwd=work)

    assert first == ("p", {})
    assert second == ("q", {})
    assert len(_calls(tmp_path)) == 1


async def test_build_timeout_falls_back(
    tmp_path: Path, fake_graft: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GRAFT_MODE", "sleep")
    graph = _graph(tmp_path, fake_graft, build_timeout_s=0.5)

    result = await graph.prepare_spawn(
        agent_type="codex", prompt="p", cwd=_workdir(tmp_path)
    )

    assert result == ("p", {})


def test_hint_quotes_paths_with_spaces(tmp_path: Path) -> None:
    root = tmp_path / "my repo"
    hint = build_worker_hint(command=("graft",), graph_dir=tmp_path / "g", root=root)
    assert f"'{root}'" in hint


def test_settings_reject_empty_command_and_bad_timeout() -> None:
    with pytest.raises(ValueError):
        ContextGraphSettings(command=())
    with pytest.raises(ValueError):
        ContextGraphSettings(build_timeout_s=0)


def _write_local_config(root: Path, body: str) -> None:
    config_dir = root / ".team-harness"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(body, encoding="utf-8")


def test_config_table_is_parsed_by_both_loaders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("team_harness.config.CONFIG_PATH", tmp_path / "absent.toml")
    monkeypatch.delenv("TEAM_HARNESS_CONTEXT_GRAPH", raising=False)
    _write_local_config(
        tmp_path,
        "[context_graph]\n"
        "enabled = true\n"
        'command = ["graft"]\n'
        "build_timeout_s = 60\n"
        'graphs_dir = "/tmp/graphs"\n',
    )

    expected = ContextGraphSettings(
        enabled=True, command=("graft",), build_timeout_s=60.0, graphs_dir="/tmp/graphs"
    )
    assert load_context_graph_settings(tmp_path) == expected
    assert load_config(cwd=str(tmp_path)).context_graph == expected


def test_env_var_toggles_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("team_harness.config.CONFIG_PATH", tmp_path / "absent.toml")
    monkeypatch.setenv("TEAM_HARNESS_CONTEXT_GRAPH", "1")
    assert load_context_graph_settings(tmp_path).enabled is True

    _write_local_config(tmp_path, "[context_graph]\nenabled = true\n")
    monkeypatch.setenv("TEAM_HARNESS_CONTEXT_GRAPH", "off")
    assert load_context_graph_settings(tmp_path).enabled is False

    monkeypatch.setenv("TEAM_HARNESS_CONTEXT_GRAPH", "maybe")
    with pytest.raises(SystemExit):
        load_context_graph_settings(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        'enabled = "yes"',
        "command = []",
        'command = "graft"',
        'build_timeout_s = "10"',
        "graphs_dir = 3",
    ],
)
def test_invalid_config_values_fail_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    monkeypatch.setattr("team_harness.config.CONFIG_PATH", tmp_path / "absent.toml")
    monkeypatch.delenv("TEAM_HARNESS_CONTEXT_GRAPH", raising=False)
    _write_local_config(tmp_path, f"[context_graph]\n{body}\n")
    with pytest.raises(SystemExit):
        load_context_graph_settings(tmp_path)


async def test_protocol_runner_sends_hint_and_env_to_worker(
    tmp_path: Path, fake_graft: tuple[str, ...]
) -> None:
    work = _workdir(tmp_path)
    record = tmp_path / "worker.json"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps("
        "{'prompt': sys.argv[-1], 'dnt': os.environ.get('DO_NOT_TRACK')}))\n",
        encoding="utf-8",
    )
    config = Config()
    config.agent_templates = {
        "codex": AgentTemplate(command=(sys.executable, str(worker)), model_flag=None)
    }
    runner = TeamHarnessAgentRunner(
        config=config,
        log_dir=tmp_path / "logs",
        context_graph=_graph(tmp_path, fake_graft),
    )

    result = await runner.run_agent(
        agent_type="codex", prompt="the task", cwd=str(work), timeout_sec=10
    )

    assert result.success is True
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["prompt"].startswith("[Code graph]")
    assert seen["prompt"].endswith("the task")
    assert seen["dnt"] == "1"
