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
    found: set[Path] = set()
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
    archive_name = f"docs/archive/AGENTS_이관_{now:%Y%m%d}.md"
    (task / "attempt.json").write_text(
        json.dumps(
            {"time": now.timestamp(), "original": original, "archive": archive_name},
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
            owner=root,
            date=now.strftime("%Y%m%d"),
            max_lines=limit(),
        ),
        encoding="utf-8",
    )
    cmd = [str(ROOT / "scripts/th-launch.sh"), str(task), "agy"]
    if os.environ.get("TH_DOCS_MODEL"):
        cmd.append(os.environ["TH_DOCS_MODEL"])
    cmd.append(str(root))
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
    data = json.loads((task / "attempt.json").read_text(encoding="utf-8"))
    original: dict[str, str] = data["original"]
    archive = root / data["archive"]
    new = [archive] if archive.is_file() else []
    before = len(original.get("AGENTS.md", "").splitlines())
    current = {
        name: (root / name).read_text(encoding="utf-8")
        for name in original
        if (root / name).is_file()
    }
    after = len(current.get("AGENTS.md", "").splitlines())
    old_lines: Counter[str] = Counter()
    new_lines: Counter[str] = Counter()
    for text in original.values():
        old_lines.update(normalized(text))
    for text in [*current.values(), *(p.read_text(encoding="utf-8") for p in new)]:
        new_lines.update(normalized(text))
    reason = ""
    if (task / "FAILED").exists():
        reason = "worker failed"
    elif (
        not (root / "AGENTS.md").is_file()
        or "AGENTS.md" not in original
        or len(current.get("AGENTS.md", "").splitlines())
        >= len(original["AGENTS.md"].splitlines())
    ):
        reason = "AGENTS.md did not shrink"
    elif old_lines - new_lines:
        reason = "original lines missing"
    elif not new:
        reason = "no new archive"
    if not reason:
        paths = [
            name
            for name in original
            if (root / name).read_text(encoding="utf-8") != original[name]
        ] + [str(p.relative_to(root)) for p in new]
        result = git(root, "add", "--", *paths)
        if not result.returncode:
            result = git(
                root,
                "commit",
                "--only",
                "-m",
                f"docs: archive AGENTS history ({datetime.fromtimestamp(data['time']):%Y-%m-%d})",
                "--",
                *paths,
            )
        if result.returncode:
            git(root, "reset", "--", *paths)
            reason = "commit failed"
    if reason:
        for name, content in original.items():
            (root / name).write_text(content, encoding="utf-8")
        for p in new:
            p.unlink()
        message = f"문서 이관 실패 {root.name} — {reason}"
    else:
        sha = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
        message = f"문서 이관 {root.name} {before}줄→{after}줄 커밋 {sha}"
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
