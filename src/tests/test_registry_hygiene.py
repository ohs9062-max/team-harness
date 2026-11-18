from datetime import datetime
from datetime import timedelta
import os
from pathlib import Path

from team_harness.registry_hygiene import clean


def test_registry_cleanup_keeps_unresolved_and_archives_old(tmp_path: Path):
    watch = tmp_path / "watch"
    watch.mkdir()
    old = tmp_path / "old"
    pending = tmp_path / "pending"
    failed = tmp_path / "failed"
    live = tmp_path / "live"
    for task in (old, pending, failed, live):
        task.mkdir()
        (task / "DONE").touch()
    for task in (old, failed, live):
        (task / "VERIFIED").touch()
    (failed / "FAILED").touch()
    (live / "job.pid").write_text(str(os.getpid()))
    then = (datetime.now() - timedelta(days=40)).timestamp()
    for marker in (old / "DONE", old / "VERIFIED"):
        os.utime(marker, (then, then))
    missing = tmp_path / "missing"
    (watch / "tasks.txt").write_text(
        "\n".join(map(str, (old, pending, failed, live, missing))) + "\n"
    )
    (watch / "quota_lock.expired").write_text("capacity expires=1\n")
    (watch / "quota_lock.unknown").write_text("quota exhausted\n")
    result = clean(watch, datetime.now().astimezone())
    assert (watch / "tasks.txt").read_text().splitlines() == list(
        map(str, (pending, failed, live))
    )
    assert result["missing"] == [str(missing)]
    assert result["archived"] == [str(old)]
    assert list((watch / "archive").glob("tasks_done_*.tsv"))[
        0
    ].read_text().splitlines() == [str(old)]
    assert result["quota"] == ["quota_lock.expired"]
    assert result["unknown_quota"] == ["quota_lock.unknown"]
    assert (watch / "quota_lock.unknown").exists()
