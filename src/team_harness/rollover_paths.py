"""Observe worker boundaries while a process is running."""

import asyncio
from collections.abc import Callable
import hashlib
from pathlib import Path
import time
from typing import Literal

from team_harness.agents.manager import AgentManager
from team_harness.agents.manager import AgentState
from team_harness.agents.session_capture import _read_prefix_and_tail
from team_harness.agents.session_capture import extract_session_id
from team_harness.worker_context import measure_agent_context

Decision = Callable[
    [int | None, int, float | None, float, int, bool],
    Literal["continue", "rollover", "split_needed"],
]


class RolloverMonitor:
    def __init__(self, workdir: Path, budget: int, decide: Decision) -> None:
        self.workdir = workdir
        self.budget = budget
        self.decide = decide
        self.last_check = time.time()
        self.last_hash: str | None = None
        self.count = 0
        self.split_mtime: float | None = None

    def check(self, state: AgentState) -> str:
        try:
            data = (self.workdir / "WORKLOG.md").read_bytes()
            mtime = (self.workdir / "WORKLOG.md").stat().st_mtime
            digest = hashlib.sha256(data).hexdigest()
        except OSError:
            mtime, digest = None, None
        try:
            output = _read_prefix_and_tail(state.stdout_log, 65536)
        except OSError:
            output = b""
        sid = (
            extract_session_id(state.template, output, state.pre_generated_uuid)
            if state.template
            else state.session_id or state.context_session_id
        )
        measurement = measure_agent_context(
            agent_type=state.agent_type,
            session_id=sid,
            sessions_root=state.codex_sessions_root,
        )
        action = self.decide(
            measurement.input_tokens
            if measurement.measurement_status == "measured"
            else None,
            self.budget,
            mtime,
            self.last_check,
            self.count,
            digest != self.last_hash,
        )
        if action == "rollover":
            self.count += 1
            self.last_check = mtime or self.last_check
            self.last_hash = digest
        elif action == "split_needed" and self.split_mtime != mtime:
            self.split_mtime = mtime
            state.failure_classification = {
                "category": "split_needed",
                "detail": "작업 분할 필요",
            }
            print("작업 분할 필요", flush=True)
        return action

    def prompt(self, prompt: str) -> str:
        sections = []
        for name in ("WORKLOG.md", "STATUS"):
            try:
                lines = (
                    (self.workdir / name)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()[-30:]
                )
            except OSError:
                lines = []
            sections.append(f"### {name}\n" + "\n".join(lines))
        return (
            prompt
            + f"\n\n## 롤오버 ({self.count}/3)\n"
            + "\n".join(sections)
            + "\n이미 된 것은 다시 하지 말고 이어서 진행하십시오.\n"
        )


async def wait_with_rollover(
    manager: AgentManager,
    state: AgentState,
    monitor: RolloverMonitor,
    timeout: float | None = None,
) -> bool:
    """Return true only if a boundary triggered termination for rollover."""
    waiter = asyncio.create_task(state.proc.wait())
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        while True:
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            done, _ = await asyncio.wait(
                {waiter}, timeout=min(1.0, remaining) if remaining is not None else 1.0
            )
            if done:
                return False
            if monitor.check(state) == "rollover":
                manager.kill(state.id)
                await manager.ensure_group_dead(state.id)
                await waiter
                state.status = "running"
                state.finished_at = None
                return True
    finally:
        if not waiter.done():
            waiter.cancel()
