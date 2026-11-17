import fcntl
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import patch

import pytest

from team_harness.unread import clean_unread
from team_harness.unread import owner_state


def test_owner_classification(tmp_path: Path) -> None:
    assert owner_state(str(tmp_path / "missing")) == "orphan"
    assert owner_state(str(tmp_path)) == "valid"  # inbox need not exist
    with patch("team_harness.unread.os.stat", side_effect=PermissionError):
        assert owner_state(str(tmp_path)) == "unknown"


def test_cleanup_and_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch = tmp_path / "watch"
    state = watch / "inbox_unread" / "x.tsv"
    state.parent.mkdir(parents=True)
    owner = tmp_path / "owner"
    cursor = owner / ".claude" / ".inbox_cursor.d" / "read"
    cursor.parent.mkdir(parents=True)
    cursor.write_text("10")
    missing = tmp_path / "missing"
    lines = [
        f"1\t10\t0\t{owner}\tread\tclaude\n",
        f"1\t10\t0\t{owner}\tunread\tclaude\n",
        f"1\t10\t0\t{missing}\tx\tclaude\n",
        f"999\t10\t0\t{missing}\tx\tclaude\n",
    ]
    state.write_text("".join(lines))
    monkeypatch.setenv("TH_UNREAD_ORPHAN_DAYS", "1")
    assert clean_unread(watch, dry_run=True, now=86401) == {"read": 1, "orphan": 1}
    assert state.read_text() == "".join(lines)
    assert list(watch.rglob("*")) == [state.parent, state]
    assert clean_unread(watch, now=86401) == {"read": 1, "orphan": 1}
    assert state.read_text() == lines[1] + lines[3]
    assert (
        next((watch / "archive").glob("unread_orphans_*.tsv")).read_text() == lines[2]
    )


def test_concurrent_append_uses_lock(tmp_path: Path) -> None:
    state = tmp_path / "inbox_unread" / "x.tsv"
    state.parent.mkdir()
    state.write_text(f"1\t2\t0\t{tmp_path / 'gone'}\tr\ta\n")
    line = f"1\t2\t0\t{tmp_path}\tr\ta\n"
    with state.with_suffix(".tsv.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        thread = threading.Thread(
            target=clean_unread, args=(tmp_path,), kwargs={"now": 99999999}
        )
        thread.start()
        with state.open("a") as out:
            out.write(line)
        fcntl.flock(lock, fcntl.LOCK_UN)
    thread.join()
    assert state.read_text() == line


def test_status_orphan_and_valid(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    state = watch / "inbox_unread" / "x.tsv"
    state.parent.mkdir(parents=True)
    owner = tmp_path / "test-session"
    owner.mkdir()
    state.write_text(
        f"1\t2\t0\t{tmp_path / 'gone'}\tr\tclaude\n1\t2\t0\t{owner}\ttest-session\tclaude\n"
    )
    script = Path(__file__).resolve().parents[2] / "scripts" / "th-status.sh"
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "TH_WATCH_DIR": str(watch),
            "TH_NO_MAINTENANCE": "1",
            "TH_OBSERVE": "0",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "고아 알림 기록 1건(삭제된 경로)" in result.stdout
    assert f"{owner} 세션 test-ses waiter 없음" in result.stdout
    assert str(tmp_path / "gone") not in result.stdout
