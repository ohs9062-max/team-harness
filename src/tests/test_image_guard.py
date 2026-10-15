"""Unit tests for the installed, stdlib-only Claude image guard hook."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "hooks" / "image_guard.py"


def _run_guard(
    tmp_path: Path, payload: dict[str, object], **env: str
) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path), **env},
    )
    return json.loads(result.stdout) if result.stdout else {}


def _prompt(session: str, text: str) -> dict[str, object]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "prompt": text,
    }


def _read(session: str, path: Path) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": "Read",
        "tool_input": {"file_path": str(path)},
    }


def _is_denied(result: dict[str, object]) -> bool:
    output = result.get("hookSpecificOutput", {})
    return isinstance(output, dict) and output.get("permissionDecision") == "deny"


def test_image_read_is_denied_without_a_user_permit(tmp_path: Path) -> None:
    assert _is_denied(_run_guard(tmp_path, _read("session-1", tmp_path / "chart.png")))


def test_explicit_path_permit_allows_only_that_path(tmp_path: Path) -> None:
    requested = tmp_path / "requested.png"
    other = tmp_path / "other.png"
    _run_guard(tmp_path, _prompt("session-1", f"{requested} 이미지를 읽어줘"))

    assert not _is_denied(_run_guard(tmp_path, _read("session-1", requested)))
    assert _is_denied(_run_guard(tmp_path, _read("session-1", other)))


def test_stop_removes_the_current_turn_permit(tmp_path: Path) -> None:
    path = tmp_path / "chart.png"
    _run_guard(tmp_path, _prompt("session-1", f"{path} 이미지를 확인해"))
    _run_guard(tmp_path, {"hook_event_name": "Stop", "session_id": "session-1"})

    assert _is_denied(_run_guard(tmp_path, _read("session-1", path)))


def test_image_cannot_be_reread_without_a_new_explicit_path_request(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chart.png"
    _run_guard(tmp_path, _prompt("session-1", f"{path} 이미지를 읽어줘"))
    assert not _is_denied(_run_guard(tmp_path, _read("session-1", path)))

    assert _is_denied(_run_guard(tmp_path, _read("session-1", path)))
    _run_guard(tmp_path, {"hook_event_name": "Stop", "session_id": "session-1"})
    _run_guard(tmp_path, _prompt("session-1", "이미지를 읽어줘"))
    assert _is_denied(_run_guard(tmp_path, _read("session-1", path)))


def test_unscoped_explicit_request_allows_at_most_five_new_images(
    tmp_path: Path,
) -> None:
    _run_guard(tmp_path, _prompt("session-1", "이미지를 읽어줘"))
    paths = [tmp_path / f"image-{index}.png" for index in range(6)]

    for path in paths[:5]:
        assert not _is_denied(_run_guard(tmp_path, _read("session-1", path)))
    assert _is_denied(_run_guard(tmp_path, _read("session-1", paths[5])))


def test_worker_image_override_allows_a_read(tmp_path: Path) -> None:
    assert not _is_denied(
        _run_guard(
            tmp_path, _read("session-1", tmp_path / "chart.png"), TH_ALLOW_IMAGES="1"
        )
    )


def test_bash_byte_reader_is_denied_without_a_permit(tmp_path: Path) -> None:
    path = tmp_path / "chart.png"
    result = _run_guard(
        tmp_path,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "tool_name": "Bash",
            "tool_input": {"command": f"base64 {path}"},
        },
    )
    assert _is_denied(result)


def test_non_image_files_always_pass(tmp_path: Path) -> None:
    assert not _is_denied(
        _run_guard(tmp_path, _read("session-1", tmp_path / "table.csv"))
    )
