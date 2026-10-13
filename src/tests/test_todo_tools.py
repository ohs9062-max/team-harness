# pyright: reportMissingParameterType=false

import json

import pytest

from team_harness.agents.manager import AgentManager
from team_harness.tools import todo_tools
from team_harness.tracking.context import ContextTracker
from team_harness.ui.console import TeamHarnessConsole


@pytest.mark.asyncio
async def test_todo_round_trip_and_console_panel(tmp_path):
    todo_tools.setup(run_dir=tmp_path)
    tasks = [{"id": "1", "description": "x", "status": "pending"}]
    assert await todo_tools.todo_write(tasks) == "Todo list updated (1 tasks)."
    assert json.loads(await todo_tools.todo_read()) == tasks

    console = TeamHarnessConsole(
        ctx=ContextTracker(model_id="m", model_limit=100),
        manager=AgentManager(),
        run_dir=tmp_path,
    )
    layout = console._render_live()
    assert "todos" in {child.name for child in layout.children}


@pytest.mark.asyncio
async def test_todo_invalid_status(tmp_path):
    todo_tools.setup(run_dir=tmp_path)
    assert "ERROR:" in await todo_tools.todo_write(
        [{"id": "1", "description": "x", "status": "bad"}]
    )


def test_console_load_todos_corrupted_or_partial_file(tmp_path):
    todo_file = tmp_path / "todo.json"
    todo_file.write_text("")  # 0 bytes, e.g. during non-atomic write
    console = TeamHarnessConsole(
        ctx=ContextTracker(model_id="m", model_limit=100),
        manager=AgentManager(),
        run_dir=tmp_path,
    )
    # Should not raise JSONDecodeError; should return []
    assert console._load_todos() == []

    # Partially written JSON
    todo_file.write_text("[{'id': ")
    assert console._load_todos() == []


@pytest.mark.asyncio
async def test_todo_write_atomic(tmp_path, monkeypatch):
    called: list[tuple[object, object]] = []
    import team_harness.tools.todo_tools as tt

    def fake_write_atomic(*, path, payload):
        called.append((path, payload))
        path.write_text(json.dumps(payload))

    monkeypatch.setattr(tt, "write_json_atomic", fake_write_atomic)
    todo_tools.setup(run_dir=tmp_path)
    tasks = [{"id": "1", "description": "atomic", "status": "pending"}]
    await todo_tools.todo_write(tasks)
    assert len(called) == 1
    assert called[0][1] == tasks

    # Also test closure binding
    bindings = todo_tools.build_todo_tool_bindings(run_dir=tmp_path)
    bound_write = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "todo_write"
    )
    await bound_write(tasks)
    assert len(called) == 2
