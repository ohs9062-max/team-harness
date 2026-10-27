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


def _waiter_env(key: str = "") -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_")}
    env.update({"ORCA_PANE_KEY": key, "CLAUDECODE": "1", "TH_POLL_SEC": "1"})
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("TH_SESSION_KEY", None)
    return env


def _append_routed(owner: Path, route: str, message: str) -> None:
    import fcntl

    root = owner / ".claude"
    with (root / ".inbox_cursor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        inbox = root / "inbox.md"
        offset = inbox.stat().st_size
        with (root / "inbox.routes").open("a") as routes:
            routes.write(f"{offset}\t{route}\n")
        with inbox.open("a") as output:
            output.write(message + "\n")


def test_sessions_route_and_broadcast(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    _init_repo(owner)
    processes: list[subprocess.Popen[str]] = []

    def start(key: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [str(SCRIPTS / "th-inbox-wait.sh"), "15"],
            cwd=owner,
            env=_waiter_env(key),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(proc)
        return proc

    try:
        a, b = start("session-a"), start("session-b")
        time.sleep(0.5)
        _append_routed(owner, "session-a", "only-a")
        out, _ = a.communicate(timeout=5)
        assert "only-a" in out
        time.sleep(1.2)
        assert b.poll() is None
        a2 = start("session-a")
        time.sleep(0.3)
        _append_routed(owner, "*", "broadcast")
        out_a, _ = a2.communicate(timeout=5)
        out_b, _ = b.communicate(timeout=5)
        assert "broadcast" in out_a and "broadcast" in out_b
        assert "only-a" not in out_b
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)


def test_codex_waiter_rejected_without_cursor_change(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    _init_repo(owner)
    root = owner / ".claude"
    root.mkdir()
    cursor = root / ".inbox_cursor"
    cursor.write_text("8\n")
    env = _waiter_env()
    env.pop("CLAUDECODE")
    env["CODEX_TEST"] = "1"
    result = subprocess.run(
        [str(SCRIPTS / "th-inbox-wait.sh"), "0"],
        cwd=owner,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert cursor.read_text() == "8\n"
    assert not (root / ".inbox_wait.d").exists()


def test_missing_session_waiter_alert_is_immediate(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    (owner / ".claude").mkdir(parents=True)
    watch = tmp_path / "watch"
    state = watch / "inbox_unread" / "owner.tsv"
    state.parent.mkdir(parents=True)
    state.write_text(f"{int(time.time())}\t4\t0\t{owner}\tabcdefghijk\n")
    env = {
        **os.environ,
        "TH_WATCH_DIR": str(watch),
        "TH_ONCE": "1",
        "TH_JEV_WATCH": "off",
    }
    _run("bash", str(SCRIPTS / "th-watchd.sh"), env=env)
    assert "[inbox 미수신]" in (watch / "events.log").read_text()
    result = _run("bash", str(SCRIPTS / "th-status.sh"), env=env)
    assert f"{owner} 세션 abcdefgh waiter 없음" in result.stdout


def test_legacy_cursor_and_peek_ack(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    _init_repo(owner)
    env = _waiter_env()
    _run("bash", str(SCRIPTS / "th-inbox-wait.sh"), "0", cwd=owner, env=env)
    root = owner / ".claude"
    assert (root / ".inbox_cursor").read_text().strip() == "0"
    _append_routed(owner, "*", "legacy message")
    for _ in range(2):
        peek = _run("bash", str(SCRIPTS / "th-inbox-peek.sh"), cwd=owner, env=env)
        assert "legacy message" in peek.stdout
        assert (root / ".inbox_cursor").read_text().strip() == "0"
    peek = _run("bash", str(SCRIPTS / "th-inbox-peek.sh"), "--ack", cwd=owner, env=env)
    assert "legacy message" in peek.stdout
    assert (
        int((root / ".inbox_cursor").read_text()) == (root / "inbox.md").stat().st_size
    )
    _append_routed(owner, "session-a", "private event")
    _append_routed(owner, "*", "public event")
    waiter = _run("bash", str(SCRIPTS / "th-inbox-wait.sh"), "0", cwd=owner, env=env)
    assert "private event" not in waiter.stdout
    assert "public event" in waiter.stdout
