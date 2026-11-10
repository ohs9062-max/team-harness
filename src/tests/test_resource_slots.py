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
