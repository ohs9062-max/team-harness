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
import shlex
import sys
from typing import Any


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")
DENY_REASON = "이미지 읽기 차단: 사용자가 명시적으로 읽으라고 한 경우에만 허용"
IMAGE_PATH_RE = re.compile(
    r"""(?:"([^"]+?\.(?:png|jpe?g|gif|webp|bmp|tiff?))"|'([^']+?\.(?:png|jpe?g|gif|webp|bmp|tiff?))'|([^\s"']+?\.(?:png|jpe?g|gif|webp|bmp|tiff?)))(?=$|[\s,;:)])""",
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
NEGATION_RE = re.compile(
    r"읽지[ ]?마|보지[ ]?마|열지[ ]?마|말아|않[아-힣 ]*|don't|do not", re.IGNORECASE
)
BASH_READER_RE = re.compile(
    r"(?:^|[;&|()\n]\s*)(?:[\w./-]+/)?"
    r"(?:cat|base64|xxd|head|tail|od|hexdump|strings|python3?|node|perl|ruby)\b",
    re.IGNORECASE,
)
# Bash is judged by what a command actually reads: an image path is blocked only
# as an argument of a byte reader, or inside inline interpreter code that opens
# it. Script bodies written through a heredoc, output paths, and scripts that
# merely save images are not reads and must pass.
READER_COMMANDS = {"cat", "base64", "xxd", "head", "tail", "od", "hexdump", "strings"}
INTERPRETER_RE = re.compile(r"(?:python[\d.]*|node|perl|ruby)", re.IGNORECASE)
COMMAND_WRAPPERS = {
    "sudo",
    "env",
    "time",
    "timeout",
    "nice",
    "command",
    "exec",
    "do",
    "then",
    "else",
    "elif",
    "if",
    "while",
    "until",
    "!",
    "{",
}
INLINE_CODE_FLAGS = {"-c", "-e", "-pe", "-ne"}
# Only bytes that can reach stdout cost tokens: base64 encoding, or a raw open()
# of a literal image path. cv2.imread/Image.open and open(variable) do not qualify.
_IMAGE_LITERAL = r"""[rbf]*['"][^'"\n]*\.(?:png|jpe?g|gif|webp|bmp|tiff?)['"]"""
INLINE_READ_RE = re.compile(
    r"base64|b64encode"
    rf"|(?<![.\w])open\s*\(\s*{_IMAGE_LITERAL}"
    rf"|(?:readFileSync|readFile)\s*\(\s*{_IMAGE_LITERAL}"
    rf"|Path\s*\(\s*{_IMAGE_LITERAL}\s*\)\s*\.\s*read_bytes",
    re.IGNORECASE,
)
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([\w.-]+))")
SHELL_OPERATOR_CHARS = ";&|()<>`\n"
VISIBLE_SUFFIX_RE = re.compile(r"\.\w{1,8}$")


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
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
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


def _command_name(tokens: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = os.path.basename(token).lower()
        is_assignment = re.fullmatch(r"\w+=.*", token, re.DOTALL) is not None
        if (
            is_assignment
            or name in COMMAND_WRAPPERS
            or token.startswith("-")
            or token.isdigit()
        ):
            index += 1
            continue
        return name, tokens[index + 1 :]
    return "", []


def _shell_segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_OPERATOR_CHARS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = [[]]
    skip_next = False
    for token in lexer:
        if skip_next:
            skip_next = False
            continue
        if token and set(token) <= set(SHELL_OPERATOR_CHARS):
            if ">" in token or token.startswith("<<"):
                skip_next = True  # output target or heredoc delimiter: not a read
            elif "<" not in token:
                segments.append([])
            continue
        segments[-1].append(token)
    return [segment for segment in segments if segment]


def _split_heredocs(command: str) -> tuple[str, list[str]]:
    """Return the command without heredoc bodies, plus bodies fed to an interpreter."""
    lines = command.split("\n")
    outer: list[str] = []
    inline: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        outer.append(line)
        index += 1
        for match in HEREDOC_RE.finditer(line):
            delimiter = next(group for group in match.groups() if group is not None)
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != delimiter:
                body.append(lines[index])
                index += 1
            index += 1
            names = [_command_name(segment)[0] for segment in _shell_segments(line)]
            if any(INTERPRETER_RE.fullmatch(name) for name in names):
                inline.append("\n".join(body))
    return "\n".join(outer), inline


def _bash_image_reads(command: str) -> list[str]:
    """Return image paths this command really reads; unparsable input stays blocked."""
    lowered = command.lower()
    if not any(extension in lowered for extension in IMAGE_EXTENSIONS):
        return []
    try:
        outer, inline = _split_heredocs(command)
        segments = _shell_segments(outer)
    except ValueError:
        return _image_paths(command) if BASH_READER_RE.search(command) else []
    paths: list[str] = []
    named_images = [
        value for segment in segments for value in segment if _is_image_path(value)
    ]
    for segment in segments:
        name, arguments = _command_name(segment)
        if name in READER_COMMANDS:
            paths.extend(
                _normalise_path(value) for value in arguments if _is_image_path(value)
            )
            # `IMG=a.png; base64 $IMG`: a reader of a variable may be reading a
            # named image, unless the argument visibly names another file type.
            if any(
                "$" in value and not VISIBLE_SUFFIX_RE.search(value)
                for value in arguments
            ):
                paths.extend(_normalise_path(value) for value in named_images)
        elif INTERPRETER_RE.fullmatch(name):
            for position, value in enumerate(arguments[:-1]):
                if value in INLINE_CODE_FLAGS:
                    inline.append(arguments[position + 1])
    for code in inline:
        if INLINE_READ_RE.search(code):
            paths.extend(_image_paths(code))
    return list(dict.fromkeys(paths))


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
    prompt = (
        payload.get("prompt") or payload.get("user_prompt") or payload.get("message")
    )
    if not isinstance(prompt, str):
        return
    permit_path = _state_path("permit", session_id)
    paths = _image_paths(prompt)
    if not _is_explicit_request(prompt, paths):
        permit_path.unlink(missing_ok=True)
        return
    _write_json(
        permit_path, {"allowed_paths": paths, "consumed_paths": [], "remaining": 5}
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
        if not isinstance(command, str):
            return
        image_paths = _bash_image_reads(command)
        if image_paths and not all(
            _consume_permit(payload, path) for path in image_paths
        ):
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
