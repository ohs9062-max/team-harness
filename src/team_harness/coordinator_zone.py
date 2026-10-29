"""One shared definition of paths reserved for the harness coordinator.

The stdlib-only Claude hook imports this module through the launcher's source
path.  Keep this module dependency-free so the guard is usable before a
project environment has been activated.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
from typing import Iterable

WORKER_PROMPT_HEADER = (
    "너는 team-harness 워커다(TH_ROLE=worker). 코디네이터가 아니다. "
    "이 지시서의 작업만 하고 결과는 결과 폴더에만 남긴다."
)
DENY_REASON = (
    "코디네이터 영역 또는 다른 작업 폴더입니다. 이 경로 없이 진행하세요 "
    "(자기 작업 트리·작업 폴더는 허용). 지시서에 있는 내용만 쓰고, "
    "다른 방법으로도 막히면 그때 FAILED에 거부 문구를 적으세요."
)


def _resolved(path: str | Path, cwd: str | Path | None = None) -> Path:
    value = os.path.expanduser(str(path))
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(cwd or os.getcwd()) / candidate
    return candidate.resolve(strict=False)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def parse_allowed_paths(value: str | None) -> tuple[Path, ...]:
    """Decode launcher's JSON envelope without treating malformed data as trust."""

    try:
        paths = json.loads(value or "[]")
    except json.JSONDecodeError:
        return ()
    if not isinstance(paths, list):
        return ()
    return tuple(_resolved(path) for path in paths if isinstance(path, str))


def coordinator_zone_paths(*, home: str | Path | None = None) -> tuple[Path, ...]:
    root = _resolved(home or Path.home())
    return (
        root / ".claude",
        root / ".codex" / "AGENTS.md",
        root / ".team-harness" / "watch",
        root / ".team-harness" / "worker_suffix.md",
    )


def is_coordinator_zone(
    path: str | Path,
    *,
    workspace: str | Path | None = None,
    output_dir: str | Path | None = None,
    allowed_paths: Iterable[str | Path] = (),
    cwd: str | Path | None = None,
    original: str | Path | None = None,
) -> bool:
    """Whether a worker must not read or write *path*.

    Explicit task front-matter paths win, including a directory and its
    children.  The task's own output directory is the only output directory a
    worker may touch; source and ordinary data files remain outside this guard.
    """

    target = _resolved(path, cwd)
    variants = (target, _resolved(original, cwd)) if original is not None else (target,)
    if any(
        _within(value, _resolved(item, cwd))
        for value in variants
        for item in allowed_paths
    ):
        return False
    if ".claude" in target.parts:
        return True
    home = _resolved(Path.home())
    if any(_within(target, zone) for zone in coordinator_zone_paths(home=home)):
        return True
    roots = [
        _resolved(root, cwd)
        for root in (workspace, cwd or os.getcwd())
        if root is not None
    ]
    for ws in roots:
        for skill in (
            ws / "skills" / "orca-harness-protocol",
            ws / "skills" / "orchestration",
        ):
            if _within(target, skill):
                return True
        for output_root in (ws / "_outputs", ws / "outputs" / "runs"):
            if _within(target, output_root):
                own = _resolved(output_dir, cwd) if output_dir else None
                return own is None or not any(_within(value, own) for value in variants)
    return False


_PATH_TOKEN = re.compile(
    r"(?:~[/\\][^\s'\";|&]+|/[^\s'\";|&]+|(?:\./)?(?:\.claude|_outputs|outputs/runs|skills)[/\\][^\s'\";|&]+|(?:\.\.?[/\\])+(?:[^\s'\";|&/\\]+[/\\])*(?:\.claude|_outputs|outputs[/\\]runs|skills)[/\\][^\s'\";|&]+)"
)

_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|([\w.-]+))")
_EXECUTOR = re.compile(
    r"(?:^|[\s;|&(])(?:\S*/)?(?:ba)?sh(?:\s|$)|(?:^|[\s;|&(])(?:\S*/)?(?:python3?|node)(?:\s|$)"
)
_PATCH_HEADER = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$"
)


def _command_text_and_patch_paths(command: str) -> tuple[str, list[str]]:
    """Exclude literal input bodies while retaining executable and patch paths."""
    lines = command.splitlines(keepends=True)
    kept: list[str] = []
    patch_paths: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
    in_patch = False
    for line in lines:
        stripped = line.rstrip("\r\n")
        if pending:
            delimiter, tabs, executable = pending[0]
            if (stripped.lstrip("\t") if tabs else stripped) == delimiter:
                pending.pop(0)
                continue
            if not executable:
                if stripped == "*** Begin Patch":
                    in_patch = True
                elif in_patch and stripped == "*** End Patch":
                    in_patch = False
                elif in_patch and (header := _PATCH_HEADER.match(stripped)):
                    patch_paths.append(header.group(1))
                continue
            kept.append(line)
            continue
        if stripped == "*** Begin Patch":
            in_patch = True
            continue
        if in_patch:
            if stripped == "*** End Patch":
                in_patch = False
            elif header := _PATCH_HEADER.match(stripped):
                patch_paths.append(header.group(1))
            continue
        kept.append(line)
        if not pending:
            for match in _HEREDOC.finditer(line):
                prefix = line[: match.start()]
                pending.append(
                    (
                        match.group(2) or match.group(3) or match.group(4),
                        bool(match.group(1)),
                        bool(_EXECUTOR.search(prefix)),
                    )
                )
    return "".join(kept), patch_paths


# Only discard arguments when the shell syntax and option arity are clear.
_SHELL_WORD = re.compile(r"(?:[^\s;|&'\"\\]+|\\[^\n]|'[^']*'|\"(?:[^\"\\]|\\.)*\")+")
_SEARCH_COMMANDS = {"rg", "grep", "egrep", "fgrep", "ag", "sed", "awk", "perl"}
_SIMPLE_OPTIONS = {
    "rg": set("nHiIilLsvwFxqocPhzuaU0N"),
    "grep": set("nHiIilLsvwFxqocEhzZbUrR"),
    "egrep": set("nHiIilLsvwFxqoczh"),
    "fgrep": set("nHiIilLsvwFxqoczh"),
    "ag": set("nHiIilLsvwFxqocQ"),
    "sed": set("nEu"),
    "awk": set(),
    "perl": set("npal"),
}


def _without_search_patterns(command: str) -> str:
    """Mask static pattern arguments; leave uncertain shell syntax untouched."""
    chars = list(command)
    parts = re.finditer(
        r"(?:[^\s;|&'\"\\]+|\\[^\n]|'[^']*'|\"(?:[^\"\\]|\\.)*\")+|[;|&\n]+|\s+|.",
        command,
    )
    segments: list[list[re.Match[str]]] = [[]]
    for part in parts:
        value = part.group()
        if value.isspace() and "\n" not in value:
            continue
        if re.fullmatch(r"[;|&\n]+", value):
            segments.append([])
        else:
            segments[-1].append(part)
    for segment in segments:
        if not segment or any(not _SHELL_WORD.fullmatch(m.group()) for m in segment):
            continue
        if any(any(c in m.group() for c in "$`<>\n") for m in segment):
            continue
        try:
            words = [shlex.split(m.group())[0] for m in segment]
        except (ValueError, IndexError):
            continue
        name = os.path.basename(words[0])
        if name not in _SEARCH_COMMANDS:
            continue
        scripts: list[int] = []
        has_script = False
        end_options = False
        valid = True
        i = 1
        while i < len(words):
            word = words[i]
            if not end_options and word == "--":
                end_options = True
            elif not end_options and word in ("-e", "--regexp", "--expression"):
                if (
                    i + 1 >= len(words)
                    or (
                        word == "--regexp"
                        and name not in {"rg", "grep", "egrep", "fgrep", "ag"}
                    )
                    or (word == "--expression" and name != "sed")
                ):
                    valid = False
                    break
                i += 1
                scripts.append(i)
                has_script = True
            elif (
                not end_options
                and name == "perl"
                and word.startswith("-")
                and word.endswith("e")
                and set(word[1:-1]) <= _SIMPLE_OPTIONS[name]
            ):
                if i + 1 >= len(words):
                    valid = False
                    break
                i += 1
                scripts.append(i)
                has_script = True
            elif not end_options and word in ("-f", "--file"):
                if i + 1 >= len(words):
                    valid = False
                    break
                i += 1
                has_script = True
            elif (
                not end_options
                and word.startswith("--regexp=")
                and name in {"rg", "grep", "egrep", "fgrep", "ag"}
            ):
                scripts.append(i)
                has_script = True
            elif (
                not end_options
                and word.startswith("-e")
                and len(word) > 2
                and name != "awk"
            ):
                scripts.append(i)
                has_script = True
            elif not end_options and word.startswith("-") and word != "-":
                if word.startswith("--") or not set(word[1:]) <= _SIMPLE_OPTIONS[name]:
                    valid = False
                    break
            elif not has_script:
                scripts.append(i)
                has_script = True
            i += 1
        if valid:
            for index in scripts:
                match = segment[index]
                chars[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


def coordinator_path_pairs_in_command(
    command: str, *, cwd: str | Path | None = None
) -> list[tuple[Path, Path]]:
    """Return cleaned paths alongside their original spelling for allowances."""
    command_text, patch_paths = _command_text_and_patch_paths(command)
    pairs = [(_resolved(path, cwd), _resolved(path, cwd)) for path in patch_paths]
    for token in _PATH_TOKEN.findall(_without_search_patterns(command_text)):
        raw = re.split(r"\\[ntr]", token, maxsplit=1)[0]
        clean = raw
        while clean and clean[-1] in ".,;:!?)]}>'\"`\\":
            if clean.endswith(("/.", "/..", "./")):
                break
            clean = clean[:-1]
        original_path = _resolved(raw, cwd)
        cleaned_path = _resolved(clean, cwd)
        match = re.search(r"(?:으로|에|을|를|은|는|이|가|로|의|와|과|도)$", clean)
        if match and match.start() > clean.rfind("/") + 1:
            candidate = _resolved(clean[: match.start()], cwd)
            if candidate.exists() or not cleaned_path.exists():
                cleaned_path = candidate
        pairs.append((cleaned_path, original_path))
    return pairs


def coordinator_paths_in_command(
    command: str, *, cwd: str | Path | None = None
) -> list[Path]:
    """Return plausible cleaned path arguments in a shell command."""
    return [path for path, _ in coordinator_path_pairs_in_command(command, cwd=cwd)]
