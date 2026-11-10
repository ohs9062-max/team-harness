"""Detect and safely verify workspace rule-document migrations."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess

from team_harness.workspace_archive import watch_dir

ROOT = Path(__file__).resolve().parents[2]
FILES = ("AGENTS.md", "CLAUDE.md")


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True, check=False
    )


def owners() -> set[Path]:
    from team_harness.maintenance import registered_owners

    found = registered_owners()
    for name in ("tasks.txt", "tasks_done.txt"):
        try:
            lines = (watch_dir() / name).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                value = (Path(line) / ".th_owner").read_text().strip()
                if value:
                    found.add(Path(value).resolve())
            except OSError:
                pass
    return found


def main_checkout(root: Path) -> tuple[Path | None, str]:
    common = git(root, "rev-parse", "--git-common-dir")
    if common.returncode:
        return None, "not a repository or checkout missing"
    bare = git(root, "rev-parse", "--is-bare-repository")
    if bare.returncode or bare.stdout.strip() == "true":
        return None, "bare repository"
    path = Path(common.stdout.strip())
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    main = path.parent
    if path.name != ".git" or not main.is_dir() or not (main / ".git").is_dir():
        return None, "main checkout missing or bare repository"
    result = git(main, "rev-parse", "--show-toplevel")
    if result.returncode or Path(result.stdout.strip()).resolve() != main:
        return None, "main checkout missing"
    return main, ""


def event(message: str) -> None:
    wd = watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    with (wd / "events.log").open("a", encoding="utf-8") as out:
        out.write(f"{datetime.now().isoformat()} docs: {message}\n")


def limit() -> int:
    try:
        return max(1, int(os.environ.get("TH_DOCS_MAX_LINES", "300")))
    except ValueError:
        return 300


def alive(task: Path) -> bool:
    try:
        pid = int((task / "worker.pid").read_text().strip())
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def detect(root: Path, now: datetime) -> tuple[dict[str, int], str]:
    if any(x in str(root).lower() for x in ("-virtual", "_snapshot", "snapshot")):
        return {}, "copy path"
    result = git(root, "rev-parse", "--show-toplevel")
    if result.returncode or Path(result.stdout.strip()).resolve() != root.resolve():
        return {}, "not repository root"
    counts = {
        name: len((root / name).read_text(encoding="utf-8").splitlines())
        for name in FILES
        if (root / name).is_file()
    }
    if not any(n > limit() for n in counts.values()):
        return counts, "below limit"
    if any(git(root, "status", "--porcelain", "--", name).stdout for name in FILES):
        return counts, "uncommitted document changes"
    if (root / "docs/archive" / f"AGENTS_이관_{now:%Y%m%d}.md").exists():
        return counts, "today archive already exists"
    tasks = root / ".th_docs"
    if tasks.exists():
        for attempt in tasks.glob("docs_migrate_*"):
            if (attempt / "attempt.json").exists():
                try:
                    data = json.loads((attempt / "attempt.json").read_text())
                    if now.timestamp() - data["time"] < 86400 or (
                        not (attempt / "finished").exists() and alive(attempt)
                    ):
                        return counts, "running or attempted within 24 hours"
                except (ValueError, KeyError, OSError):
                    return counts, "invalid attempt state"
    return counts, "eligible"


def scan(
    roots: set[Path], now: datetime, *, execute: bool, log: bool = True
) -> list[str]:
    rows: list[str] = []
    grouped: dict[Path, int] = {}
    for owner in sorted(roots):
        root, reason = main_checkout(owner)
        if root is None:
            row = f"{owner}: [{reason}]"
            rows.append(row)
            if log:
                event(row)
            continue
        grouped[root] = grouped.get(root, 0) + (owner.resolve() != root)
    for root, count in sorted(grouped.items()):
        try:
            counts, reason = detect(root, now)
            row = (
                f"{root} (워크트리 {count}개에서): "
                + ", ".join(f"{n}={v}" for n, v in counts.items())
                + f" [{reason}]"
            )
            rows.append(row)
            if log:
                event(row)
            if reason == "eligible" and execute:
                launch(root, now)
        except (OSError, UnicodeError, ValueError) as exc:
            row = f"{root}: error {exc}"
            rows.append(row)
            if log:
                event(row)
    return rows


def launch(root: Path, now: datetime) -> None:
    task = root / ".th_docs" / f"docs_migrate_{now:%Y%m%d}"
    task.mkdir(parents=True, exist_ok=False)
    info = git(root, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    exclude = Path(info)
    if not exclude.is_absolute():
        exclude = root / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a+", encoding="utf-8") as out:
        out.seek(0)
        if ".th_docs/" not in out.read().splitlines():
            out.write("\n.th_docs/\n")
    original = {
        name: (root / name).read_text(encoding="utf-8")
        for name in FILES
        if (root / name).is_file()
    }
    worktree = task / "worktree"
    created = git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    if created.returncode:
        raise OSError(f"cannot isolate document migration: {created.stderr}")
    archive_name = f"docs/archive/AGENTS_이관_{now:%Y%m%d}.md"
    (task / "attempt.json").write_text(
        json.dumps(
            {
                "time": now.timestamp(),
                "original": original,
                "archive": archive_name,
                "worktree": str(worktree),
                "targets": [
                    name
                    for name, text in original.items()
                    if len(text.splitlines()) > limit()
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    template = (ROOT / "design/user_config/docs_migrate_task.md").read_text(
        encoding="utf-8"
    )
    (task / "task.md").write_text(
        template.format(
            policy=ROOT / "design/designs/workspace_docs_policy.md",
            owner=worktree,
            date=now.strftime("%Y%m%d"),
            max_lines=limit(),
        ),
        encoding="utf-8",
    )
    cmd = [str(ROOT / "scripts/th-launch.sh"), str(task), "agy"]
    if os.environ.get("TH_DOCS_MODEL"):
        cmd.append(os.environ["TH_DOCS_MODEL"])
    cmd.append(str(worktree))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "TH_WORKSPACE": str(root)},
        )
    except OSError as exc:
        (task / "FAILED").write_text(f"launch failed: {exc}", encoding="utf-8")
        event(f"{root}: launch failed: {exc}")
        return
    if proc.returncode:
        (task / "FAILED").write_text(
            f"launch failed: {proc.stderr or proc.stdout}", encoding="utf-8"
        )
        event(f"{root}: launch failed rc={proc.returncode}")


def normalized(text: str) -> Counter[str]:
    return Counter(" ".join(line.split()) for line in text.splitlines() if line.strip())


def finish(root: Path, task: Path) -> None:
    """Validate an isolated proposal; never restore over a user's current edits.

    Legacy in-place attempts are left untouched on failure. New attempts apply
    only if every original document is still identical and unstaged. The
    proposal remains in its worktree when validation or application fails.
    """
    data = json.loads((task / "attempt.json").read_text(encoding="utf-8"))
    original: dict[str, str] = data["original"]
    if not original or not set(original).issubset(FILES):
        raise ValueError("invalid migration document set")
    source = Path(data.get("worktree", str(root)))
    relative_archive = data["archive"]
    relative = Path(relative_archive)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:2] != ("docs", "archive")
    ):
        raise ValueError("invalid migration archive path")
    if source != root and source.resolve() != (task / "worktree").resolve():
        raise ValueError("migration worktree is outside the task")
    archive = source / relative_archive
    current = {
        name: (source / name).read_text(encoding="utf-8")
        for name in original
        if (source / name).is_file()
    }
    targets = data.get("targets") or list(original)
    old_lines: Counter[str] = Counter()
    new_lines: Counter[str] = Counter()
    for text in original.values():
        old_lines.update(normalized(text))
    for text in current.values():
        new_lines.update(normalized(text))
    if archive.is_file():
        new_lines.update(normalized(archive.read_text(encoding="utf-8")))
    reason = ""
    if any((source / name).is_symlink() for name in original) or archive.is_symlink():
        reason = "document or archive is a symlink"
    elif (task / "FAILED").exists():
        reason = "worker failed"
    elif set(current) != set(original):
        reason = "original document removed"
    elif not any(
        len(current[name].splitlines()) < len(original[name].splitlines())
        for name in targets
    ):
        reason = "target documents did not shrink"
    elif any(
        len(current[name].splitlines()) > len(original[name].splitlines())
        for name in original
    ):
        reason = "document grew"
    elif old_lines - new_lines:
        reason = "original lines missing"
    elif not archive.is_file():
        reason = "no new archive"
    elif any(
        relative_archive not in current[name]
        for name in original
        if len(current[name].splitlines()) < len(original[name].splitlines())
    ):
        reason = "archive reference missing from shortened document"
    paths = [name for name in original if current.get(name) != original[name]]
    paths.append(relative_archive)
    if not reason and source != root:
        # A per-migration lock cannot lock out an editor. Check the actual
        # content and index immediately before git applies its contextual patch.
        if any(
            not (root / name).is_file()
            or (root / name).read_text(encoding="utf-8") != text
            for name, text in original.items()
        ):
            reason = "original documents changed during migration"
        elif git(root, "status", "--porcelain", "--", *paths).stdout:
            reason = "document or archive has uncommitted changes"
        else:
            staged = git(source, "add", "--", *paths)
            patch = git(source, "diff", "--cached", "--binary", "--", *paths)
            if staged.returncode or patch.returncode or not patch.stdout:
                reason = "cannot prepare migration patch"
            else:
                applied = subprocess.run(
                    ["git", "-C", str(root), "apply", "--index", "-"],
                    input=patch.stdout,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if applied.returncode:
                    reason = "original changed or patch could not be applied"
    if not reason:
        result = git(root, "add", "--", *paths)
        if not result.returncode:
            result = git(
                root,
                "commit",
                "--only",
                "-m",
                f"docs: archive rule history ({datetime.fromtimestamp(data['time']):%Y-%m-%d})",
                "--",
                *paths,
            )
        if result.returncode:
            # Preserve the proposed edits and index on commit failure. A blind
            # reset/restore can destroy unrelated concurrent edits or staging.
            reason = "commit failed; edits preserved for inspection"
    if reason:
        message = f"문서 이관 실패 {root.name} — {reason}; proposal: {source}"
    else:
        sha = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
        counts = ", ".join(
            f"{name} {len(original[name].splitlines())}줄→{len(current[name].splitlines())}줄"
            for name in targets
        )
        message = f"문서 이관 {root.name} {counts} 커밋 {sha}"
    inbox = root / ".claude/inbox.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a", encoding="utf-8") as out:
        out.write(message + "\n")
    (task / "finished").write_text(message, encoding="utf-8")
    event(message)


def reap() -> None:
    roots = {
        root for owner in owners() if (root := main_checkout(owner)[0]) is not None
    }
    for root in roots:
        for task in (root / ".th_docs").glob("docs_migrate_*"):
            if (
                (task / "attempt.json").is_file()
                and not (task / "finished").exists()
                and ((task / "DONE").exists() or (task / "FAILED").exists())
            ):
                try:
                    finish(root, task)
                except (OSError, ValueError) as exc:
                    event(f"{root}: verification error {exc}")


def manual(roots: set[Path], now: datetime, *, dry_run: bool) -> list[str]:
    if dry_run:
        return scan(roots, now, execute=False, log=False)
    wd = watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    with (wd / "docs.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return scan(roots, now, execute=True)


def watch(*, daily: bool = False) -> None:
    wd = watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    with (wd / "docs.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reap()
        if daily:
            now = datetime.now().astimezone()
            last = wd / "docs.last"
            try:
                due = now.timestamp() - float(last.read_text()) >= 86400
            except (OSError, ValueError):
                due = True
            if due:
                scan(owners(), now, execute=True)
                last.write_text(str(now.timestamp()))


if __name__ == "__main__":
    import sys

    watch(daily="--daily" in sys.argv)
