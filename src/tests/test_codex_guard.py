"""Codex guard subprocess behavior and persistent state."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

GUARD = Path(__file__).resolve().parents[2] / "scripts/hooks/th-codex-guard.py"


def run(
    tmp_path: Path,
    command: str = "true",
    event: str = "PreToolUse",
    output: str = "",
    env: dict[str, str] | None = None,
    raw: str | None = None,
    session: str = "test",
) -> bool:
    environ = {**os.environ, "TH_WATCH_DIR": str(tmp_path)}
    for key in ("TH_ROLE", "TH_JOB_TASK", "TH_CODEX_GUARD", "ORCA_PANE_KEY"):
        environ.pop(key, None)
    environ.update(env or {})
    payload = {
        "hook_event_name": event,
        "session_id": session,
        "tool_input": {"cmd": command},
        "tool_response": output,
    }
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=raw if raw is not None else json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=environ,
    )
    return bool(result.stdout)


def test_sleep_limits(tmp_path: Path) -> None:
    assert not run(tmp_path, "sleep 9")
    assert run(tmp_path, "sleep 10")
    assert not run(tmp_path, "sleep 59", env={"TH_ROLE": "worker"})
    assert run(tmp_path, "sleep 60", env={"TH_ROLE": "worker"})
    assert run(tmp_path, "while true; do sleep 10; done")
    assert not run(tmp_path, "timeout 10 command")
    assert run(tmp_path, "watch -n 10 command")
    assert not run(tmp_path, "sleep 59", env={"TH_JOB_TASK": "job"})


def test_polling_window(tmp_path: Path) -> None:
    assert not run(tmp_path, "cat /tmp/a/STATUS")
    assert not run(tmp_path, "cat /tmp/a/STATUS")
    assert run(tmp_path, "cat /tmp/a/STATUS")
    assert not run(tmp_path, "cat /tmp/b/STATUS")
    state_path = next((tmp_path / "codex_guard").glob("*.json"))
    state = json.loads(state_path.read_text())
    for entry in state["checks"]:
        entry[0] = time.time() - 601
    state_path.write_text(json.dumps(state))
    assert not run(tmp_path, "cat /tmp/a/STATUS")
    assert not run(tmp_path, "ths", env={"TH_ROLE": "worker"})


def test_denial_and_fail_open(tmp_path: Path) -> None:
    assert not run(tmp_path, "cat /tmp/a", "PostToolUse", "permission denied")
    assert run(tmp_path, "cat /tmp/a")
    assert not run(tmp_path, "cat /tmp/b")
    assert not run(tmp_path, "sleep 100", env={"TH_CODEX_GUARD": "off"})
    assert not run(tmp_path, raw="{")
    assert (
        json.loads((tmp_path / "guard.log").read_text().splitlines()[0])["agent"]
        == "codex"
    )


def test_timeout_train(tmp_path: Path) -> None:
    assert not run(tmp_path, "timeout 900 python train.py")
    assert not run(tmp_path, "timeout 900 python train.py", env={"TH_ROLE": "worker"})


def test_timeout_curl(tmp_path: Path) -> None:
    assert not run(tmp_path, "timeout 30 curl -s x")


def test_timeout_sleep(tmp_path: Path) -> None:
    assert run(tmp_path, "timeout 900 sleep 10")


def test_timeout_shell_sleep(tmp_path: Path) -> None:
    assert run(tmp_path, "timeout 900 bash -c 'echo hi; sleep 10'")


def test_until_sleep(tmp_path: Path) -> None:
    assert run(tmp_path, "until false; do sleep 10; done")


def test_watch_interval(tmp_path: Path) -> None:
    assert run(tmp_path, "watch -n 10 echo hi")


def test_prompt_resets_polling(tmp_path: Path) -> None:
    assert not run(tmp_path, "ths")
    assert not run(tmp_path, "ths")
    assert run(tmp_path, "ths")
    assert not run(tmp_path, event="UserPromptSubmit")
    assert not run(tmp_path, "ths")


def test_prompt_resets_denial(tmp_path: Path) -> None:
    assert not run(tmp_path, "cat /tmp/a", "PostToolUse", "permission denied")
    assert run(tmp_path, "cat /tmp/a")
    assert not run(tmp_path, event="UserPromptSubmit")
    assert not run(tmp_path, "cat /tmp/a")


def test_sessions_are_isolated(tmp_path: Path) -> None:
    run(tmp_path, "cat /tmp/a", "PostToolUse", "permission denied", session="one")
    assert not run(tmp_path, "cat /tmp/a", session="two")
    run(tmp_path, event="UserPromptSubmit", session="two")
    assert run(tmp_path, "cat /tmp/a", session="one")


def test_worker_role(tmp_path: Path) -> None:
    assert not run(tmp_path, "sleep 59", env={"TH_ROLE": "worker"})
    assert run(tmp_path, "sleep 60", env={"TH_ROLE": "worker"})


def test_guard_disabled(tmp_path: Path) -> None:
    assert not run(tmp_path, "sleep 600", env={"TH_CODEX_GUARD": "off"})


def test_invalid_json(tmp_path: Path) -> None:
    assert not run(tmp_path, raw="{")


def test_unread_session_labels(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    state = watch / "inbox_unread" / "owner.tsv"
    state.parent.mkdir(parents=True)
    owner = tmp_path / "owner"
    state.write_text(
        f"{int(time.time())}\t10\t0\t{owner}\tone\tcodex\n"
        f"{int(time.time())}\t20\t0\t{owner}\ttwo\tclaude\n"
    )
    result = subprocess.run(
        ["bash", str(GUARD.parents[1] / "th-status.sh")],
        env={**os.environ, "TH_WATCH_DIR": str(watch)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"ℹ Codex 창 확인 필요: {owner} 1건" in result.stdout
    assert "⚠ 알림 미수신:" in result.stdout
    assert "세션 two waiter 없음" in result.stdout
