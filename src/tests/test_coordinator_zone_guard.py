"""Worker-only coordinator-zone PreToolUse guard regression tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

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
            **{k: v for k, v in os.environ.items() if k != "TH_WORKER_ALLOW_PATHS"},
            "HOME": str(tmp_path),
            "TH_HARNESS_SRC": str(REPO / "src"),
            **env,
        },
    )
    return json.loads(result.stdout) if result.stdout else {}


def denied(value: dict[str, object]) -> bool:
    output = value.get("hookSpecificOutput", {})
    return isinstance(output, dict) and output.get("permissionDecision") == "deny"


@pytest.mark.parametrize(
    ("template", "blocked"),
    [
        ("cat >> design/decisions.md <<'EOF'\n{target}로 옮긴다\nEOF\n", False),
        (
            "*** Begin Patch\n*** Update File: design/decisions.md\n+{target}\n*** End Patch\n",
            False,
        ),
        ("*** Begin Patch\n*** Add File: {target}\n+text\n*** End Patch\n", True),
        (
            "*** Begin Patch\n*** Update File: {target}\n-old\n+new\n*** End Patch\n",
            True,
        ),
        ("*** Begin Patch\n*** Delete File: {target}\n*** End Patch\n", True),
        (
            "*** Begin Patch\n*** Update File: safe\n*** Move to: {target}\n*** End Patch\n",
            True,
        ),
        ("cat > {target} <<EOF\ntext\nEOF\n", True),
        ("python3 - <<PY\nopen('{target}')\nPY\n", True),
        ("cat <<-EOF\n\t{target}\n\tEOF\necho safe\n", False),
        ("tee {target} <<'END'\ntext\nEND\n", True),
        ("bash <<EOF\ncat {target}\nEOF\n", True),
        ("cat <<EOF\ntext\nEOF\ncat {target}\n", True),
        (
            "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: safe\n+{target}\n*** End Patch\nPATCH\n",
            False,
        ),
        (
            "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: {target}\n+x\n*** End Patch\nPATCH\n",
            True,
        ),
    ],
)
def test_literal_bodies_and_patch_headers(
    tmp_path: Path, template: str, blocked: bool
) -> None:
    target = tmp_path / "other" / ("." + "claude") / "archive"
    command = template.format(target=target)
    payload: dict[str, object] = {"tool_input": {"command": command}}
    assert denied(run_guard(tmp_path, payload, TH_ROLE="worker")) is blocked


def test_no_immediate_failed_advice_in_source_or_scripts() -> None:
    forbidden = "FAILED에 이유를" + " 쓰세요"
    for root in (REPO / "src", REPO / "scripts"):
        for path in root.rglob("*.py"):
            assert forbidden not in path.read_text(encoding="utf-8"), str(path)


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

    result = run_guard(tmp_path, read(tmp_path / ".claude" / "workers.md"), **common)
    assert denied(result)
    output = result["hookSpecificOutput"]
    assert isinstance(output, dict)
    reason = output["permissionDecisionReason"]
    assert "다른 방법으로도 막히면" in reason
    assert "FAILED에 이유를" + " 쓰세요" not in reason
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


def test_printf_escaped_newlines_allow_own_output(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    own = workspace / "_outputs" / "task"
    command = f"printf '...\\n출력: {own}\\n' > {own}/WORKLOG.md"
    assert not denied(
        run_guard(
            tmp_path,
            {"hook_event_name": "PreToolUse", "tool_input": {"cmd": command}},
            TH_ROLE="worker",
            TH_WORKSPACE=str(workspace),
            TH_WORKER_OUTPUT_DIR=str(own),
        )
    )


@pytest.mark.parametrize(
    "suffix", [".", ",", ";", ":", "!", "?", ")", "]", "}", ">", "'", '"', "`", ".),"]
)
def test_command_path_trailing_punctuation(tmp_path: Path, suffix: str) -> None:
    target = tmp_path / "outputs" / "runs" / "retrain_v19_test100_20261029"
    assert coordinator_paths_in_command(
        f"echo 'save to {target}{suffix} then'", cwd=tmp_path
    ) == [target]


def test_command_path_parentheses_and_escape(tmp_path: Path) -> None:
    target = tmp_path / "outputs" / "runs" / "x" / "STATUS"
    assert coordinator_paths_in_command(f"cat ({target})", cwd=tmp_path) == [target]
    assert coordinator_paths_in_command(f"printf '{target}\\n'", cwd=tmp_path) == [
        target
    ]


@pytest.mark.parametrize(
    "particle",
    ["에", "을", "를", "은", "는", "이", "가", "로", "으로", "의", "와", "과", "도"],
)
def test_command_path_korean_particle(tmp_path: Path, particle: str) -> None:
    target = tmp_path / "outputs" / "runs" / "abc_1029"
    assert coordinator_paths_in_command(f"echo {target}{particle}", cwd=tmp_path) == [
        target
    ]


def test_existing_korean_path_and_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "outputs" / "runs" / "한글이"
    target.mkdir(parents=True)
    assert coordinator_paths_in_command(f"cat {target}", cwd=tmp_path) == [target]
    assert coordinator_paths_in_command(f"cat {target}/..", cwd=tmp_path) == [
        target.parent
    ]
    assert coordinator_paths_in_command(f"cat {target}/.", cwd=tmp_path) == [target]


def test_guard_allows_both_path_variants_and_denies_other(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    own = workspace / "outputs" / "runs" / "abc_1029"
    original = workspace / "outputs" / "runs" / "abc_1029에"
    common = {
        "TH_ROLE": "worker",
        "TH_WORKSPACE": str(workspace),
        "TH_WORKER_OUTPUT_DIR": str(own),
    }

    def check(path: Path, **env: str) -> bool:
        return denied(
            run_guard(
                tmp_path,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_input": {"cmd": f"echo {path}"},
                },
                **env,
            )
        )

    assert not check(Path(str(own) + "."), **common)
    assert not check(original, **common)
    assert not check(original, **{**common, "TH_WORKER_OUTPUT_DIR": str(original)})
    assert not check(
        original,
        **{
            **common,
            "TH_WORKER_OUTPUT_DIR": "",
            "TH_WORKER_ALLOW_PATHS": json.dumps([str(original)]),
        },
    )
    assert check(workspace / "outputs" / "runs" / "other", **common)
