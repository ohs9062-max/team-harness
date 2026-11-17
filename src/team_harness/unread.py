"""Shared unread-owner classification and conservative maintenance."""

import fcntl
import os
from pathlib import Path
import stat
import sys
import tempfile
import time


def owner_state(owner: str) -> str:
    """Only a confirmed missing owner is orphaned; errors are indeterminate."""
    try:
        mode = os.stat(owner).st_mode
    except FileNotFoundError:
        return "orphan"
    except OSError:
        return "unknown"
    return "valid" if stat.S_ISDIR(mode) else "unknown"


def read_confirmed(owner: str, route: str, size: int) -> bool:
    root = Path(owner) / ".claude"
    paths = [root / ".inbox_cursor.d" / route]
    if route == "*":
        try:
            paths = [root / ".inbox_cursor", *(root / ".inbox_cursor.d").glob("*")]
        except OSError:
            return False
    for path in paths:
        try:
            if int(path.read_text().strip()) >= size:
                return True
        except (OSError, ValueError):
            continue
    return False


def clean_unread(
    watch: Path, *, dry_run: bool = False, now: float | None = None
) -> dict[str, int]:
    """Remove confirmed reads and archive expired orphans under the writer lock."""
    try:
        days = max(0, int(os.environ.get("TH_UNREAD_ORPHAN_DAYS", "30")))
    except ValueError:
        days = 30
    now = time.time() if now is None else now
    counts = {"read": 0, "orphan": 0}
    for state in sorted((watch / "inbox_unread").glob("*.tsv")):
        # Dry run must not create lock files or any other filesystem changes.
        if dry_run:
            lines = state.read_text(encoding="utf-8").splitlines(keepends=True)
            _clean_lines(lines, counts, days, now)
            continue
        with state.with_suffix(state.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                lines = state.read_text(encoding="utf-8").splitlines(keepends=True)
            except FileNotFoundError:
                continue
            kept, archived = _clean_lines(lines, counts, days, now)
            if len(kept) == len(lines):
                continue
            if archived:
                archive = watch / "archive"
                archive.mkdir(parents=True, exist_ok=True)
                with (archive / f"unread_orphans_{state.name}").open(
                    "a", encoding="utf-8"
                ) as out:
                    out.writelines(archived)
                    out.flush()
                    os.fsync(out.fileno())
            fd, tmp = tempfile.mkstemp(dir=state.parent, prefix=".unread-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    out.writelines(kept)
                os.replace(tmp, state)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    return counts


def _clean_lines(
    lines: list[str], counts: dict[str, int], days: int, now: float
) -> tuple[list[str], list[str]]:
    kept, archived = [], []
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        try:
            stamp, size, owner = int(parts[0]), int(parts[1]), parts[3]
            route = parts[4] if len(parts) > 4 and parts[4] else "*"
        except (IndexError, ValueError):
            kept.append(line)
            continue
        state = owner_state(owner)
        if state == "valid" and read_confirmed(owner, route, size):
            counts["read"] += 1
        elif state == "orphan" and now - stamp >= days * 86400:
            counts["orphan"] += 1
            archived.append(line)
        else:
            kept.append(line)
    return kept, archived


if __name__ == "__main__":
    print(owner_state(sys.argv[1]))
