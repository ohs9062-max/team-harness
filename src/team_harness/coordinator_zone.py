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
from typing import Iterable

WORKER_PROMPT_HEADER = (
    "너는 team-harness 워커다(TH_ROLE=worker). 코디네이터가 아니다. "
    "이 지시서의 작업만 하고 결과는 결과 폴더에만 남긴다."
)
DENY_REASON = "코디네이터 영역입니다. 필요하면 FAILED에 이유를 쓰세요"


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


def coordinator_path_pairs_in_command(
    command: str, *, cwd: str | Path | None = None
) -> list[tuple[Path, Path]]:
    """Return cleaned paths alongside their original spelling for allowances."""
    pairs = []
    for token in _PATH_TOKEN.findall(command):
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
