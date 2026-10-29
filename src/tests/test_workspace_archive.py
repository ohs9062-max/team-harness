from datetime import datetime
from datetime import timedelta
import os
from pathlib import Path
import subprocess

import pytest

from team_harness.workspace_archive import archive_daily
from team_harness.workspace_archive import archive_workspace

NOW = datetime(2026, 10, 29, 12, 0).astimezone()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    owner = tmp_path / "owner"
    (owner / ".claude").mkdir(parents=True)
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    return owner, watch


def inbox(owner: Path, cursor: int | None = None) -> bytes:
    root = owner / ".claude"
    data = b"2026-10-20 10:00 | old\n2026-10-28 10:00 | new\n"
    (root / "inbox.md").write_bytes(data)
    (root / ".inbox_cursor").write_text(str(len(data) if cursor is None else cursor))
    return data


def task(env: tuple[Path, Path], *, register: bool = True) -> Path:
    owner, watch = env
    path = owner / "_outputs" / "job"
    path.mkdir(parents=True)
    for name, value in [(".th_owner", str(owner)), ("DONE", ""), ("VERIFIED", "")]:
        (path / name).write_text(value)
    if register:
        (watch / "tasks.txt").write_text(str(path) + "\n")
    age(path)
    return path


def age(path: Path) -> None:
    ts = (NOW - timedelta(days=8)).timestamp()
    for p in [*path.rglob("*"), path]:
        os.utime(p, (ts, ts))


def test_inbox_three_days(env: tuple[Path, Path]) -> None:
    owner, _ = env
    inbox(owner)
    archive_workspace(owner, NOW)
    assert (owner / ".claude/inbox.md").read_bytes().startswith(b"2026-10-28")
    assert (
        (owner / ".claude/archive/inbox_202610.md")
        .read_bytes()
        .startswith(b"2026-10-20")
    )


def test_inbox_unread_preserved(env: tuple[Path, Path]) -> None:
    owner, _ = env
    data = inbox(owner, 0)
    archive_workspace(owner, NOW)
    assert (owner / ".claude/inbox.md").read_bytes() == data


def test_inbox_cursors_and_routes(env: tuple[Path, Path]) -> None:
    owner, _ = env
    data = inbox(owner)
    root = owner / ".claude"
    first = data.index(b"\n") + 1
    (root / ".inbox_cursor.d").mkdir()
    (root / ".inbox_cursor.d/session").write_text(str(first))
    (root / "inbox.routes").write_text(f"0\ta\n{first}\tb\n")
    archive_workspace(owner, NOW)
    assert (root / ".inbox_cursor").read_text().strip() == str(len(data) - first)
    assert (root / ".inbox_cursor.d/session").read_text().strip() == "0"
    assert (root / "inbox.routes").read_text() == "0\tb\n"
    assert (root / "archive/inbox_routes_202610.txt").read_text() == "0\ta\n"


def test_task_old_registered(env: tuple[Path, Path]) -> None:
    path = task(env)
    archive_workspace(env[0], NOW)
    dest = path.parent / "_archive/202610/job"
    assert dest.is_dir() and env[1].joinpath("tasks.txt").read_text().strip() == str(
        dest
    )


def test_retired_task_is_archived_and_registry_updated(env: tuple[Path, Path]) -> None:
    path = task(env)
    watch = env[1]
    (watch / "tasks.txt").rename(watch / "tasks_done.txt")
    archive_daily(NOW)
    dest = path.parent / "_archive/202610/job"
    assert dest.is_dir()
    assert (watch / "tasks_done.txt").read_text().strip() == str(dest)


def test_archive_updates_both_registries_after_reregistration(
    env: tuple[Path, Path],
) -> None:
    path = task(env)
    watch = env[1]
    (watch / "tasks_done.txt").write_text(f"{path}\n{path}\n")
    archive_workspace(env[0], NOW)
    dest = str(path.parent / "_archive/202610/job")
    assert (watch / "tasks.txt").read_text().splitlines() == [dest]
    assert (watch / "tasks_done.txt").read_text().splitlines() == [dest, dest]


def test_task_recent(env: tuple[Path, Path]) -> None:
    path = task(env)
    os.utime(path / "DONE", (NOW.timestamp(), NOW.timestamp()))
    archive_workspace(env[0], NOW)
    assert path.exists()


def test_task_requires_verified(env: tuple[Path, Path]) -> None:
    path = task(env)
    (path / "VERIFIED").unlink()
    age(path)
    archive_workspace(env[0], NOW)
    assert path.exists()


def test_task_failed_excluded(env: tuple[Path, Path]) -> None:
    path = task(env)
    (path / "FAILED").touch()
    age(path)
    archive_workspace(env[0], NOW)
    assert path.exists()


def test_task_alive_excluded(env: tuple[Path, Path]) -> None:
    path = task(env)
    (path / "worker.pid").write_text(str(os.getpid()))
    age(path)
    archive_workspace(env[0], NOW)
    assert path.exists()


def test_task_dirty_worktree_excluded(env: tuple[Path, Path], tmp_path: Path) -> None:
    path = task(env)
    wt = tmp_path / "repo"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / "dirty").touch()
    (path / ".th_worktree").write_text(str(wt))
    age(path)
    archive_workspace(env[0], NOW)
    assert path.exists()


def test_task_unregistered_excluded(env: tuple[Path, Path]) -> None:
    path = task(env, register=False)
    archive_workspace(env[0], NOW)
    assert path.exists()


def test_journal_age(env: tuple[Path, Path]) -> None:
    owner, _ = env
    old = owner / ".claude/work_log_20261001.md"
    old.touch()
    ts = (NOW - timedelta(days=8)).timestamp()
    os.utime(old, (ts, ts))
    new = owner / ".claude/workers_archive.md"
    new.touch()
    os.utime(new, (NOW.timestamp(), NOW.timestamp()))
    archive_workspace(owner, NOW)
    assert not old.exists() and (owner / ".claude/archive" / old.name).exists()
    assert new.exists()


def test_events_size(env: tuple[Path, Path]) -> None:
    owner, watch = env
    (watch / "events.log").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    archive_workspace(owner, NOW)
    assert (watch / "events_202610.log").exists()


def test_dry_run(env: tuple[Path, Path]) -> None:
    path = task(env)
    moves = archive_workspace(env[0], NOW, dry_run=True)
    assert moves and path.exists() and not moves[0].destination.exists()


def test_daily_once(env: tuple[Path, Path]) -> None:
    owner, watch = env
    task(env)
    assert archive_daily(NOW, owner)
    assert (watch / "archive.last").exists()
    (watch / "events.log").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    assert not archive_daily(NOW + timedelta(hours=23), owner)
    assert archive_daily(NOW + timedelta(days=1), owner)
