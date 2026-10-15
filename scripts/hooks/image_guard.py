#!/usr/bin/env python3
"""Claude Code hooks that make image reads opt-in for one user turn.

The hook intentionally uses only the standard library: it is installed into
``~/.claude/hooks`` and must work independently of any project environment.
``image_permit-<session>.json`` is short-lived authorization for the current
turn; ``image_seen-<session>.json`` survives compaction/resume so an unscoped
permission cannot re-send an already-read image to the model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import Any


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")
DENY_REASON = "이미지 읽기 차단: 사용자가 명시적으로 읽으라고 한 경우에만 허용"
IMAGE_PATH_RE = re.compile(
    r'''(?:"([^"]+?\.(?:png|jpe?g|gif|webp|bmp|tiff?))"|'([^']+?\.(?:png|jpe?g|gif|webp|bmp|tiff?))'|([^\s"']+?\.(?:png|jpe?g|gif|webp|bmp|tiff?)))(?=$|[\s,;:)])''',
    re.IGNORECASE,
)
ACTION_RE = re.compile(
    r"읽(?:어|어봐)?|봐|확인(?:해|해줘)?|열어|분석(?:해|해줘)?|"
    r"read|view|look|inspect|open|analy[sz]e",
    re.IGNORECASE,
)
IMAGE_WORD_RE = re.compile(
    r"이미지|그림|사진|스크린샷|화면[ ]?캡처|image|picture|photo|screenshot",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(r"읽지[ ]?마|보지[ ]?마|열지[ ]?마|말아|않[아-힣 ]*|don't|do not", re.IGNORECASE)
BASH_READER_RE = re.compile(
    r"(?:^|[;&|()\n]\s*)(?:[\w./-]+/)?"
    r"(?:cat|base64|xxd|head|tail|od|hexdump|strings|python3?|node|perl|ruby)\b",
    re.IGNORECASE,
)


def _state_dir() -> Path:
    return Path.home() / ".claude" / "hooks" / "state"


def _session_id(payload: dict[str, Any]) -> str | None:
    raw = payload.get("session_id") or payload.get("sessionId")
    if not isinstance(raw, str) or not raw:
        return None
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


def _state_path(kind: str, session_id: str) -> Path:
    return _state_dir() / f"image_{kind}-{session_id}.json"


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value if isinstance(value, dict) else default


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _normalise_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _image_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in IMAGE_PATH_RE.finditer(text):
        path = next((part for part in match.groups() if part is not None), "")
        normalised = _normalise_path(path)
        if normalised not in paths:
            paths.append(normalised)
    return paths


def _is_image_path(value: str) -> bool:
    return value.lower().endswith(IMAGE_EXTENSIONS)


def _deny() -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": DENY_REASON,
                }
            },
            ensure_ascii=False,
        )
    )


def _is_explicit_request(text: str, paths: list[str]) -> bool:
    return bool(
        ACTION_RE.search(text)
        and not NEGATION_RE.search(text)
        and (paths or IMAGE_WORD_RE.search(text))
    )


def _submit_prompt(payload: dict[str, Any]) -> None:
    session_id = _session_id(payload)
    if session_id is None:
        return
    prompt = payload.get("prompt") or payload.get("user_prompt") or payload.get("message")
    if not isinstance(prompt, str):
        return
    permit_path = _state_path("permit", session_id)
    paths = _image_paths(prompt)
    if not _is_explicit_request(prompt, paths):
        permit_path.unlink(missing_ok=True)
        return
    _write_json(
        permit_path,
        {"allowed_paths": paths, "consumed_paths": [], "remaining": 5},
    )


def _stop(payload: dict[str, Any]) -> None:
    session_id = _session_id(payload)
    if session_id is not None:
        _state_path("permit", session_id).unlink(missing_ok=True)


def _consume_permit(payload: dict[str, Any], image_path: str) -> bool:
    session_id = _session_id(payload)
    if session_id is None:
        return False
    permit_path = _state_path("permit", session_id)
    permit = _read_json(permit_path, {})
    allowed_paths = permit.get("allowed_paths", [])
    consumed_paths = permit.get("consumed_paths", [])
    if not isinstance(allowed_paths, list) or not isinstance(consumed_paths, list):
        return False
    seen_path = _state_path("seen", session_id)
    seen = _read_json(seen_path, {"paths": []})
    seen_paths = seen.get("paths", [])
    if not isinstance(seen_paths, list):
        seen_paths = []
    if allowed_paths:
        allowed = image_path in allowed_paths and image_path not in consumed_paths
    else:
        allowed = (
            isinstance(permit.get("remaining"), int)
            and permit["remaining"] > 0
            and image_path not in consumed_paths
            and image_path not in seen_paths
        )
    if not allowed:
        return False
    consumed_paths.append(image_path)
    permit["consumed_paths"] = consumed_paths
    if not allowed_paths:
        permit["remaining"] -= 1
    _write_json(permit_path, permit)
    if image_path not in seen_paths:
        seen_paths.append(image_path)
        _write_json(seen_path, {"paths": seen_paths})
    return True


def _pre_tool_use(payload: dict[str, Any]) -> None:
    if os.environ.get("TH_ALLOW_IMAGES") == "1":
        return
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    if tool_name == "Read":
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str) and _is_image_path(file_path):
            if not _consume_permit(payload, _normalise_path(file_path)):
                _deny()
        return
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not BASH_READER_RE.search(command):
            return
        image_paths = _image_paths(command)
        if image_paths and not all(_consume_permit(payload, path) for path in image_paths):
            _deny()


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    if event == "UserPromptSubmit":
        _submit_prompt(payload)
    elif event in {"Stop", "StopFailure"}:
        _stop(payload)
    elif event == "PreToolUse":
        _pre_tool_use(payload)


if __name__ == "__main__":
    main()
