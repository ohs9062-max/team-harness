"""Worker-only coordinator-zone PreToolUse guard regression tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from team_harness.coordinator_zone import coordinator_paths_in_command

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "hooks" / "coordinator_zone_guard.py"


def run_guard(
    tmp_path: Path, payload: dict[str, object], **env: str
) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "TH_HARNESS_SRC": str(REPO / "src"),
            **env,
        },
    )
    return json.loads(result.stdout) if result.stdout else {}


def denied(value: dict[str, object]) -> bool:
    output = value.get("hookSpecificOutput", {})
    return isinstance(output, dict) and output.get("permissionDecision") == "deny"


def test_worker_guard_blocks_coordinator_zone_but_not_source_or_own_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    own_output = workspace / "_outputs" / "task"
    own_output.mkdir(parents=True)
    common = {
        "TH_ROLE": "worker",
        "TH_WORKSPACE": str(workspace),
        "TH_WORKER_OUTPUT_DIR": str(own_output),
    }

    def read(path: Path) -> dict[str, object]:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(path)},
        }

    assert denied(
        run_guard(tmp_path, read(tmp_path / ".claude" / "workers.md"), **common)
    )
    assert denied(
        run_guard(
            tmp_path, read(workspace / "_outputs" / "other" / "WORKLOG.md"), **common
        )
    )
    assert not denied(run_guard(tmp_path, read(own_output / "WORKLOG.md"), **common))
    assert not denied(
        run_guard(tmp_path, read(workspace / "src" / "code.py"), **common)
    )


def test_worker_guard_allows_explicit_task_front_matter_path(tmp_path: Path) -> None:
    target = tmp_path / ".team-harness" / "worker_suffix.md"
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": f"cat {target}"},
    }
    result = run_guard(
        tmp_path,
        payload,
        TH_ROLE="worker",
        TH_WORKER_ALLOW_PATHS=json.dumps([str(target)]),
    )
    assert not denied(result)


def test_worker_guard_blocks_claude_directory_in_any_project(tmp_path: Path) -> None:
    other_project_record = tmp_path / "other-project" / ".claude" / "work_log.md"
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": str(other_project_record)},
    }
    common = {"TH_ROLE": "worker", "TH_WORKSPACE": str(tmp_path / "workspace")}

    assert denied(run_guard(tmp_path, payload, **common))
    assert not denied(
        run_guard(
            tmp_path,
            payload,
            TH_WORKER_ALLOW_PATHS=json.dumps([str(other_project_record)]),
            **common,
        )
    )
    for name in (".claude_backup", "claude"):
        allowed_path = tmp_path / "other-project" / name / "work_log.md"
        payload["tool_input"] = {"file_path": str(allowed_path)}
        assert not denied(run_guard(tmp_path, payload, **common))


def test_worker_guard_blocks_absolute_and_parent_claude_command_paths(
    tmp_path: Path,
) -> None:
    absolute = "/tmp/p/.claude/current_status.md"
    parent_relative = "../p/.claude/x.md"
    assert coordinator_paths_in_command(f"cat {absolute}", cwd=tmp_path) == [
        Path(absolute)
    ]
    assert coordinator_paths_in_command(f"cat {parent_relative}", cwd=tmp_path) == [
        (tmp_path / parent_relative).resolve()
    ]

    for command in (f"cat {absolute}", f"cat {parent_relative}"):
        assert denied(
            run_guard(
                tmp_path,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                TH_ROLE="worker",
            )
        )


def test_worker_guard_handles_codex_payloads_and_allows_without_worker_role(
    tmp_path: Path,
) -> None:
    codex_payloads: list[dict[str, object]] = [
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "cat .claude/work_log.md"},
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "shell",
            "tool_input": {"command": ["bash", "-lc", "cat .claude/work_log.md"]},
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(tmp_path),
            "tool_input": {"command": "cat .claude/work_log.md"},
        },
    ]
    for payload in codex_payloads:
        assert denied(run_guard(tmp_path, payload, TH_ROLE="worker"))
        env = {k: v for k, v in os.environ.items() if k != "TH_ROLE"}
        env["HOME"] = str(tmp_path)
        env["TH_HARNESS_SRC"] = str(REPO / "src")
        res = subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            cwd=tmp_path,
            env=env,
        )
        assert res.stdout == ""


def test_install_script_updates_claude_and_codex_hooks_idempotently(
    tmp_path: Path,
) -> None:
    install_script = REPO / "scripts" / "install-coordinator-zone-guard.sh"
    claude_settings = tmp_path / ".claude" / "settings.json"
    codex_hooks = tmp_path / ".codex" / "hooks.json"
    codex_backup = tmp_path / ".codex" / "hooks.json.bak_20261019c"
    codex_hooks.parent.mkdir(parents=True)
    initial_codex = {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": ": orca-agent-hook-form=1;",
                            "timeout": 10,
                        }
                    ]
                }
            ]
        }
    }
    codex_hooks.write_text(json.dumps(initial_codex), encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TH_CLAUDE_HOOKS_DIR": str(tmp_path / ".claude" / "hooks"),
        "TH_CLAUDE_SETTINGS": str(claude_settings),
        "TH_CODEX_HOOKS": str(codex_hooks),
        "TH_CODEX_HOOKS_BACKUP": str(codex_backup),
    }
    subprocess.run(["bash", str(install_script)], check=True, env=env)
    subprocess.run(["bash", str(install_script)], check=True, env=env)

    assert codex_backup.exists()
    assert json.loads(codex_backup.read_text(encoding="utf-8")) == initial_codex
    updated = json.loads(codex_hooks.read_text(encoding="utf-8"))
    pre_tool_use = updated["hooks"]["PreToolUse"]
    assert len(pre_tool_use) == 2
    assert pre_tool_use[0]["hooks"][0]["command"] == ": orca-agent-hook-form=1;"
    assert "coordinator_zone_guard.py" in pre_tool_use[1]["hooks"][0]["command"]
