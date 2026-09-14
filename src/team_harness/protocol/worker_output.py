"""Recover a worker's final answer from its raw stdout.

Worker CLIs don't all print plain text: `codex exec --json` emits a JSONL
event stream and `claude -p --output-format stream-json` emits NDJSON, while
antigravity prints plain text. The protocol feeds `AgentResult.output_text`
into the next agent's prompt (design -> implement, review -> fix/response),
into verdict/disposition parsing, and into the MODE A compare report, so
passing the raw event stream through bloats prompts with tool-call noise and
lets words inside intermediate events leak into disposition matching.

The raw stream is still preserved on disk (`AgentResult.stdout_path`); this
only chooses what the protocol treats as the agent's answer. Anything that
isn't a recognized event stream is returned unchanged.
"""

from __future__ import annotations

import json
from typing import Any


def extract_final_text(stdout: str) -> str:
    """Return the worker's final answer text, or `stdout` if not a known stream."""

    events = _json_events(stdout)
    if not events:
        return stdout

    # claude stream-json: the terminal `result` event carries the final answer.
    for event in reversed(events):
        result = event.get("result")
        if event.get("type") == "result" and isinstance(result, str) and result.strip():
            return result

    # codex exec --json: the last completed agent_message is the final answer;
    # earlier ones are progress narration ("I'll check X next").
    for event in reversed(events):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text

    # claude stream-json without a result event (e.g. killed mid-run): fall
    # back to the last assistant message's text blocks.
    for event in reversed(events):
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
        ]
        if texts:
            return "\n".join(texts)

    return stdout


def _json_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events
