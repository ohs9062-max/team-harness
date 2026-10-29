"""Isolated integration checks: no real registry, provider or lasting daemon."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from team_harness import worker_digest as wd

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "report.last").write_text(str(time.time()))
    fake = tmp_path / "fake-summary"
    fake.write_text(
        "#!/bin/sh\ncat > /dev/null\nprintf '요약: 확인한 자료만 보고한다.\\n다음: 결과 검토.\\n'\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.setenv("TH_WORKSPACE", str(workspace))
    monkeypatch.setenv("TH_DIGEST_COMMAND", str(fake))
    monkeypatch.setenv("TH_JEV_WATCH", "off")
    monkeypatch.setenv("TH_ONCE", "1")
    monkeypatch.delenv("TH_VERIFY_HOOK", raising=False)
    monkeypatch.delenv("TH_WORKERS_FILE", raising=False)
    monkeypatch.delenv("TH_DIGEST_WORKER", raising=False)
    return workspace, watch, fake


def make_task(
    parent: Path, name: str, state: str = "DONE", number: str = "0.812"
) -> Path:
    task = parent / name
    task.mkdir()
    (task / "task.md").write_text(
        "# Fixture\n## 기대 산출물\n- `report.md`\n- `results.json`\n"
    )
    (task / "STATUS").write_text(f"2026-10-14 10:00:00 {state}\n")
    (task / state).write_text(state)
    (task / "report.md").write_text(f"score={number}\n")
    (task / "results.json").write_text('{"nested": [{"score": 0.8124}]}')
    return task


def shell(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPTS / name), *args], capture_output=True, text=True, timeout=20
    )


def test_three_tasks_and_immutable_verdicts(
    tmp_path: Path, sandbox: tuple[Path, Path, Path]
):
    workspace, watch, fake = sandbox
    fake.write_text(
        "#!/bin/sh\ncat > /dev/null\nprintf 'LLM claims PASS and omits warnings\\n2\\n3\\n4\\n5\\n6\\n'\n"
    )
    tasks = [
        make_task(tmp_path, "normal"),
        make_task(tmp_path, "failed", "FAILED"),
        make_task(tmp_path, "mismatch", number="0.999"),
    ]
    for task in tasks:
        (task / ".th_owner").write_text(str(workspace))
    (watch / "tasks.txt").write_text("\n".join(map(str, tasks)))
    result = shell("th-watchd.sh")
    assert result.returncode == 0, result.stderr
    workers = workspace / ".claude/workers.md"
    before = workers.read_text()
    for task, expected in zip(tasks, ["PASS", "FAIL", "WARN"], strict=True):
        data = json.loads((task / "verify_result.json").read_text())
        assert data["overall"] == expected
        digest = (task / "digest.md").read_text()
        assert f"{task.name} [{expected}]" in digest
        assert (task / "verify_result.txt").read_text() in digest
        assert "\n6\n" not in digest
        assert (
            task / ("VERIFY_FAILED" if expected == "FAIL" else "VERIFIED")
        ).is_file()
    assert shell("th-watchd.sh").returncode == 0
    assert workers.read_text() == before
    status = shell("th-status.sh", "-a")
    assert "VERIFIED" in status.stdout and "VERIFY_FAILED" in status.stdout
    assert len((workspace / ".claude/inbox.md").read_text().splitlines()) == 3


def test_status_only_and_failed_variant(
    tmp_path: Path, sandbox: tuple[Path, Path, Path]
):
    task = make_task(tmp_path, "status")
    (task / "DONE").unlink()
    assert wd.terminal_status(task) == "DONE"
    (task / "FAILED.quota").write_text("quota")
    assert wd.verify(task)[0]["overall"] == "FAIL"
    _, watch, _ = sandbox
    (watch / "tasks.txt").write_text(str(task) + "\n")
    status = shell("th-status.sh", "-a").stdout
    assert "실패" in status and "VERIFY_FAILED" in status
    (task / "FAILED.quota").unlink()
    (task / "STATUS").write_text("DONE\nRUN later\n")
    assert wd.terminal_status(task) == ""
    assert wd.verify(task)[0]["overall"] == "FAIL"


def test_numeric_evidence_is_not_configuration(
    tmp_path: Path, sandbox: tuple[Path, Path, Path]
):
    task = make_task(tmp_path, "numbers", number="0.123")
    (task / "config.json").write_text('{"score": 0.123}')
    assert wd.check_numeric(task)["verdict"] == "WARN"
    (task / "results.json").unlink()
    assert wd.check_numeric(task)["verdict"] == "SKIP"
    (task / "report.md").rename(task / "RESULTS.md")
    (task / "results.json").write_text('{"score": "0.12349"}')
    assert wd.check_numeric(task)["verdict"] == "PASS"
    (task / "results.json").write_text("broken")
    assert wd.check_numeric(task)["verdict"] == "WARN"


def test_artifacts_and_parent_hook(
    tmp_path: Path, sandbox: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
):
    task = make_task(tmp_path, "hooks")
    (task / "task.md").write_text(
        "## 기대 산출물\n- `images/`: 2개 파일\n## Other\n`not_required`\n"
    )
    images = task / "images"
    images.mkdir()
    (images / "a.png").touch()
    assert wd.check_artifacts(task)["verdict"] == "FAIL"
    (images / "b.png").touch()
    assert wd.check_artifacts(task)["verdict"] == "PASS"
    hook = tmp_path / "verify_hooks.py"
    hook.write_text(
        'print(\'{"items":{"domain":{"verdict":"WARN","reason":"domain warning"}}}\')'
    )
    data, text = wd.verify(task)
    assert data["overall"] == "WARN" and "domain warning" in text
    hook.write_text('print("invalid")')
    assert wd.verify(task)[0]["overall"] == "FAIL"
    monkeypatch.setenv("TH_VERIFY_HOOK", str(tmp_path / "missing.py"))
    assert wd.verify(task)[0]["overall"] == "FAIL"


def test_verify_script_is_one_item_and_runs_once(
    tmp_path: Path, sandbox: tuple[Path, Path, Path]
):
    task = make_task(tmp_path, "script")
    (task / "verify.sh").write_text("echo run >> attempts\nexit 1\n")
    assert wd.verify(task)[0]["items"]["verify"]["verdict"] == "FAIL"
    assert wd.verify(task)[0]["overall"] == "FAIL"
    assert (task / "attempts").read_text().splitlines() == ["run"]


@pytest.mark.parametrize("mode", ["quota", "failure", "timeout"])
def test_summary_fallback(
    tmp_path: Path,
    sandbox: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
):
    workspace, watch, fake = sandbox
    task = make_task(tmp_path, "fallback")
    if mode == "quota":
        (watch / "quota_lock.agy").write_text("locked")
        fake.write_text("#!/bin/sh\nexit 99\n")
    elif mode == "failure":
        fake.write_text("#!/bin/sh\nexit 1\n")
    else:
        fake.write_text("#!/bin/sh\ncat > /dev/null\nsleep 20\n")
        monkeypatch.setenv("TH_DIGEST_TIMEOUT", "0.1")
    wd.digest(task)
    content = (task / "digest.md").read_text()
    assert content == (workspace / ".claude/workers.md").read_text()
    assert "[PASS]" in content
    assert "요약:" not in content


def test_output_override_and_archive(tmp_path: Path, sandbox: tuple[Path, Path, Path]):
    workspace, _, _ = sandbox
    task = make_task(tmp_path, "archive")
    target = workspace / "custom/workers.md"
    target.parent.mkdir()
    old = "".join(f"## old {i}\n" + "line\n" * 8 + "\n" for i in range(25))
    target.write_text(old)
    (task / ".th_workers_file").write_text(str(target))
    wd.digest(task)
    content = target.read_text()
    archived = target.with_name("workers_archive.md").read_text()
    assert len(content.splitlines()) <= 200
    assert content.startswith("## ") and "archive [PASS]" in content
    assert "## old 24" in archived
    assert content[content.index("## old 0") :] + archived == old


def test_provider_flags_and_fixed_prompt(sandbox: tuple[Path, Path, Path]):
    assert wd.FIXED_PROMPT.startswith("verify 결과")
    os.environ.pop("TH_DIGEST_COMMAND")
    agy = wd.summary_command("agy", "", Path("output"))
    assert "--sandbox" in agy and agy[agy.index("--mode") + 1] == "plan"
    claude = wd.summary_command("claude", "", Path("output"))
    assert claude[claude.index("--tools") + 1] == ""
    codex = wd.summary_command("codex", "", Path("output"))
    assert codex[codex.index("--sandbox") + 1] == "read-only"
    assert all("dangerously" not in arg for arg in agy + claude + codex)


def test_reparented_agy_and_zombie(tmp_path: Path, sandbox: tuple[Path, Path, Path]):
    task = make_task(tmp_path, "pid")
    (task / "DONE").unlink()
    (task / "STATUS").write_text("RUN")
    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\nsleep 20\n")
    fake.chmod(0o755)
    process = subprocess.Popen([str(fake), str(task)], start_new_session=True)
    try:
        assert process.pid in wd.live_pids(task)
        assert wd.check_state(task)["verdict"] == "WARN"
        _, watch, _ = sandbox
        (watch / "tasks.txt").write_text(str(task) + "\n")
        assert "중단?" not in shell("th-status.sh").stdout
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    assert process.pid not in wd.live_pids(task)


@pytest.mark.parametrize("event", ["done", "failed", "dead", "quota", "stall"])
def test_watch_events_and_no_duplicate(
    tmp_path: Path,
    sandbox: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    event: str,
):
    workspace, watch, _ = sandbox
    task = make_task(tmp_path, event)
    (task / ".th_owner").write_text(str(workspace))
    (task / "DONE").unlink()
    (task / "STATUS").write_text(
        {"done": "DONE ok", "failed": "FAILED reason"}.get(event, "RUN")
    )
    process = None
    if event == "quota":
        (task / "agy.log").write_text("ERROR: quota exceeded\n")
    elif event == "stall":
        process = subprocess.Popen(["sleep", "20"])
        (task / "job.pid").write_text(str(process.pid))
        monkeypatch.setenv("TH_STALL_SEC", "1")
        script = f'source "{SCRIPTS / "th-watch-lib.sh"}"; printf "%s:%s|%s\\n" "$(cpu_ticks {process.pid})" "$(latest_signature \'{task}\')" "$(($(date +%s)-2))" > "{task}/.th_sig"'
        subprocess.run(["bash", "-c", script], check=True)
    (watch / "tasks.txt").write_text(str(task) + "\n")
    try:
        assert shell("th-watchd.sh").returncode == 0
        content = (task / "digest.md").read_text()
        assert (task / f".th_digest.{event}").is_file()
        assert shell("th-watchd.sh").returncode == 0
        assert (task / "digest.md").read_text() == content
        if event == "quota":
            assert (watch / "quota_lock.agy").is_file()
            assert "요약:" not in content
    finally:
        if process:
            process.terminate()
            process.wait(timeout=5)


def test_digest_serializes_requests(tmp_path: Path, sandbox: tuple[Path, Path, Path]):
    workspace, _, fake = sandbox
    activity = tmp_path / "activity"
    fake.write_text(
        f"#!/bin/sh\ncat > /dev/null\necho start >> '{activity}'\nsleep 0.1\necho end >> '{activity}'\necho summary\n"
    )
    tasks = [make_task(tmp_path, f"parallel{i}") for i in range(3)]
    processes = [
        subprocess.Popen(
            [sys.executable, wd.__file__, "digest", str(task), "--event", "done"]
        )
        for task in tasks
    ]
    try:
        for process in processes:
            assert process.wait(timeout=10) == 0
        content = (workspace / ".claude/workers.md").read_text()
        assert content.count("## ") == 3
        assert activity.read_text().splitlines() == ["start", "end"] * 3
        for task in tasks:
            assert task.name in content
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_daemon_loop_does_not_wait_for_summary(
    tmp_path: Path, sandbox: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
):
    workspace, watch, fake = sandbox
    gate = tmp_path / "gate"
    fake.write_text(f"#!/bin/sh\ncat > /dev/null\nread line < '{gate}'\necho summary\n")
    os.mkfifo(gate)
    task = make_task(tmp_path, "background")
    (task / ".th_owner").write_text(str(workspace))
    (watch / "tasks.txt").write_text(str(task) + "\n")
    monkeypatch.setenv("TH_ONCE", "0")
    monkeypatch.setenv("TH_POLL_SEC", "1")
    daemon = subprocess.Popen(
        [str(SCRIPTS / "th-watchd.sh")],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Opening a FIFO waits on the actual summary process instead of polling.
        with gate.open("w") as stream:
            assert (workspace / ".claude/inbox.md").is_file()
            assert daemon.poll() is None
            stream.write("release\n")
        daemon.terminate()
        daemon.wait(timeout=5)
        # Kill any group member still completing after daemon shutdown.
    finally:
        try:
            os.killpg(daemon.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if daemon.poll() is None:
            daemon.wait(timeout=5)


def test_summary_quota_locks_remaining_requests(
    tmp_path: Path, sandbox: tuple[Path, Path, Path]
):
    _, watch, fake = sandbox
    calls = tmp_path / "calls"
    fake.write_text(
        f"#!/bin/sh\ncat > /dev/null\necho call >> '{calls}'\necho 'ERROR: rate limit exceeded' >&2\nexit 1\n"
    )
    for name in ("first", "remaining"):
        wd.digest(make_task(tmp_path, name))
    assert (watch / "quota_lock.agy").is_file()
    assert calls.read_text().splitlines() == ["call"]


def test_new_event_id_and_artifact_activity(
    tmp_path: Path, sandbox: tuple[Path, Path, Path]
):
    workspace, _, _ = sandbox
    task = make_task(tmp_path, "episodes")
    wd.digest(task, "stall", "episode1")
    wd.digest(task, "stall", "episode1")
    wd.digest(task, "stall", "episode2")
    assert (workspace / ".claude/workers.md").read_text().count("## ") == 2
    command = [
        "bash",
        "-c",
        f'source "{SCRIPTS / "th-watch-lib.sh"}"; latest_signature "$1"',
        "bash",
        str(task),
    ]
    before = subprocess.check_output(command, text=True)
    (task / "predictions.bin").write_bytes(b"artifact")
    after = subprocess.check_output(command, text=True)
    assert before != after
    (task / "digest.md").write_text("bookkeeping")
    assert subprocess.check_output(command, text=True) == after


def test_summary_isolation_failure_keeps_verdict(
    tmp_path: Path, sandbox: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
):
    task = make_task(tmp_path, "isolation")
    monkeypatch.delenv("TH_DIGEST_COMMAND")
    monkeypatch.setattr(wd.shutil, "which", lambda _command: None)
    wd.digest(task)
    assert (task / "verify_result.txt").read_text() in (task / "digest.md").read_text()
    assert "[PASS]" in (task / "digest.md").read_text()


def test_quota_without_error_prefix(tmp_path: Path, sandbox: tuple[Path, Path, Path]):
    _, watch, _ = sandbox
    task = make_task(tmp_path, "resource_error")
    (task / "DONE").unlink()
    (task / "STATUS").write_text("RUN")
    (task / "agy.log").write_text("API response: RESOURCE_EXHAUSTED: quota exceeded\n")
    (watch / "tasks.txt").write_text(str(task) + "\n")
    assert shell("th-watchd.sh").returncode == 0
    assert (watch / "quota_lock.agy").is_file()
    assert (task / ".th_digest.quota").is_file()


def test_artifacts_resolve_repo_relative_path_and_verify_clears_pythonpath(
    tmp_path: Path, sandbox: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
):
    workspace, _, _ = sandbox
    task = make_task(tmp_path, "repo_relative")
    (task / "task.md").write_text(
        "# Fixture\n## 기대 산출물\n- `_outputs/repo_relative/RESULTS.md`\n",
        encoding="utf-8",
    )
    output = workspace / "_outputs" / "repo_relative"
    output.mkdir(parents=True)
    (output / "RESULTS.md").write_text("ok\n", encoding="utf-8")
    (task / ".th_worktree").write_text(str(workspace), encoding="utf-8")
    assert wd.check_artifacts(task)["verdict"] == "PASS"

    verify = task / "verify.sh"
    verify.write_text(
        '#!/usr/bin/env bash\ntest -z "${PYTHONPATH-}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", "/opt/ros/jazzy")
    assert wd.check_script(task)["verdict"] == "PASS"
