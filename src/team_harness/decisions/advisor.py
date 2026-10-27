"""Bounded, non-controlling Jev advice shared by every worker launch path."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from functools import lru_cache
import json
import math
import os
from pathlib import Path
from queue import Empty
from queue import Queue
from threading import Thread
import time
from typing import Callable

from team_harness.decisions.jev_backend import JevAnswer
from team_harness.decisions.jev_backend import JevBackend
from team_harness.decisions.models import DecisionRequest
from team_harness.decisions.models import DecisionRouterSettings
from team_harness.decisions.watch_cli import load_secrets
from team_harness.decisions.watch_cli import MockWatchBackend
from team_harness.tracking.models import DecisionRecord

TIER_OPTIONS = {
    "TERRA_MEDIUM": "Routine bounded code changes; Terra medium is sufficient.",
    "TERRA_HIGH": "Complex code reasoning within a well-defined task; Terra high.",
    "SOL_MEDIUM": "Broad design or integration needing Sol reasoning at medium effort.",
    "SOL_HIGH": "Hard architecture or ambiguous cross-system debugging; Sol high.",
}
FAILURE_OPTIONS = {
    "RETRY_SELF_ERROR": "Worker made a transient self-correctable execution error.",
    "ENV_FIX": "Environment, dependency, permission or quota must be repaired first.",
    "BRIEF_ERROR": "Task instructions or scope need correction before retrying.",
    "ESCALATE_TIER": "Task complexity exceeds current model tier.",
    "ASK_USER": "A user decision or missing information is required.",
}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Advice:
    choice: str
    confidence: float

    def line(self) -> str:
        return f"jev: {self.choice} {self.confidence:.2f}"


@lru_cache(maxsize=None)
def _settings(workspace: str = "") -> DecisionRouterSettings:
    # Configuration and secrets are read once, never once per worker in a batch.
    from team_harness.config import _merged_config_layers
    from team_harness.config import _parse_decision_router

    return _parse_decision_router(
        _merged_config_layers(Path(workspace) if workspace else None)
    )


@lru_cache(maxsize=1)
def _secrets() -> None:
    load_secrets()


def _enabled(settings: DecisionRouterSettings | None, workspace: str = "") -> bool:
    override = os.environ.get("TEAM_HARNESS_JEV", "").strip().lower()
    if override:
        return override not in _FALSE
    return (settings or _settings(workspace)).enabled


def _tail(path: Path, limit: int, lines: int | None = None) -> str:
    try:
        with path.open("rb") as stream:
            if lines is not None:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - limit))
            text = stream.read(limit).decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:]) if lines else text
    except OSError:
        return ""


def _decide(
    text: str,
    *,
    kind: str,
    options: dict[str, str],
    path: str,
    task: str,
    settings: DecisionRouterSettings | None,
    record: Callable[[DecisionRecord], None] | None,
    workspace: str = "",
) -> Advice | None:
    started = time.monotonic()
    choice: str | None = None
    confidence: float | None = None
    reason: str | None = None
    advice = None
    active = False
    model = "jev-latest"
    try:
        if not _enabled(settings, workspace):
            return None
        active = True
        settings = settings or _settings(workspace)
        model = settings.model
        _secrets()
        timeout = float(os.environ.get("TEAM_HARNESS_JEV_TIMEOUT", "1.5"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("TEAM_HARNESS_JEV_TIMEOUT must be positive and finite")
        key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")
        mock = os.environ.get("TH_TEST_MOCK_JEV") == "1"
        if not key and not mock:
            raise ValueError("TYPESAFE_API_KEY or JEV_API_KEY is not set")
        backend = (
            MockWatchBackend(os.environ.get("TH_TEST_JEV_CHOICE"))
            if mock
            else JevBackend(
                api_url=settings.api_url,
                api_key=key or "",
                model=model,
                timeout_s=timeout,
            )
        )
        request = DecisionRequest(
            kind="agent_selection" if kind == "tier" else "worker_failure",
            state=text[:4000],
            options=options,
            fallback=next(iter(options)),
            instructions="Recommend one option only. Advisory; never authorize automatic changes.",
        )
        results: Queue[JevAnswer | Exception] = Queue(maxsize=1)

        async def invoke() -> JevAnswer:
            return await asyncio.wait_for(backend.decide(request), timeout=timeout)

        def run() -> None:
            try:
                results.put(asyncio.run(invoke()))
            except Exception as exc:
                results.put(exc)

        Thread(target=run, daemon=True).start()
        try:
            answer = results.get(timeout=timeout)
        except Empty as exc:
            raise TimeoutError("Jev advice deadline exceeded") from exc
        if isinstance(answer, Exception):
            raise answer
        choice, confidence = answer.choice, answer.confidence
        if choice not in options:
            reason = "invalid choice"
        elif (
            confidence is None
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            reason = "missing or invalid confidence"
        elif confidence < settings.confidence_threshold:
            reason = "low confidence"
        else:
            advice = Advice(choice, confidence)
    except (Exception, SystemExit) as exc:
        # Config parsing may raise SystemExit. Advisory calls must never stop work.
        if not active and os.environ.get("TEAM_HARNESS_JEV", "").lower() in _FALSE:
            return None
        active = True
        reason = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if active:
            latency = round((time.monotonic() - started) * 1000)
            now = datetime.now(timezone.utc)
            entry = dict(
                path=path,
                kind=kind,
                choice=choice,
                confidence=confidence,
                latency_ms=latency,
                reason=reason,
                task=task,
                timestamp=now.isoformat(),
            )
            try:
                watch = Path(
                    os.environ.get(
                        "TH_WATCH_DIR", str(Path.home() / ".team-harness/watch")
                    )
                )
                watch.mkdir(parents=True, exist_ok=True)
                with (watch / "jev_decisions.log").open(
                    "a", encoding="utf-8"
                ) as stream:
                    stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
            if record is not None:
                try:
                    record(
                        DecisionRecord(
                            kind=f"jev_{kind}",
                            choice=choice or "NONE",
                            confidence=confidence,
                            applied=False,
                            fallback_reason=reason,
                            backend="jev",
                            model=model,
                            input_chars=len(text[:4000]),
                            latency_ms=latency,
                            recorded_at=now,
                        )
                    )
                except Exception as exc:
                    try:
                        watch = Path(
                            os.environ.get(
                                "TH_WATCH_DIR", str(Path.home() / ".team-harness/watch")
                            )
                        )
                        watch.mkdir(parents=True, exist_ok=True)
                        entry["reason"] = (
                            f"callback_error: {type(exc).__name__}: {exc}"[:500]
                        )
                        with (watch / "jev_decisions.log").open(
                            "a", encoding="utf-8"
                        ) as stream:
                            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
    return advice


def advise_model_tier(
    task_text: str,
    worker_type: str,
    requested_model: str | None,
    requested_effort: str | None,
    *,
    settings: DecisionRouterSettings | None = None,
    path: str = "python",
    task: str = "",
    workspace: str = "",
    record: Callable[[DecisionRecord], None] | None = None,
) -> Advice | None:
    if worker_type != "codex":
        return None
    text = f"worker={worker_type} model={requested_model} effort={requested_effort}\n{task_text}"
    return _decide(
        text,
        kind="tier",
        options=TIER_OPTIONS,
        path=path,
        task=task,
        settings=settings,
        record=record,
        workspace=workspace,
    )


def advise_failure(
    task_dir: str | Path,
    *,
    settings: DecisionRouterSettings | None = None,
    failure_context: str = "",
    task_text: str = "",
    stdout_log: Path | None = None,
    stderr_log: Path | None = None,
    path: str = "watchd",
    record: Callable[[DecisionRecord], None] | None = None,
) -> Advice | None:
    try:
        task = Path(task_dir)
        # Built-in workers may have no output-folder markers: use their real
        # exit/log evidence and original assignment instead of an empty request.
        failure = _tail(task / "FAILED", 1000) or failure_context[:1000]
        summary = _tail(task / "task.md", 1800) or task_text[:1800]
        logs = ""
        for log in (stderr_log, stdout_log):
            if log is not None:
                logs += _tail(log, 500, 5) + "\n"
        text = (
            f"FAILED: {failure}\nlogs: {logs[:1000]}\n"
            f"STATUS: {_tail(task / 'STATUS', 1000, 5)}\n"
            f"task: {summary}"
        )
        return _decide(
            text,
            kind="failure",
            options=FAILURE_OPTIONS,
            path=path,
            task=str(task),
            settings=settings,
            record=record,
        )
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("tier", "failure"))
    parser.add_argument("--task", required=True)
    parser.add_argument("--worker", default="codex")
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", default="")
    args = parser.parse_args()
    if args.kind == "tier":
        advice = advise_model_tier(
            _tail(Path(args.task), 4000),
            args.worker,
            args.model,
            args.effort,
            path="th-launch",
            task=args.task,
        )
        if advice:
            print(
                f"[jev] 등급 권고: {advice.choice} ({advice.confidence:.2f}) — 현재 {args.model} {args.effort}"
            )
    else:
        advice = advise_failure(args.task)
        if advice:
            print(advice.line())
    return 0


if __name__ == "__main__":
    main()
