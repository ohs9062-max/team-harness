from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from team_harness import stop_policy
from team_harness.stop_policy import classify_stop


@pytest.fixture(autouse=True)
def recent_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stop_policy, "_last_launch_time", lambda *args: time.time() - 10
    )


def test_incomplete_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE: work remains")
    assert classify_stop(tmp_path) == "self_stop"


def test_ambiguous_failed_continues(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("작업을 마치지 못함")
    assert classify_stop(tmp_path) == "self_stop"


def test_quota_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("quota exhausted")
    assert classify_stop(tmp_path) == "allowed_fail"


def test_rate_limit_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("rate limit")
    assert classify_stop(tmp_path) == "allowed_fail"


def test_http_429_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("HTTP 429 Too Many Requests")
    assert classify_stop(tmp_path) == "allowed_fail"


def test_capacity_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("용량 부족")
    assert classify_stop(tmp_path) == "allowed_fail"


def test_permission_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("permission denied")
    assert classify_stop(tmp_path) == "allowed_fail"


def test_auth_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("인증 실패")
    assert classify_stop(tmp_path) == "allowed_fail"


def test_license_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("license error")
    assert classify_stop(tmp_path) == "allowed_fail"


def test_time_limit_failed(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("시간 제한")
    assert classify_stop(tmp_path) == "allowed_fail"


def test_remaining_done(tmp_path: Path) -> None:
    (tmp_path / "DONE").write_text("남은 작업이 있습니다")
    assert classify_stop(tmp_path) == "self_stop"


def test_verify_failed_done(tmp_path: Path) -> None:
    (tmp_path / "DONE").write_text("completed")
    (tmp_path / "VERIFY_FAILED").touch()
    assert classify_stop(tmp_path) == "self_stop"


def test_normal_done(tmp_path: Path) -> None:
    (tmp_path / "DONE").write_text("completed")
    assert classify_stop(tmp_path) == "ok"


@pytest.mark.parametrize(
    "reason",
    [
        "같은 수정 2회 실패",
        "동일 수정 실패 두 차례",
        "설정 수정 3회 실패",
        "setup failed three times",
        "다른 방식 2회 거부",
        "different approach denied twice",
    ],
)
def test_allowed_guard_reasons(tmp_path: Path, reason: str) -> None:
    (tmp_path / "FAILED").write_text(reason)
    assert classify_stop(tmp_path) == "allowed_fail"


def test_auto_continue_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "task.md").write_text("kind: code\n")
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    calls: list[int] = []
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    monkeypatch.setattr(
        stop_policy, "relaunch_worker", lambda *args, **kwargs: calls.append(1)
    )
    for index in range(2):
        (tmp_path / "FAILED").write_text("INCOMPLETE" + str(index))
        assert "자동 계속(" in stop_policy.continue_stopped_task(
            tmp_path, tmp_path, tmp_path
        )
    (tmp_path / "FAILED").write_text("INCOMPLETE final")
    assert "작업 분할 필요" in stop_policy.continue_stopped_task(
        tmp_path, tmp_path, tmp_path
    )
    assert len(calls) == 2
    assert "## 자동 계속 (2차)" in (tmp_path / "task.md").read_text()


def test_killed_is_excluded(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    (tmp_path / "KILLED").touch()
    assert stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path) == ""


def test_research_without_verify_is_excluded(tmp_path: Path) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    (tmp_path / "task.md").write_text("kind: research\n")
    assert stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path) == ""


def test_quota_lock_prevents_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    (tmp_path / "task.md").write_text("kind: code\n")
    (tmp_path / "quota_lock.codex").touch()
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    assert stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path) == ""
    assert not (tmp_path / ".auto_continue_count").exists()
    assert not (tmp_path / ".th_daemon.settled").exists()


def test_no_continuation_required_marks_settled(tmp_path: Path) -> None:
    (tmp_path / "DONE").write_text("complete")
    assert stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path) == ""
    assert (tmp_path / ".th_daemon.settled").is_file()


def test_relaunch_error_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    (tmp_path / "task.md").write_text("kind: code\n")
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("launcher unavailable")

    monkeypatch.setattr(stop_policy, "relaunch_worker", fail)
    assert "자동 계속 실패" in stop_policy.continue_stopped_task(
        tmp_path, tmp_path, tmp_path
    )


def test_verification_tail_in_continue_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "DONE").write_text("done")
    (tmp_path / "VERIFY_FAILED").touch()
    (tmp_path / "task.md").write_text("kind: code\n")
    (tmp_path / ".th_verify_output.txt").write_text(
        "\n".join(f"line-{i}" for i in range(25))
    )
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    monkeypatch.setattr(stop_policy, "relaunch_worker", lambda *args, **kwargs: None)
    stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path)
    text = (tmp_path / "task.md").read_text()
    assert "line-4\n" not in text
    assert "line-5\n" in text
    assert "line-24\n" in text


def test_verification_marker_supplies_output_if_log_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "DONE").write_text("done")
    (tmp_path / "VERIFY_FAILED").write_text("verification failed\n")
    (tmp_path / "task.md").write_text("kind: code\n")
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    monkeypatch.setattr(stop_policy, "relaunch_worker", lambda *args, **kwargs: None)
    stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path)
    assert "verification failed" in (tmp_path / "task.md").read_text()


def test_continue_relaunch_uses_rollover_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from team_harness import worker_relaunch

    calls: list[dict] = []
    monkeypatch.setattr(
        worker_relaunch.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs)
    )
    worker_relaunch.relaunch_worker(tmp_path, [], tmp_path, auto_continue=True)
    assert calls[0]["env"]["TH_ROLLOVER"] == "1"
    assert calls[0]["env"]["TH_AUTO_CONTINUE"] == "1"


def test_watchd_routes_completed_task_to_shared_policy(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "DONE").write_text("남은 작업")
    (task / ".th_daemon.done").touch()
    (task / ".th_digest.done").touch()
    (task / "VERIFIED").touch()
    library = Path(__file__).resolve().parents[2] / "scripts" / "th-watch-lib.sh"
    script = r"""
source "$1"
inspect_context_and_job() { :; }
task_live_pids() { :; }
worker_digest_program() { echo dummy; }
python3() { echo DONE; }
th_python() { [[ $1 == -m && $2 == team_harness.stop_policy ]] || return 1; printf '자동 계속(1/2): test'; }
callback() { printf '%s\n' "$3"; }
inspect_task_event "$2" "$3" .th_daemon 1 '' callback
"""
    result = subprocess.run(
        ["bash", "-c", script, "test", str(library), str(task), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "자동 계속(1/2): test" in result.stdout


def test_concurrent_watchers_start_only_one_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    (tmp_path / "task.md").write_text("kind: code\n")
    calls: list[int] = []
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])

    def launch(*args: object, **kwargs: object) -> None:
        calls.append(1)
        (tmp_path / "FAILED").unlink()

    monkeypatch.setattr(stop_policy, "relaunch_worker", launch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: stop_policy.continue_stopped_task(
                    tmp_path, tmp_path, tmp_path
                ),
                range(2),
            )
        )
    assert len(calls) == 1
    assert sum(bool(result) for result in results) == 1


def test_text_terminal_result_classification() -> None:
    assert stop_policy.classify_stop_text("FAILED: INCOMPLETE") == "self_stop"
    assert stop_policy.classify_stop_text("DONE: 남은 작업 있음") == "self_stop"
    assert stop_policy.classify_stop_text("FAILED: quota exhausted") == "allowed_fail"
    assert stop_policy.classify_stop_text("DONE: complete") == "ok"


@pytest.mark.asyncio
async def test_protocol_continues_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from team_harness.agents.template import AgentTemplate
    from team_harness.config import Config
    from team_harness.protocol import mode_c

    count = tmp_path / "count"
    script = tmp_path / "worker.sh"
    script.write_text(
        f'''if [ ! -f "{count}" ]; then touch "{count}"; echo "FAILED: INCOMPLETE"; else echo "DONE: complete"; fi\n'''
    )
    config = Config()
    config.agent_templates = {
        "codex": AgentTemplate(command=("sh", str(script)), model_flag=None)
    }
    calls = []
    original = stop_policy.classify_stop_text

    def classify(text: str) -> stop_policy.StopClassification:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(mode_c, "classify_stop_text", classify)
    runner = mode_c.TeamHarnessAgentRunner(config=config, log_dir=tmp_path / "logs")
    result = await runner.run_agent(
        agent_type="codex", prompt="p", cwd=str(tmp_path), timeout_sec=10
    )
    assert result.success and result.output_text.strip() == "DONE: complete"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_direct_continuation_shared_wait_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from datetime import datetime
    from datetime import timezone

    from team_harness.agents import spawner
    from team_harness.agents.manager import AgentManager
    from team_harness.agents.manager import AgentState
    from team_harness.agents.template import AgentTemplate
    from team_harness.config import Config
    from team_harness.tools import agent_tools

    script = tmp_path / "worker.sh"
    script.write_text('echo "FAILED: INCOMPLETE"\n')
    config = Config()
    config.agent_templates = {
        "codex": AgentTemplate(command=("sh", str(script)), model_flag=None)
    }
    stdout, stderr = tmp_path / "out", tmp_path / "err"
    result = await spawner.spawn(
        agent_id="a",
        agent_type="codex",
        prompt="p",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path,
        stdout_path=stdout,
        stderr_path=stderr,
    )
    state = AgentState(
        id="a",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=result.proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
        template=result.template,
    )
    calls = []
    original = stop_policy.classify_stop_text

    def classify(text: str) -> stop_policy.StopClassification:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(agent_tools, "classify_stop_text", classify)
    agent_tools._install_continuation(
        state,
        config=config,
        run_dir=tmp_path,
        full_prompt="p",
        extra_env={},
        model=None,
        effort=None,
        flags=None,
        agents=None,
    )
    manager = AgentManager()
    manager.register(state)
    assert await asyncio.gather(manager.wait_one("a"), manager.wait_one("a")) == [1, 1]
    assert len(calls) == 3
    assert state.continuation_exhausted and state.status == "failed"


def test_expired_capacity_lock_allows_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    (tmp_path / "task.md").write_text("kind: code\n")
    (tmp_path / "quota_lock.codex").write_text("capacity | expires=1\n")
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    calls: list[int] = []
    monkeypatch.setattr(
        stop_policy, "relaunch_worker", lambda *a, **kw: calls.append(1)
    )
    assert "자동 계속(" in stop_policy.continue_stopped_task(
        tmp_path, tmp_path, tmp_path
    )
    assert calls == [1]


def test_future_capacity_lock_prevents_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    (tmp_path / "task.md").write_text("kind: code\n")
    (tmp_path / "quota_lock.codex").write_text("capacity | expires=9999999999\n")
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    assert stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path) == ""
    assert not (tmp_path / ".auto_continue_count").exists()


@pytest.mark.parametrize(
    "case", ["old", "before_launch", "pre_deployment", "missing", "future"]
)
def test_ineligible_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    now = time.time()
    marker = tmp_path / "FAILED"
    marker.write_text("INCOMPLETE")
    (tmp_path / "task.md").write_text("kind: code\n")
    launch = now - 10
    if case == "old":
        os.utime(marker, (now - 1801, now - 1801))
        launch = now - 2000
    elif case == "before_launch":
        launch = now + 1
    elif case == "pre_deployment":
        launch = stop_policy._DEPLOYED - 1
    elif case == "missing":
        launch = None
    elif case == "future":
        os.utime(marker, (now + 60, now + 60))
    monkeypatch.setattr(stop_policy, "_last_launch_time", lambda *args: launch)
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    calls: list[int] = []
    monkeypatch.setattr(
        stop_policy, "relaunch_worker", lambda *args, **kwargs: calls.append(1)
    )
    assert stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path) == ""
    assert calls == []


def test_recent_failure_is_processed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "FAILED").write_text("INCOMPLETE")
    (tmp_path / "task.md").write_text("kind: code\n")
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    calls: list[int] = []
    monkeypatch.setattr(
        stop_policy, "relaunch_worker", lambda *args, **kwargs: calls.append(1)
    )
    assert "자동 계속(" in stop_policy.continue_stopped_task(
        tmp_path, tmp_path, tmp_path
    )
    assert stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path) == ""
    assert len(calls) == 1


def test_launch_evidence_from_ledger_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime
    from datetime import timezone
    import json

    monkeypatch.undo()
    now = int(time.time())
    (tmp_path / "usage.jsonl").write_text(
        json.dumps(
            {
                "task": str(tmp_path),
                "at": datetime.fromtimestamp(now - 20, timezone.utc).isoformat(),
            }
        )
        + "\n"
    )
    (tmp_path / "STATUS").write_text(
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 10))
        + " LAUNCH worker=codex\n"
    )
    assert stop_policy._last_launch_time(tmp_path, tmp_path) == now - 10


def test_verified_done_overrides_auxiliary_failure(tmp_path: Path) -> None:
    import json

    (tmp_path / "DONE").write_text("completed")
    (tmp_path / "verify.sh").write_text("exit 0\n")
    (tmp_path / "VERIFY_FAILED").write_text("- 산출물: [FAIL] missing\n")
    (tmp_path / "verify_result.json").write_text(
        json.dumps(
            {"items": {"verify": {"verdict": "PASS"}, "산출물": {"verdict": "FAIL"}}}
        )
    )
    assert classify_stop(tmp_path) == "ok"
    assert stop_policy.classify_stop_text("DONE: remaining", tmp_path) == "ok"


def test_done_unchanged_does_not_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "task.md").write_text("kind: code\n")
    (tmp_path / "DONE").write_text("remaining")
    (tmp_path / "result.csv").write_text("result")
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])
    calls: list[int] = []
    monkeypatch.setattr(
        stop_policy, "relaunch_worker", lambda *args, **kwargs: calls.append(1)
    )
    assert "자동 계속(" in stop_policy.continue_stopped_task(
        tmp_path, tmp_path, tmp_path
    )
    (tmp_path / "DONE").write_text("remaining")
    assert not stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path)
    assert calls == [1]
    (tmp_path / "result.csv").write_text("changed")
    assert "자동 계속(" in stop_policy.continue_stopped_task(
        tmp_path, tmp_path, tmp_path
    )


def test_watchd_done_while_worker_alive_then_exits(tmp_path: Path) -> None:
    from team_harness.worker_digest import verify

    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("kind: code\n")
    (task / "verify.sh").write_text("exit 0\n")
    worker = subprocess.Popen(
        [
            "python3",
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('complete'); print('ready', flush=True); sys.stdin.read()",
            str(task / "DONE"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert worker.stdout is not None
        assert worker.stdout.readline() == b"ready\n"
        assert (task / "DONE").exists()
        (task / "worker.pid").write_text(str(worker.pid))
        result, _ = verify(task, "done")
        assert result["items"]["verify"]["verdict"] == "PASS"
        assert classify_stop(task) == "ok"
        (task / ".th_digest.done").touch()
        library = Path(__file__).resolve().parents[2] / "scripts" / "th-watch-lib.sh"
        script = r"""
source "$1"
inspect_context_and_job() { :; }
task_live_pids() { :; }
worker_digest_program() { echo dummy; }
python3() { echo DONE; }
th_python() { command "$4" -m team_harness.stop_policy "$2" "$3" "$3"; }
callback() { printf '%s\n' "$3"; }
inspect_task_event "$2" "$3" .th_daemon 1 '' callback
"""
        # The shell function has its own argument list, so pass Python via environment.
        script = script.replace(
            'command "$4" -m team_harness.stop_policy "$2" "$3" "$3"',
            'command "$TEST_PYTHON" "$@"',
        )
        env = dict(os.environ, TEST_PYTHON=sys.executable)
        before = subprocess.run(
            ["bash", "-c", script, "test", str(library), str(task), str(tmp_path)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "자동 계속" not in before.stdout
        worker.communicate(timeout=5)
        (task / ".th_daemon.done").touch()
        proc = subprocess.run(
            ["bash", "-c", script, "test", str(library), str(task), str(tmp_path)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "자동 계속" not in proc.stdout
        assert not (task / ".auto_continue_count").exists()
    finally:
        if worker.poll() is None:
            worker.communicate(timeout=5)


def test_transient_state_failure_is_rechecked_after_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    (tmp_path / "DONE").write_text("complete")
    (tmp_path / "VERIFY_FAILED").write_text(
        "- 상태: [FAIL] 작업 미완료 (생존 PID: [123])\n"
    )
    (tmp_path / "verify_result.json").write_text(
        json.dumps(
            {
                "items": {
                    "상태": {
                        "verdict": "FAIL",
                        "reason": "작업 미완료 (생존 PID: [123])",
                    },
                    "verify": {"verdict": "SKIP"},
                }
            }
        )
    )
    monkeypatch.setattr(stop_policy, "live_pids", lambda _: [123])
    assert classify_stop(tmp_path) == "ok"
    monkeypatch.setattr(stop_policy, "live_pids", lambda _: [])

    def reverify(task: Path, event: str) -> tuple[dict, str]:
        (task / "VERIFY_FAILED").unlink()
        return {
            "items": {"상태": {"verdict": "PASS"}, "verify": {"verdict": "SKIP"}}
        }, ""

    monkeypatch.setattr(stop_policy, "verify", reverify)
    assert classify_stop(tmp_path) == "ok"


def test_auto_continue_event_includes_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "task.md").write_text("kind: code\n")
    (tmp_path / "DONE").write_text("complete")
    (tmp_path / "VERIFY_FAILED").write_text("- 산출물: [FAIL] missing report\n")
    monkeypatch.setattr(stop_policy, "launch_arguments", lambda *args: ["t", "codex"])

    def relaunch(task: Path, *args: object, **kwargs: object) -> None:
        (task / "VERIFY_FAILED").unlink()

    monkeypatch.setattr(stop_policy, "relaunch_worker", relaunch)
    event = stop_policy.continue_stopped_task(tmp_path, tmp_path, tmp_path)
    assert "산출물: [FAIL] missing report" in event
