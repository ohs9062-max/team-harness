#!/usr/bin/env python3
"""PreToolUse guard for paths reserved to the team-harness coordinator."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def _load_zone():
    candidates: list[Path] = []
    source = os.environ.get("TH_HARNESS_SRC")
    if source:
        candidates.append(Path(source) / "team_harness" / "coordinator_zone.py")
    candidates.append(
        Path(__file__).resolve().parents[2]
        / "src"
        / "team_harness"
        / "coordinator_zone.py"
    )
    for mod_path in candidates:
        if mod_path.is_file():
            spec = importlib.util.spec_from_file_location(
                "coordinator_zone", mod_path
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return (
                    module.DENY_REASON,
                    module.coordinator_paths_in_command,
                    module.is_coordinator_zone,
                    module.parse_allowed_paths,
                )
    try:
        from team_harness.coordinator_zone import (  # pyright: ignore[reportMissingImports]
            DENY_REASON,
            coordinator_paths_in_command,
            is_coordinator_zone,
            parse_allowed_paths,
        )
    except ImportError:
        # Installed hook fallback: a missing source tree must not accidentally
        # block coordinators or arbitrary user sessions.
        return None
    return (
        DENY_REASON,
        coordinator_paths_in_command,
        is_coordinator_zone,
        parse_allowed_paths,
    )



def _deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}, ensure_ascii=False))


def _extract_command_paths(
    value: object, command_paths, *, cwd: str | None
) -> list[Path]:
    if isinstance(value, str):
        return list(command_paths(value, cwd=cwd))
    if isinstance(value, list):
        parts = [item for item in value if isinstance(item, str)]
        return list(command_paths(" ".join(parts), cwd=cwd))
    return []


def _extract_paths(
    payload: dict[str, object], command_paths, *, cwd: str | None
) -> list[Path]:
    raw_input = (
        payload.get("tool_input")
        if "tool_input" in payload
        else payload.get("toolInput")
        or payload.get("input")
        or payload.get("arguments")
    )
    if isinstance(raw_input, str):
        try:
            raw_input = json.loads(raw_input)
        except json.JSONDecodeError:
            return _extract_command_paths(raw_input, command_paths, cwd=cwd)
    tool_input = raw_input if isinstance(raw_input, dict) else payload
    paths: list[Path] = []
    for key in ("file_path", "path", "target_file"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    for key in ("command", "cmd", "patch", "script"):
        paths.extend(
            _extract_command_paths(tool_input.get(key), command_paths, cwd=cwd)
        )
    return paths


def main() -> None:
    if os.environ.get("TH_ROLE") != "worker":
        return
    loaded = _load_zone()
    if loaded is None:
        return
    reason, command_paths, in_zone, parse_allowed = loaded
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    if event not in (None, "PreToolUse", "pre_tool_use"):
        return
    payload_cwd = payload.get("cwd")
    cwd = (
        payload_cwd
        if isinstance(payload_cwd, str) and payload_cwd
        else os.environ.get("PWD")
    )
    allowed = parse_allowed(os.environ.get("TH_WORKER_ALLOW_PATHS"))
    kwargs = {
        "workspace": os.environ.get("TH_WORKSPACE"),
        "output_dir": os.environ.get("TH_WORKER_OUTPUT_DIR"),
        "allowed_paths": allowed,
        "cwd": cwd,
    }
    paths = _extract_paths(payload, command_paths, cwd=cwd)
    if any(in_zone(path, **kwargs) for path in paths):
        _deny(reason)


if __name__ == "__main__":
    main()

