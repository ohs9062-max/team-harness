from datetime import datetime
import os
from pathlib import Path

from pytest import MonkeyPatch

from team_harness.quota_lock import expiry_for_message


def test_time_only_reset_crosses_midnight() -> None:
    now = datetime(2026, 11, 25, 23, 50)
    assert datetime.fromtimestamp(
        expiry_for_message("try again at 12:10 AM", now)
    ) == datetime(2026, 11, 26, 0, 15)


def test_existing_lock_and_logging(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from team_harness import quota_lock

    lock = tmp_path / "quota_lock.codex"
    lock.write_text("2026-11-25 17:21 | task | try again at 6:57 PM")
    for now, expected in [
        (datetime(2026, 11, 25, 18), True),
        (datetime(2026, 11, 26, 8, 40), False),
    ]:
        monkeypatch.setattr(quota_lock.time, "time", lambda now=now: now.timestamp())
        assert quota_lock.active(lock) is expected
    assert not quota_lock.active(lock)
    assert lock.exists()
    assert (tmp_path / "events.log").read_text().count("[quota expired]") == 1


def test_legacy_retry_time_prevents_early_unlock(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from team_harness import quota_lock

    lock = tmp_path / "quota_lock.codex"
    lock.write_text("2026-11-25 17:21 | task | try again at 8:00 AM")
    for now, expected in [
        (datetime(2026, 11, 26, 6), True),
        (datetime(2026, 11, 26, 8, 6), False),
    ]:
        monkeypatch.setattr(quota_lock.time, "time", lambda now=now: now.timestamp())
        assert quota_lock.active(lock) is expected


def test_legacy_expiry_from_timestamp_and_mtime(tmp_path: Path) -> None:
    from team_harness.quota_lock import lock_expiry

    lock = tmp_path / "quota_lock.codex"
    cases = [
        ("2026-11-25 23:50 | try again at 12:30 AM", datetime(2026, 11, 26, 0, 35)),
        ("2026-11-25 17:21 | quota", datetime(2026, 11, 26, 5, 21)),
        ("2026-11-25 17:21 | at capacity", datetime(2026, 11, 25, 17, 36)),
    ]
    for content, expected in cases:
        lock.write_text(content)
        assert lock_expiry(lock, content) == expected.timestamp()
    content = "try again at 8:00 AM"
    lock.write_text(content)
    stamp = datetime(2026, 11, 25, 17, 21).timestamp()
    os.utime(lock, (stamp, stamp))
    assert lock_expiry(lock, content) == datetime(2026, 11, 26, 8, 5).timestamp()
    assert lock_expiry(lock, content + " expires=123") == 123


def test_message_expiries() -> None:
    now = datetime(2026, 11, 25, 23, 50)
    assert datetime.fromtimestamp(
        expiry_for_message("try again at 12:30 AM", now)
    ) == datetime(2026, 11, 26, 0, 35)
    assert expiry_for_message("at capacity", now) == int(now.timestamp()) + 900
    assert expiry_for_message("quota", now) == int(now.timestamp()) + 43200


def test_explicit_expiry_and_mtime(tmp_path: Path) -> None:
    from team_harness.quota_lock import lock_expiry

    lock = tmp_path / "quota_lock.codex"
    lock.write_text("expires=123")
    assert lock_expiry(lock, lock.read_text()) == 123
    lock.write_text("no timestamp")
    assert lock_expiry(lock, lock.read_text()) == lock.stat().st_mtime + 43200


def test_invalid_retry_time_uses_twelve_hours(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from team_harness import quota_lock

    lock = tmp_path / "quota_lock.codex"
    lock.write_text("2026-11-25 17:21 | quota exceeded try again at 13:00 PM")
    monkeypatch.setattr(
        quota_lock.time, "time", lambda: datetime(2026, 11, 26, 5).timestamp()
    )
    assert quota_lock.active(lock)
    monkeypatch.setattr(
        quota_lock.time, "time", lambda: datetime(2026, 11, 26, 6).timestamp()
    )
    assert not quota_lock.active(lock)


def test_evaluation_exception_blocks(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from team_harness import quota_lock

    lock = tmp_path / "quota_lock.codex"
    lock.write_text("quota")

    def fail(*args: object) -> float:
        raise ValueError("evaluation failed")

    monkeypatch.setattr(quota_lock, "lock_expiry", fail)
    assert quota_lock.active(lock)
    assert not quota_lock.active(tmp_path / "missing")
