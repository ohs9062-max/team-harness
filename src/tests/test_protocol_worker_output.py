from __future__ import annotations

import json
from pathlib import Path

import pytest

from team_harness.config import AgentTemplate
from team_harness.config import Config
from team_harness.protocol.mode_c import TeamHarnessAgentRunner
from team_harness.protocol.worker_output import extract_final_text


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"


def test_plain_text_is_returned_unchanged() -> None:
    text = "### Review\n- looks fine\nVERDICT: PASS\n"
    assert extract_final_text(text) == text


def test_codex_stream_returns_last_agent_message_only() -> None:
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "I'll check ACCEPT/REJECT next."},
        },
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": "REJECT noise"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Finding 1 — REJECT: URL is valid.",
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    )

    assert extract_final_text(stdout) == "Finding 1 — REJECT: URL is valid."


def test_claude_stream_prefers_result_event() -> None:
    stdout = _jsonl(
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "working on it"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "Final answer.\nVERDICT: PASS",
        },
    )

    assert extract_final_text(stdout) == "Final answer.\nVERDICT: PASS"


def test_claude_stream_without_result_falls_back_to_last_assistant_text() -> None:
    stdout = _jsonl(
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {}},
                    {"type": "text", "text": "Partial conclusion"},
                ]
            },
        },
    )

    assert extract_final_text(stdout) == "Partial conclusion"


def test_unrecognized_json_lines_return_raw_stdout() -> None:
    stdout = 'progress\n{"level": "info", "msg": "hello"}\ndone\n'
    assert extract_final_text(stdout) == stdout


@pytest.mark.asyncio
async def test_runner_output_text_is_the_final_answer_raw_log_kept(
    tmp_path: Path,
) -> None:
    events = _jsonl(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "noise"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ANSWER"}},
    )
    script = tmp_path / "emit.sh"
    script.write_text(f"cat <<'EOF'\n{events}EOF\n", encoding="utf-8")
    config = Config()
    config.agent_templates = {
        "codex": AgentTemplate(command=("sh", str(script)), model_flag=None)
    }
    runner = TeamHarnessAgentRunner(config=config, log_dir=tmp_path / "logs")

    result = await runner.run_agent(
        agent_type="codex", prompt="p", cwd=str(tmp_path), timeout_sec=10
    )

    assert result.success is True
    assert result.output_text == "ANSWER"
    assert "noise" in Path(result.stdout_path).read_text(encoding="utf-8")
