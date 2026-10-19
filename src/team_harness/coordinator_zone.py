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
) -> bool:
    """Whether a worker must not read or write *path*.

    Explicit task front-matter paths win, including a directory and its
    children.  The task's own output directory is the only output directory a
    worker may touch; source and ordinary data files remain outside this guard.
    """

    target = _resolved(path, cwd)
    if any(_within(target, _resolved(item, cwd)) for item in allowed_paths):
        return False
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
        for root in (ws / ".claude",):
            if _within(target, root):
                return True
        for output_root in (ws / "_outputs", ws / "outputs" / "runs"):
            if _within(target, output_root):
                own = _resolved(output_dir, cwd) if output_dir else None
                return own is None or not _within(target, own)
    return False


_PATH_TOKEN = re.compile(
    r"(?:~[/\\][^\s'\";|&]+|/[^\s'\";|&]+|(?:\./)?(?:\.claude|_outputs|outputs/runs|skills)[/\\][^\s'\";|&]+)"
)


def coordinator_paths_in_command(
    command: str, *, cwd: str | Path | None = None
) -> list[Path]:
    """Return plausible path arguments in a shell command, conservatively."""

    return [_resolved(token, cwd) for token in _PATH_TOKEN.findall(command)]
