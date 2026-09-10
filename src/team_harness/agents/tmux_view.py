"""Optional tmux windows that let a human watch worker output live.

This is a pure observability layer (TH-D12): each window runs `tail -f` on a
worker's stdout log file. It never touches the worker's stdin, never attaches
to the worker process itself, and its absence (tmux not installed, window
creation failing) never affects a run's outcome. Workers stay the one-shot
batch subprocesses TH-D2 describes; a tmux window is only a read-only view of
the same log file `read_agent_output` already reads.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import shlex
import shutil
import subprocess

_SAFE_WINDOW_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def tmux_available() -> bool:
    """True when a `tmux` binary is on PATH."""

    return shutil.which("tmux") is not None


def sanitize_window_name(name: str) -> str:
    """tmux window names are freeform, but keep them shell- and glob-safe."""

    cleaned = _SAFE_WINDOW_NAME.sub("-", name).strip("-")
    return cleaned or "worker"


class TmuxViewer:
    """Creates a dedicated tmux session with one window per worker.

    All calls shell out to the `tmux` binary and are deliberately
    best-effort: a failure to create or refresh a window is swallowed (not
    raised) so a missing/broken tmux never breaks the underlying run. Callers
    that care whether visibility actually worked should check
    `tmux_available()` up front.
    """

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name

    async def ensure_session(self) -> None:
        """Create the session (with a placeholder window) if it doesn't exist."""

        await asyncio.to_thread(self._ensure_session_sync)

    def _ensure_session_sync(self) -> None:
        try:
            probe = subprocess.run(
                ["tmux", "has-session", "-t", self.session_name],
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                return
            subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    self.session_name,
                    "-n",
                    "coordinator",
                ],
                capture_output=True,
                check=False,
            )
        except OSError:
            # tmux vanished (or never existed) between the caller's
            # tmux_available() check and this call. A view failing to appear
            # is never a reason to fail the underlying worker run.
            pass

    async def open_log_window(self, *, window_name: str, log_path: Path) -> None:
        """Open (or reopen) a window that tails `log_path` from the start."""

        await asyncio.to_thread(
            self._open_log_window_sync, window_name=window_name, log_path=log_path
        )

    def _open_log_window_sync(self, *, window_name: str, log_path: Path) -> None:
        self._ensure_session_sync()
        name = sanitize_window_name(window_name)
        # tail -F keeps following across the writer's open/close cycles and
        # tolerates the file not existing yet for a brief moment after spawn.
        tail_cmd = f"tail -n +1 -F -- {shlex.quote(str(log_path))}"
        try:
            subprocess.run(
                ["tmux", "new-window", "-t", self.session_name, "-n", name, tail_cmd],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass

    def attach_hint(self) -> str:
        return f"tmux attach -t {self.session_name}"
