"""The Claude adapter must never repeat a command after the runner starts it."""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts/hooks/th-exec-pipeline.py"


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("claude_execution_adapter", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("TH_NO_DAEMON", "1")
    return module


def test_watch_is_file_runs_original(
    adapter: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "watch").touch()
    assert adapter.execute(tmp_path, "printf 'train original ran\\n'") == 0
    out, err = capfd.readouterr()
    assert out == "train original ran\n"
    assert len(err.splitlines()) == 1


def test_missing_runner_runs_once(
    adapter: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_root = tmp_path / "fake"
    monkeypatch.setattr(adapter, "__file__", str(fake_root / "scripts/hooks/hook.py"))
    counter = tmp_path / "counter"
    assert adapter.execute(tmp_path, f"echo x >> {counter}; exit 7") == 7
    assert counter.read_text().splitlines() == ["x"]


def test_failure_is_not_repeated(adapter: ModuleType, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    assert adapter.execute(tmp_path, f"echo x >> {counter}; exit 7") == 7
    assert counter.read_text().splitlines() == ["x"]
    assert (tmp_path / "FAILED").read_text() == "command rc=7\n"


def test_normal_path(
    adapter: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert (
        adapter.execute(
            tmp_path,
            "test -z \"${TH_JOB_START_NOTIFY_FD:-}\" && printf 'normal output\\n'",
        )
        == 0
    )
    assert capfd.readouterr().out == "normal output\n"
    assert (tmp_path / "exit_code").read_text() == "0\n"
    assert (tmp_path / "DONE").exists()
    assert (tmp_path / "logs/job.log").read_text() == "normal output\n"
    assert str(tmp_path) in (tmp_path / "watch/tasks.txt").read_text()


def test_post_record_failure_preserves_rc(
    adapter: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.open
    status_opens = 0

    def fail_output(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal status_opens
        if path.name == "STATUS":
            status_opens += 1
            if status_opens == 2:
                raise OSError("status unavailable")
        if path.name in {"exit_code", "DONE", "FAILED"}:
            raise OSError("record unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_output)
    assert adapter.execute(tmp_path, "exit 7") == 7


def test_runner_preparation_failure_runs_once(
    adapter: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "logs").touch()
    counter = tmp_path / "counter"
    assert adapter.execute(tmp_path, f"echo x >> {counter}; exit 7") == 7
    assert counter.read_text().splitlines() == ["x"]


def test_unknown_runner_does_not_retry(
    adapter: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_root = tmp_path / "fake"
    runner = fake_root / "scripts/th-job-start.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/bash\nexit 9\n")
    runner.chmod(0o755)
    monkeypatch.setattr(adapter, "__file__", str(fake_root / "scripts/hooks/hook.py"))
    counter = tmp_path / "counter"
    assert adapter.execute(tmp_path, f"echo x >> {counter}") == 9
    assert not counter.exists()
