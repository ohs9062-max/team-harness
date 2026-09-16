# pyright: reportMissingParameterType=false

import asyncio
import os
from pathlib import Path

import pytest

from team_harness.tools.shell_tools import bash
from team_harness.tools.shell_tools import BASH_SCHEMA
from team_harness.tools.shell_tools import DEFAULT_BASH_TIMEOUT_SECONDS


async def _assert_all_terminated(*pid_paths: Path, deadline_s: float = 5.0) -> None:
    """Assert every recorded pid is gone, allowing a bounded reap window.

    `bash` kills the whole process group and reaps the shell it started, but
    the shell's own children are grandchildren of this test process. When the
    shell dies first, a killed grandchild is re-parented to init and stays a
    zombie until init reaps it — and `os.kill(zombie, 0)` still succeeds. That
    window is only ~20ms, so asserting termination the instant `task.cancel()`
    returns fails most of the time on a loaded machine. Poll instead: this
    still proves the processes die, without depending on how fast init runs.
    """
    pids = [int(path.read_text()) for path in pid_paths]
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + deadline_s
    while True:
        alive = [pid for pid in pids if _pid_resolves(pid)]
        if not alive:
            return
        if loop.time() >= give_up_at:
            pytest.fail(f"pids still resolve {deadline_s}s after cancellation: {alive}")
        await asyncio.sleep(delay=0.01)


def _pid_resolves(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process now owns a recycled pid; ours is gone.
        return False
    return True


@pytest.mark.asyncio
async def test_bash_echo_and_stdin_devnull():
    """The shell tool should combine output and keep stdin non-interactive."""
    assert await bash(command="echo hello") == "hello\n"
    output = await bash(command='python3 -c "input()"')
    assert "EOFError" in output


@pytest.mark.asyncio
async def test_bash_starts_command_in_new_session(monkeypatch):
    """Every shell command should lead a dedicated process group."""
    observed_arguments = {}

    class FakeProcess:
        """Minimal successful subprocess double for spawn-argument inspection."""

        async def communicate(self):
            """Return one successful combined-output result."""
            return b"ok\n", None

    async def fake_create_subprocess_shell(**kwargs):
        """Capture subprocess arguments and return the successful double."""
        observed_arguments.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_shell", fake_create_subprocess_shell)
    assert await bash(command="echo ok") == "ok\n"
    assert observed_arguments["start_new_session"] is True


@pytest.mark.asyncio
async def test_bash_timeout(monkeypatch):
    """An explicit timeout should be honored and kill the whole process group."""
    observed_timeouts = []

    async def fake_wait_for(fut, timeout):
        """Record the timeout while simulating expiration of the wait."""
        observed_timeouts.append(timeout)
        if timeout == 907_800:
            fut.close()
            raise asyncio.TimeoutError
        return await fut

    monkeypatch.setattr("asyncio.wait_for", fake_wait_for)
    monkeypatch.setattr(
        "team_harness.tools.shell_tools.BASH_TERMINATION_GRACE_SECONDS", 0.0
    )
    assert await bash(command="sleep 200", timeout_seconds=907_800) == (
        "ERROR: command timed out after 907800 seconds."
    )
    assert observed_timeouts == [907_800, 0.0]


@pytest.mark.asyncio
async def test_bash_default_timeout_remains_120_seconds(monkeypatch):
    """Omitting the new argument should preserve the legacy timeout contract."""

    async def fake_wait_for(fut, timeout):
        """Close the communication coroutine and simulate default expiration."""
        if timeout == DEFAULT_BASH_TIMEOUT_SECONDS:
            fut.close()
            raise asyncio.TimeoutError
        return await fut

    monkeypatch.setattr("asyncio.wait_for", fake_wait_for)
    monkeypatch.setattr(
        "team_harness.tools.shell_tools.BASH_TERMINATION_GRACE_SECONDS", 0.0
    )
    assert await bash(command="sleep 200") == (
        "ERROR: command timed out after 120 seconds."
    )


@pytest.mark.asyncio
async def test_bash_cancellation_cleans_up_process_group(tmp_path, monkeypatch):
    """Cancelling a shell call should not leave its command process running."""
    shell_pid_path = tmp_path / "shell.pid"
    child_pid_path = tmp_path / "child.pid"
    monkeypatch.setattr(
        "team_harness.tools.shell_tools.BASH_TERMINATION_GRACE_SECONDS", 0.0
    )
    task = asyncio.create_task(
        coro=bash(
            command=(
                f"printf '%s' \"$$\" > '{shell_pid_path}'; "
                f"sleep 200 & printf '%s' \"$!\" > '{child_pid_path}'; wait"
            ),
            timeout_seconds=200,
        )
    )
    while not shell_pid_path.exists() or not child_pid_path.exists():
        await asyncio.sleep(delay=0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_all_terminated(shell_pid_path, child_pid_path)


@pytest.mark.asyncio
async def test_bash_cancellation_escalates_for_sigterm_ignoring_group(
    tmp_path, monkeypatch
):
    """Cleanup should SIGKILL a command group that ignores its SIGTERM grace."""
    shell_pid_path = tmp_path / "stubborn-shell.pid"
    child_pid_path = tmp_path / "stubborn-child.pid"
    monkeypatch.setattr(
        "team_harness.tools.shell_tools.BASH_TERMINATION_GRACE_SECONDS", 0.05
    )
    task = asyncio.create_task(
        coro=bash(
            command=(
                "trap '' TERM; "
                f"printf '%s' \"$$\" > '{shell_pid_path}'; "
                f"sleep 200 & printf '%s' \"$!\" > '{child_pid_path}'; wait"
            ),
            timeout_seconds=200,
        )
    )
    while not shell_pid_path.exists() or not child_pid_path.exists():
        await asyncio.sleep(delay=0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_all_terminated(shell_pid_path, child_pid_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds", [True, 0, -1, 1.5, "120"])
async def test_bash_rejects_invalid_timeout(timeout_seconds):
    """Invalid direct-call timeouts should fail before starting a subprocess."""
    result = await bash(command="echo should-not-run", timeout_seconds=timeout_seconds)
    assert result.startswith("ERROR: timeout_seconds must")


def test_bash_schema_exposes_bounded_timeout():
    """The coordinator schema should advertise the safe long-command control."""
    timeout_schema = BASH_SCHEMA["function"]["parameters"]["properties"][
        "timeout_seconds"
    ]
    assert timeout_schema == {
        "type": "integer",
        "minimum": 1,
        "default": DEFAULT_BASH_TIMEOUT_SECONDS,
        "description": (
            "Maximum runtime before the command and its descendants are terminated. "
            "Defaults to 120 seconds; explicitly raise this for a known long-running "
            "foreground command."
        ),
    }
