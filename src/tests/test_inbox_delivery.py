"""Regression coverage for inbox owner routing and unread delivery alerts."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _run(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def _init_repo(path: Path) -> None:
    path.mkdir()
    _run("git", "init", str(path))


def test_resolve_launch_owner_uses_task_repo_and_reports_pwd_mismatch(
    tmp_path: Path,
) -> None:
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    _init_repo(repo_a)
    _init_repo(repo_b)
    task = repo_b / "outputs" / "task"
    task.mkdir(parents=True)

    result = _run(
        "bash",
        "-c",
        'source "$1"; owner=$(resolve_launch_owner "$2" "$3" "$4"); '
        'warn_launch_owner_mismatch "$owner" "$4"; printf "%s\\n" "$owner"',
        "bash",
        str(SCRIPTS / "th-watch-lib.sh"),
        str(task),
        str(repo_a / "outside"),
        str(repo_a),
    )

    assert result.stdout.strip() == str(repo_b.resolve())
    assert f"owner={repo_b.resolve()}" in result.stderr
    assert str(repo_a.resolve()) in result.stderr


def test_multiple_waiters_keep_independent_registrations(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    _init_repo(owner)
    env = {**os.environ, "TH_POLL_SEC": "1"}
    first = subprocess.Popen(
        [str(SCRIPTS / "th-inbox-wait.sh"), "30"],
        cwd=owner,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.2)
        second = subprocess.Popen(
            [str(SCRIPTS / "th-inbox-wait.sh"), "30"],
            cwd=owner,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            time.sleep(0.2)
            registry = owner / ".claude" / ".inbox_wait.d"
            assert (registry / str(first.pid)).is_file()
            assert (registry / str(second.pid)).is_file()

            first.terminate()
            first.wait(timeout=3)
            assert not (registry / str(first.pid)).exists()
            assert (registry / str(second.pid)).is_file()
        finally:
            second.terminate()
            second.wait(timeout=3)
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=3)


def test_watchd_realerts_only_when_cursor_has_not_reached_recorded_size(
    tmp_path: Path,
) -> None:
    owner = tmp_path / "owner"
    inbox_dir = owner / ".claude"
    inbox_dir.mkdir(parents=True)
    watch_dir = tmp_path / "watch"
    state_dir = watch_dir / "inbox_unread"
    state_dir.mkdir(parents=True)
    old = int(time.time()) - 2
    state = state_dir / "owner.tsv"
    state.write_text(f"{old}\t4\t0\t{owner}\n", encoding="utf-8")
    (inbox_dir / ".inbox_cursor").write_text("3\n", encoding="utf-8")
    env = {
        **os.environ,
        "TH_WATCH_DIR": str(watch_dir),
        "TH_ONCE": "1",
        "TH_INBOX_UNREAD_SEC": "1",
        "TH_JEV_WATCH": "off",
    }

    _run("bash", str(SCRIPTS / "th-watchd.sh"), env=env)
    assert "[inbox 미수신]" in (watch_dir / "events.log").read_text(encoding="utf-8")
    assert "\t1\t" in state.read_text(encoding="utf-8")

    (inbox_dir / ".inbox_cursor").write_text("4\n", encoding="utf-8")
    state.write_text(f"{old}\t4\t0\t{owner}\n", encoding="utf-8")
    (watch_dir / "events.log").unlink()
    _run("bash", str(SCRIPTS / "th-watchd.sh"), env=env)
    assert not (watch_dir / "events.log").exists()
