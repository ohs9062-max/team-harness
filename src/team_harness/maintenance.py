"""Independent workspace maintenance; never block the event watcher."""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from team_harness.workspace_archive import watch_dir


def start(owner: Path | None = None) -> None:
    """Ensure one background maintainer, preserving unrelated worker processes."""
    if os.environ.get("TH_NO_MAINTENANCE") == "1" or os.environ.get("TH_ONCE") == "1":
        return
    wd = watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    if owner is not None:
        with (wd / "maintenance_owners.jsonl").open("a+", encoding="utf-8") as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            out.seek(0)
            previous = out.read()
            resolved = owner.resolve()
            if resolved not in _parse_owners(previous):
                if previous and not previous.endswith("\n"):
                    out.write("\n")
                out.write(json.dumps(str(resolved)) + "\n")
    with (wd / "maintenance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Hold the singleton lock across spawn by passing its descriptor.
        with (wd / "maintenance.log").open("ab") as log:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "team_harness.maintenance",
                    "--lock-fd",
                    str(lock.fileno()),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                pass_fds=(lock.fileno(),),
            )


def _parse_owners(text: str) -> set[Path]:
    result = set()
    for line in text.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, str) and value.strip():
                result.add(Path(value).resolve())
        except (ValueError, OSError):
            continue
    return result


def registered_owners(wd: Path | None = None) -> set[Path]:
    try:
        return _parse_owners(
            ((wd or watch_dir()) / "maintenance_owners.jsonl").read_text(
                encoding="utf-8", errors="replace"
            )
        )
    except FileNotFoundError:
        return set()


def run_once() -> None:
    from team_harness import docs_migrate
    from team_harness.deployment import auto_sync
    from team_harness.loss_report import daily
    from team_harness.workspace_archive import archive_daily

    now = datetime.now().astimezone()
    if os.environ.get("TH_STATE_DB") != "0":
        try:
            from team_harness.state_index import update

            update()
        except Exception as exc:
            print(f"maintenance state index failed: {exc}", file=sys.stderr)
    for name, operation in (
        ("deployment", auto_sync),
        ("report", lambda: daily(now)),
        ("archive", lambda: archive_daily(now)),
        ("documents", lambda: docs_migrate.watch(daily=True)),
    ):
        try:
            operation()
        except Exception as exc:
            with (watch_dir() / "events.log").open("a", encoding="utf-8") as log:
                log.write(f"{now.isoformat()} maintenance {name} failed: {exc}\n")


def signature() -> list[tuple[str, int]]:
    root = Path(__file__).resolve().parent
    return [(str(p), p.stat().st_mtime_ns) for p in sorted(root.rglob("*.py"))]


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--start":
        start()
        return
    lock_fd = int(sys.argv[sys.argv.index("--lock-fd") + 1])
    os.set_inheritable(lock_fd, True)
    previous = signature()
    while True:
        run_once()
        time.sleep(30)
        if signature() != previous:
            os.execv(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "team_harness.maintenance",
                    "--lock-fd",
                    str(lock_fd),
                ],
            )


if __name__ == "__main__":
    main()
