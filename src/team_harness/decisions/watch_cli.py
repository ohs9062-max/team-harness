from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys

from team_harness.decisions.models import DecisionRequest
from team_harness.decisions.models import DecisionRouterSettings
from team_harness.decisions.router import DecisionRouter


def load_secrets() -> None:
    secrets_path = Path.home() / ".team-harness" / "secrets.env"
    if secrets_path.is_file():
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass


class MockWatchBackend:
    def __init__(self, choice: str | None = None) -> None:
        self.choice = choice

    async def decide(self, request: DecisionRequest):
        from team_harness.decisions.jev_backend import JevAnswer

        choice = self.choice or next(iter(request.options))
        return JevAnswer(
            choice=choice,
            confidence=0.95,
            probabilities={k: (0.95 if k == choice else 0.05) for k in request.options},
            usage={"input_tokens": 50, "output_tokens": 10},
            latency_ms=10,
        )


async def main() -> int:
    if len(sys.argv) < 3:
        return 0

    task_dir_str = sys.argv[1]
    event_type = sys.argv[2]
    watch_dir_str = (
        sys.argv[3]
        if len(sys.argv) >= 4
        else os.environ.get("TH_WATCH_DIR", os.path.expanduser("~/.team-harness/watch"))
    )

    task_dir = Path(task_dir_str).resolve()
    watch_dir = Path(watch_dir_str).resolve()

    if not task_dir.is_dir():
        return 0

    # 대상 사건 및 선택지 판정
    # - 완료 + 검수통과: {ACCEPT, SUSPECT, NOTIFY}
    # - 멈춤: {WAIT_MORE, KILL, NOTIFY}
    # - 쿼터: {LOCK_AND_WAIT, NOTIFY}
    # - 그 외: 호출 안 함
    verified_file = task_dir / "VERIFIED"
    is_verified_done = event_type == "완료" and verified_file.is_file()
    is_stalled = event_type == "멈춤"
    is_quota = (
        "쿼터" in event_type or "quota" in event_type.lower() or "용량" in event_type
    )

    if not (is_verified_done or is_stalled or is_quota):
        return 0

    if is_verified_done:
        options = {
            "ACCEPT": "The task completed and verification passed cleanly.",
            "SUSPECT": "The task completed but verification warnings or doubts exist.",
            "NOTIFY": "Notify coordinator without automatic approval.",
        }
        instructions = "Assess completed task with passed verification."
    elif is_stalled:
        options = {
            "WAIT_MORE": "Worker is still making progress; continue waiting.",
            "KILL": "Worker is hung or stalled; terminate it.",
            "NOTIFY": "Notify coordinator to decide manual action.",
        }
        instructions = "Assess stalled worker and recommend action."
    else:  # is_quota
        options = {
            "LOCK_AND_WAIT": "Lock worker family and wait for quota reset.",
            "NOTIFY": "Notify coordinator immediately about quota exhaustion.",
        }
        instructions = "Assess quota exhaustion event."

    fallback = "NOTIFY"

    # State 수집: 사건, STATUS 끝 5줄, WORKLOG 20줄, 검수 결과만
    status_file = task_dir / "STATUS"
    worklog_file = task_dir / "WORKLOG.md"
    failed_file = task_dir / "VERIFY_FAILED"

    status_tail: list[str] = []
    if status_file.is_file():
        try:
            status_lines = [
                line.strip()
                for line in status_file.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if line.strip()
            ]
            status_tail = status_lines[-5:]
        except Exception:
            pass

    worklog_tail: list[str] = []
    if worklog_file.is_file():
        try:
            worklog_lines = [
                line.strip()
                for line in worklog_file.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if line.strip()
            ]
            worklog_tail = worklog_lines[-20:]
        except Exception:
            pass

    verify_result = "없음"
    if verified_file.is_file():
        try:
            verify_result = verified_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()[:500]
        except Exception:
            verify_result = "통과"
    elif failed_file.is_file():
        try:
            verify_result = failed_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()[:500]
        except Exception:
            verify_result = "실패"

    state = {
        "event": event_type,
        "status_tail": status_tail,
        "worklog_tail": worklog_tail,
        "verification": verify_result,
    }

    # DecisionRouter 준비
    load_secrets()
    api_key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY") or ""
    settings = DecisionRouterSettings(enabled=True)

    backend = None
    if (
        api_key.startswith("fake")
        or os.environ.get("TH_TEST_MOCK_JEV") == "1"
        or "fake" in api_key
    ):
        backend = MockWatchBackend()

    router = DecisionRouter(settings, backend=backend)

    request = DecisionRequest(
        kind="watch_observation",
        state=state,
        options=options,
        fallback=fallback,
        instructions=instructions,
    )

    try:
        decision = await router.decide(request)
    except Exception as exc:
        decision = router._fallback(
            request=request, reason=f"Exception: {exc}", input_chars=0
        )

    # jev_decisions.log 기록
    watch_dir.mkdir(parents=True, exist_ok=True)
    log_file = watch_dir / "jev_decisions.log"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conf_str = (
        f"{decision.confidence:.2f}" if decision.confidence is not None else "None"
    )
    reason_str = decision.fallback_reason or "-"
    line = (
        f"{now_str} | {task_dir} | {event_type} | "
        f"choice={decision.choice} confidence={conf_str} applied={decision.applied} reason={reason_str}\n"
    )

    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        sys.exit(0)
