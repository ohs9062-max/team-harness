from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

import pytest

from team_harness import resource_slots as slots


def test_atomic_reservations_enforce_limit_across_different_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TH_MAX_PARALLEL", "1")

    def attempt(n: int):
        try:
            return slots.reserve(tmp_path / str(n), "codex")
        except slots.AdmissionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(attempt, range(8)))
    assert sum(key is not None for key in keys) == 1
    slots.release(next(key for key in keys if key is not None))
    assert slots.reserve(tmp_path / "next", "codex")


def test_dead_reservation_does_not_leak_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TH_MAX_PARALLEL", "1")
    directory = tmp_path / "watch"
    directory.mkdir()
    (directory / "admission.json").write_text(
        json.dumps(
            {
                "old": {
                    "pid": os.getpid(),
                    "start": "linux:old-boot:0",
                    "worker": "codex",
                    "task": "old",
                    "bound": False,
                }
            }
        )
    )
    assert slots.reserve(tmp_path / "new", "codex", directory=directory)


def test_same_basename_tasks_each_occupy_a_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TH_MAX_PARALLEL", "2")
    slots.reserve(tmp_path / "a/task", "codex")
    slots.reserve(tmp_path / "b/task", "codex")
    with pytest.raises(slots.AdmissionError):
        slots.reserve(tmp_path / "c/task", "codex")


def test_quota_applies_to_antigravity_alias(tmp_path: Path):
    directory = tmp_path / "watch"
    directory.mkdir()
    (directory / "quota_lock.agy").write_text("quota exhausted")
    with pytest.raises(slots.AdmissionError, match="quota"):
        slots.reserve(tmp_path / "task", "antigravity", directory=directory)


def test_corrupt_ledger_does_not_admit_unbounded_workers(tmp_path: Path):
    directory = tmp_path / "watch"
    directory.mkdir()
    (directory / "admission.json").write_text("invalid json")
    with pytest.raises(ValueError):
        slots.reserve(tmp_path / "task", "codex", directory=directory)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux zombie state")
def test_unbound_zombie_launcher_releases_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import subprocess
    import time

    from team_harness.agents.process_identity import capture_starttime

    monkeypatch.setenv("TH_MAX_PARALLEL", "1")
    launcher = subprocess.Popen(["sleep", "30"])
    try:
        token = capture_starttime(launcher.pid)
        assert token is not None
        directory = tmp_path / "watch"
        directory.mkdir()
        (directory / "admission.json").write_text(
            json.dumps(
                {
                    "old": {
                        "pid": launcher.pid,
                        "start": token,
                        "worker": "codex",
                        "task": "old",
                        "bound": False,
                    }
                }
            )
        )
        launcher.terminate()
        deadline = time.monotonic() + 5
        while (
            Path(f"/proc/{launcher.pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
            != "Z"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # Do not poll/wait before reserve: leave an actual unreaped zombie.
        assert slots.reserve(tmp_path / "new", "codex", directory=directory)
        assert "old" not in json.loads((directory / "admission.json").read_text())
    finally:
        launcher.wait(timeout=5)


def test_preflight_counts_shared_reservations_but_excludes_replaced_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from team_harness.launch_policy import check

    monkeypatch.setenv("TH_MAX_PARALLEL", "1")
    directory = tmp_path / "watch"
    task = tmp_path / "existing"
    task.mkdir()
    (task / "worker.pid").write_text(str(os.getpid()))
    key = slots.reserve(task, "codex", directory=directory)
    slots.bind(key, os.getpid(), directory=directory)
    assert not any(
        v.code == "parallel" for v in check(task, "codex", "", "", directory)
    )
    assert any(
        v.code == "parallel"
        for v in check(tmp_path / "other", "codex", "", "", directory)
    )
    slots.release(key, directory=directory)


def test_preflight_counts_other_pending_launch_for_same_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from team_harness.launch_policy import check

    monkeypatch.setenv("TH_MAX_PARALLEL", "1")
    directory = tmp_path / "watch"
    task = tmp_path / "same"
    slots.reserve(task, "codex", directory=directory)
    assert any(v.code == "parallel" for v in check(task, "codex", "", "", directory))
