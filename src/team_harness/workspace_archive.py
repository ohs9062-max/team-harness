"""Conservative, non-deleting archival of workspace watch data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True)
class Move:
    source: Path
    destination: Path


def watch_dir() -> Path:
    return Path(
        os.environ.get("TH_WATCH_DIR", str(Path.home() / ".team-harness/watch"))
    )


def _destination(path: Path, now: datetime) -> Path:
    if not path.exists():
        return path
    candidate = path.with_name(f"{path.name}.{now:%H%M%S}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{now:%H%M%S}.{index}")
        index += 1
    return candidate


def _move(
    src: Path, dst: Path, now: datetime, dry_run: bool, moves: list[Move], log: Path
) -> Path:
    dst = _destination(dst, now)
    moves.append(Move(src, dst))
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as out:
            out.write(f"{now.isoformat(sep=' ', timespec='seconds')} | {src} → {dst}\n")
    return dst


def _inbox(
    root: Path, now: datetime, dry_run: bool, moves: list[Move], log: Path
) -> None:
    inbox = root / "inbox.md"
    if not inbox.is_file():
        return
    with (root / ".inbox_cursor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = inbox.read_bytes()
        cursors = [
            p
            for p in [root / ".inbox_cursor", *(root / ".inbox_cursor.d").glob("*")]
            if p.is_file()
        ]
        # Without a cursor, or with unread alert bookkeeping, preserve everything.
        alert = (
            watch_dir()
            / "inbox_unread"
            / (hashlib.sha256(str(root.parent).encode()).hexdigest() + ".tsv")
        )
        if not cursors or (alert.exists() and alert.stat().st_size):
            return
        try:
            positions = [int(p.read_text().strip()) for p in cursors]
        except (OSError, ValueError):
            return
        if any(p < 0 or p > len(data) for p in positions):
            return
        end = 0
        chunks: dict[str, bytes] = {}
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n") or end + len(line) > min(positions):
                break
            try:
                stamp = datetime.strptime(line[:16].decode("ascii"), "%Y-%m-%d %H:%M")
                if now.tzinfo:
                    stamp = stamp.replace(tzinfo=now.tzinfo)
            except (ValueError, UnicodeError):
                break
            if now - stamp < timedelta(days=3):
                break
            key = stamp.strftime("%Y%m")
            chunks[key] = chunks.get(key, b"") + line
            end += len(line)
        if not end:
            return
        routes = root / "inbox.routes"
        route_data = routes.read_text() if routes.exists() else ""
        adjusted = []
        archived_routes = []
        for line in route_data.splitlines():
            try:
                raw, route = line.split("\t", 1)
                offset = int(raw)
            except ValueError:
                return
            if offset < end:
                archived_routes.append(line + "\n")
            else:
                adjusted.append(f"{offset - end}\t{route}\n")
        for month, chunk in chunks.items():
            target = root / "archive" / f"inbox_{month}.md"
            moves.append(Move(inbox, target))
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("ab") as out:
                    out.write(chunk)
                with log.open("a", encoding="utf-8") as out:
                    out.write(
                        f"{now.isoformat(sep=' ', timespec='seconds')} | {inbox} → {target}\n"
                    )
        if archived_routes:
            route_target = root / "archive" / f"inbox_routes_{now:%Y%m}.txt"
            moves.append(Move(routes, route_target))
            if not dry_run:
                with route_target.open("a", encoding="utf-8") as out:
                    out.writelines(archived_routes)
                with log.open("a", encoding="utf-8") as out:
                    out.write(
                        f"{now.isoformat(sep=' ', timespec='seconds')} | {routes} → {route_target}\n"
                    )
        if not dry_run:
            inbox.write_bytes(data[end:])
            if routes.exists():
                routes.write_text("".join(adjusted))
            for cursor, pos in zip(cursors, positions, strict=True):
                cursor.write_text(f"{pos - end}\n")


def _alive(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        pid = int(path.read_text().strip())
        if pid <= 0:
            return True
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (ValueError, OSError):
        return True


def _eligible(task: Path, owner: Path, now: datetime) -> bool:
    if not task.is_dir() or task.is_symlink():
        return False
    try:
        # Require explicit owner and an output directory, never arbitrary project data.
        if Path((task / ".th_owner").read_text().strip()).resolve() != owner.resolve():
            return False
        if "_outputs" not in task.parts or "_archive" in task.parts:
            return False
        if not all((task / name).is_file() for name in ("DONE", "VERIFIED")):
            return False
        if any((task / name).exists() for name in ("FAILED", "VERIFY_FAILED")):
            return False
        if any(_alive(task / name) for name in ("worker.pid", "job.pid")):
            return False
        latest = max(p.lstat().st_mtime for p in [task, *task.rglob("*")])
        if now.timestamp() - latest < 7 * 86400:
            return False
        wtfile = task / ".th_worktree"
        if wtfile.exists():
            wt = Path(wtfile.read_text().strip())
            if not wt.is_absolute():
                return False
            if wt.exists():
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(wt),
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode or result.stdout:
                    return False
        return True
    except OSError:
        return False


def archive_workspace(
    owner_root: Path, now: datetime, dry_run: bool = False
) -> list[Move]:
    owner = Path(owner_root).resolve()
    root = owner / ".claude"
    log = root / "archive/archive.log"
    moves: list[Move] = []
    if root.is_dir():
        _inbox(root, now, dry_run, moves, log)
        for src in [*root.glob("work_log_*.md"), root / "workers_archive.md"]:
            if src.is_file() and now.timestamp() - src.stat().st_mtime >= 7 * 86400:
                _move(src, root / "archive" / src.name, now, dry_run, moves, log)
    registry = watch_dir() / "tasks.txt"
    if registry.is_file():
        with (watch_dir() / "tasks.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            lines = registry.read_text().splitlines()
            changed = False
            for i, line in enumerate(lines):
                task = Path(line)
                if line and _eligible(task, owner, now):
                    dest = _move(
                        task,
                        task.parent / "_archive" / now.strftime("%Y%m") / task.name,
                        now,
                        dry_run,
                        moves,
                        log,
                    )
                    lines[i] = str(dest)
                    changed = True
            if not dry_run and changed:
                registry.write_text("".join(line + "\n" for line in lines))
    events = watch_dir() / "events.log"
    if events.is_file() and events.stat().st_size > 5 * 1024 * 1024:
        _move(
            events, events.with_name(f"events_{now:%Y%m}.log"), now, dry_run, moves, log
        )
    return moves


def archive_daily(
    now: datetime, owner: Path | None = None, dry_run: bool = False
) -> list[Move]:
    wd = watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    with (wd / "archive.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        last = wd / "archive.last"
        try:
            if now.timestamp() - float(last.read_text()) < 86400:
                return []
        except (OSError, ValueError):
            pass
        owners: set[Path] = set()
        if owner is not None:
            owners.add(owner.resolve())
        registry = wd / "tasks.txt"
        if registry.exists():
            for line in registry.read_text().splitlines():
                try:
                    value = (Path(line) / ".th_owner").read_text().strip()
                    if value:
                        owners.add(Path(value).resolve())
                except OSError:
                    continue
        moves = []
        for root in sorted(owners):
            moves.extend(archive_workspace(root, now, dry_run))
        if not dry_run:
            last.write_text(str(now.timestamp()))
        return moves


def archive_on_start(owner: Path) -> None:
    """Run the shared daily check without blocking the caller on failure."""
    try:
        archive_daily(datetime.now().astimezone(), owner)
    except Exception as exc:
        try:
            wd = watch_dir()
            wd.mkdir(parents=True, exist_ok=True)
            with (wd / "events.log").open("a") as out:
                out.write(f"{datetime.now().isoformat()} archive failed: {exc}\n")
        except OSError:
            import logging

            logging.getLogger(__name__).exception("Unable to record archive failure")


if __name__ == "__main__":
    try:
        archive_daily(datetime.now().astimezone())
    except Exception as exc:
        wd = watch_dir()
        wd.mkdir(parents=True, exist_ok=True)
        with (wd / "events.log").open("a") as out:
            out.write(f"{datetime.now().isoformat()} archive failed: {exc}\n")
        raise
