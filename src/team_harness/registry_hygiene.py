"""Conservative daily retirement of watch registry entries."""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import tempfile

from team_harness.quota_lock import lock_expiry
from team_harness.workspace_archive import _alive


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def _write(path: Path, lines: list[str]) -> None:
    fd, name = tempfile.mkstemp(prefix=".registry-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.writelines(line + "\n" for line in lines)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _finished(task: Path) -> bool:
    return (
        (task / "DONE").is_file()
        and any((task / x).is_file() for x in ("VERIFIED", "VERIFY_NONE"))
        and not any(
            (task / x).exists()
            for x in ("FAILED", "VERIFY_FAILED", "context_exit_requested.json")
        )
        and not list(task.glob("FAILED.*"))
        and not list(task.glob(".th_daemon.handoff_wait.*"))
        and not any(_alive(task / x) for x in ("worker.pid", "job.pid"))
    )


def clean(wd: Path, now: datetime) -> dict[str, list[str]]:
    wd.mkdir(parents=True, exist_ok=True)
    result: dict[str, list[str]] = {
        x: [] for x in ("missing", "retired", "archived", "quota", "unknown_quota")
    }
    with (wd / "registry_hygiene.lock").open("a") as daily:
        fcntl.flock(daily, fcntl.LOCK_EX)
        stamp = wd / "registry_hygiene.last"
        try:
            if now.timestamp() - float(stamp.read_text()) < 86400:
                return result
        except (OSError, ValueError):
            pass
        try:
            days = max(0, int(os.environ.get("TH_DONE_KEEP_DAYS", "30")))
        except ValueError:
            days = 30
        with (wd / "tasks.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            active, done = _lines(wd / "tasks.txt"), _lines(wd / "tasks_done.txt")
            kept_active, kept_done = [], []
            for line in active:
                if not line:
                    kept_active.append(line)
                elif not Path(line).exists():
                    result["missing"].append(line)
                elif Path(line).is_dir() and _finished(Path(line)):
                    result["retired"].append(line)
                else:
                    kept_active.append(line)
            done += [line for line in result["retired"] if line not in done]
            for line in done:
                task = Path(line)
                if line and not task.exists():
                    result["missing"].append(line)
                    continue
                if not line or not task.is_dir() or not _finished(task):
                    kept_done.append(line)
                    continue
                markers = [task / "DONE"] + [
                    task / x
                    for x in ("VERIFIED", "VERIFY_NONE")
                    if (task / x).is_file()
                ]
                if (
                    now.timestamp() - max(x.stat().st_mtime for x in markers)
                    >= days * 86400
                ):
                    result["archived"].append(line)
                else:
                    kept_done.append(line)
            if result["archived"]:
                archive = wd / "archive"
                archive.mkdir(exist_ok=True)
                target = archive / f"tasks_done_{now:%Y%m%d}.tsv"
                _write(target, _lines(target) + result["archived"])
                # Preserve owner discovery after pruning the active registries.
                from team_harness.loss_report import normalize_owner

                with (wd / "maintenance_owners.jsonl").open(
                    "a+", encoding="utf-8"
                ) as owners:
                    fcntl.flock(owners, fcntl.LOCK_EX)
                    owners.seek(0)
                    previous = owners.read()
                    known = previous.splitlines()
                    for line in result["archived"]:
                        try:
                            value = (Path(line) / ".th_owner").read_text().strip()
                            if not value:
                                continue
                            normalized = normalize_owner(value, wd, log_failure=False)
                            if normalized is None:
                                continue
                            entry = json.dumps(str(normalized))
                            if entry not in known:
                                if previous and not previous.endswith("\n"):
                                    owners.write("\n")
                                owners.write(entry + "\n")
                                previous = entry + "\n"
                                known.append(entry)
                        except OSError:
                            pass
            if (wd / "tasks_done.txt").exists() or done:
                _write(wd / "tasks_done.txt", kept_done)
            if (wd / "tasks.txt").exists():
                _write(wd / "tasks.txt", kept_active)
        with (wd / "quota_locks.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for path in wd.glob("quota_lock.*"):
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    result["unknown_quota"].append(path.name)
                    continue
                try:
                    expiry = lock_expiry(path, content)
                except OSError:
                    result["unknown_quota"].append(path.name)
                    continue
                if expiry <= now.timestamp():
                    result["quota"].append(path.name)
                elif "expires=" not in content:
                    result["unknown_quota"].append(path.name)
        stamp.write_text(str(now.timestamp()), encoding="utf-8")
    return result
