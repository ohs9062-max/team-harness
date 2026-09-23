from __future__ import annotations

import json

from team_harness.decisions.models import DecisionRequest

TOOL_PROFILES: dict[str, frozenset[str] | None] = {
    "FULL": None,
    "DELEGATE": frozenset(
        {
            "spawn_agent",
            "agent_status",
            "read_agent_output",
            "read_new_agent_output",
            "list_agents",
            "agent_availability",
            "wait_for_agents",
            "wait_for_any",
            "kill_agent",
            "read_file",
            "read_new_file_content",
            "ls",
            "glob",
            "grep",
            "bash",
            "todo_write",
            "todo_read",
        }
    ),
    "EDIT": frozenset(
        {
            "read_file",
            "write_file",
            "append_file",
            "edit_file",
            "multi_edit_file",
            "read_new_file_content",
            "ls",
            "glob",
            "grep",
            "bash",
            "todo_write",
            "todo_read",
        }
    ),
    "MONITOR": frozenset(
        {
            "agent_status",
            "read_agent_output",
            "read_new_agent_output",
            "list_agents",
            "agent_availability",
            "wait_for_agents",
            "wait_for_any",
            "kill_agent",
            "read_file",
            "read_new_file_content",
            "todo_write",
            "todo_read",
        }
    ),
}

TOOL_PROFILE_CRITERIA = {
    "FULL": "Mixed, unclear, or likely to need both agent delegation and file editing.",
    "DELEGATE": "Plan, spawn, inspect, wait for, or manage worker agents.",
    "EDIT": "Directly inspect, edit, and test files without managing worker agents.",
    "MONITOR": "Observe or control workers already running; no new worker or file edits needed.",
}

_KNOWN_PROFILED_TOOLS = frozenset().union(
    *(tools for tools in TOOL_PROFILES.values() if tools is not None)
)


def build_tool_selection_request(
    *, messages: list[dict], max_state_chars: int
) -> DecisionRequest:
    state = _compact_state(messages=messages, max_chars=max_state_chars)
    return DecisionRequest(
        kind="tool_selection",
        state=state,
        options=TOOL_PROFILE_CRITERIA,
        fallback="FULL",
        instructions=(
            "Choose the smallest tool profile that can complete the coordinator's "
            "next turn. Choose FULL whenever the next action is uncertain or mixed."
        ),
    )


def schemas_for_profile(*, schemas: list[dict], profile: str) -> list[dict]:
    allowed = TOOL_PROFILES.get(profile)
    if allowed is None:
        return schemas
    return [
        schema
        for schema in schemas
        if (
            schema.get("function", {}).get("name") in allowed
            or schema.get("function", {}).get("name") not in _KNOWN_PROFILED_TOOLS
        )
    ]


def schema_chars(schemas: list[dict]) -> int:
    return len(json.dumps(schemas, ensure_ascii=False, separators=(",", ":")))


def _compact_state(*, messages: list[dict], max_chars: int) -> dict:
    items: list[dict[str, str]] = []
    remaining = max_chars
    for message in reversed(messages):
        if remaining <= 0 or len(items) >= 6:
            break
        role = str(message.get("role", "unknown"))
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        content = content[:remaining]
        items.append({"role": role, "content": content})
        remaining -= len(content)
    items.reverse()
    return {"recent_messages": items}
