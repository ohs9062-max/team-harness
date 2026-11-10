import json
import os
from pathlib import Path

import pytest

from team_harness import worker_relaunch as wr


def test_missing_launch_identity_never_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(wr.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(wr, "kill_group", lambda *a: pytest.fail("unverified signal"))
    with pytest.raises(ValueError, match="identity unavailable"):
        wr.terminate_worker_group(12345, task=tmp_path)


@pytest.mark.parametrize("saved_pid,token", [(12345, "boot:10"), (98765, "boot:10")])
def test_legacy_identity_is_bound_to_recorded_pid(
    tmp_path: Path, saved_pid: int, token: str
):
    (tmp_path / "worker.pid.start").write_text(
        json.dumps({"pid": saved_pid, "start": token})
    )
    assert wr.persisted_worker_starttime(tmp_path, 12345) == (
        "linux:boot:10" if saved_pid == 12345 else None
    )


def test_identity_mismatch_is_reported_without_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "worker.pid.start").write_text(
        json.dumps({"pid": 12345, "start": "boot:10"})
    )
    monkeypatch.setattr(wr.os, "getpgid", lambda pid: pid)
    calls = []
    monkeypatch.setattr(
        wr, "kill_group", lambda *a: calls.append(a) or "identity-mismatch-skipped"
    )
    with pytest.raises(ValueError, match="identity-mismatch-skipped"):
        wr.terminate_worker_group(12345, task=tmp_path)
    assert calls == [(12345, "linux:boot:10")]


def test_own_coordinator_group_is_never_signalled(tmp_path: Path):
    with pytest.raises(ValueError, match="isolated"):
        wr.terminate_worker_group(os.getpid(), task=tmp_path)
