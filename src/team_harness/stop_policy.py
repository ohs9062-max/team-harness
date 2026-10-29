"""Classify terminal worker markers for automatic continuation."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import fcntl
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Literal

from team_harness.worker_digest import quota_lock_active
from team_harness.worker_relaunch import launch_arguments
from team_harness.worker_relaunch import relaunch_worker

StopClassification = Literal["ok", "self_stop", "allowed_fail"]

_REMAINING = re.compile(
    r"남음|남은|remaining|incomplete|partial|미구현|not implemented|일부만",
    re.IGNORECASE,
)
_ALLOWED = re.compile(
    r"quota|쿼터|rate[ -]?limit|속도 제한|\b429\b|too many requests|"
    r"resource[_ -]exhausted|capacity|용량|"
    r"(?:permission|권한|authentication|인증|license|라이선스)"
    r".{0,30}(?:denied|failed|failure|error|거부|실패|오류|없음|만료)|"
    r"time[ -]?limit|timeout|시간 제한|시간 초과|"
    r"(?:같은|동일|same).{0,30}(?:수정|fix).{0,30}(?:2회|두 번|twice|two times).{0,30}(?:실패|fail)|"
    r"(?:설정|setup).{0,30}(?:3회|세 번|three times).{0,30}(?:실패|fail)|"
    r"(?:다른|서로 다른|different).{0,30}(?:방식|방법|approach).{0,30}(?:2회|두 번|twice|two times).{0,30}(?:거부|deni)",
    re.IGNORECASE | re.DOTALL,
)


def _allowed_failure(reason: str) -> bool:
    if _ALLOWED.search(reason):
        return True
    twice = re.search(
        r"2\s*(?:회|차례|번)|두\s*(?:번|차례)|twice|two times", reason, re.IGNORECASE
    )
    failure = re.search(r"실패|fail", reason, re.IGNORECASE)
    if twice and failure and re.search(r"같은|동일|same", reason, re.IGNORECASE):
        return bool(re.search(r"수정|fix", reason, re.IGNORECASE))
    three = re.search(
        r"3\s*(?:회|차례|번)|세\s*(?:번|차례)|three times", reason, re.IGNORECASE
    )
    if three and failure and re.search(r"설정|setup", reason, re.IGNORECASE):
        return True
    denial = re.search(r"거부|deni", reason, re.IGNORECASE)
    return bool(
        twice
        and denial
        and re.search(r"다른|different", reason, re.IGNORECASE)
        and re.search(r"방식|방법|approach", reason, re.IGNORECASE)
    )


def classify_stop_text(text: str) -> StopClassification:
    """Classify an explicit terminal result card when no marker directory exists."""
    match = re.search(
        r"(?im)^\s*(?:\*\*)?(FAILED|DONE)(?:\*\*)?\s*[:：-]?\s*(.*)$", text
    )
    if match is None:
        return "ok"
    reason = text[match.start(2) :]
    if match.group(1).upper() == "FAILED":
        return "allowed_fail" if _allowed_failure(reason) else "self_stop"
    return "self_stop" if _REMAINING.search(reason) else "ok"


def continuation_prompt(prompt: str, reason: str, count: int) -> str:
    """Append the previous result without discarding the original assignment."""
    return (
        f"{prompt}\n\n## 자동 계속 ({count}차)\n\n직전 사유:\n{reason}\n\n"
        "남은 항목을 이어서 끝까지 하라, 이미 된 것은 다시 하지 마라."
    )


def classify_stop(task_dir: Path) -> StopClassification:
    """Classify the current terminal marker; uncertainty favors continuation."""
    failed = task_dir / "FAILED"
    done = task_dir / "DONE"
    if failed.is_file():
        reason = failed.read_text(encoding="utf-8", errors="replace")
        return "allowed_fail" if _allowed_failure(reason) else "self_stop"
    if done.is_file():
        if (task_dir / "VERIFY_FAILED").is_file():
            return "self_stop"
        body = done.read_text(encoding="utf-8", errors="replace")
        return "self_stop" if _REMAINING.search(body) else "ok"
    return "ok"


# TH-D44 was deployed at 10:33 Asia/Seoul on 2026-10-29.
_DEPLOYED = datetime(
    2026, 10, 29, 10, 33, tzinfo=timezone(timedelta(hours=9))
).timestamp()


def _last_launch_time(task: Path, watch_dir: Path) -> float | None:
    """Find the latest launch, rejecting malformed matching evidence."""
    times: list[float] = []
    ledger = watch_dir / "usage.jsonl"
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    return None
                if Path(row["task"]).resolve() != task.resolve():
                    continue
                at = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
                if at.tzinfo is None:
                    return None
                times.append(at.timestamp())
            except (ValueError, KeyError, TypeError, AttributeError):
                return None
    status = task / "STATUS"
    if status.exists():
        for line in status.read_text(encoding="utf-8").splitlines():
            if " LAUNCH " in line:
                try:
                    times.append(
                        datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                    )
                except ValueError:
                    return None
    return max(times) if times else None


def _eligible_signature(task: Path, watch_dir: Path) -> str | None:
    marker = task / ("FAILED" if (task / "FAILED").is_file() else "DONE")
    stat = marker.stat()
    now = time.time()
    launched = _last_launch_time(task, watch_dir)
    if (
        launched is None
        or launched <= _DEPLOYED
        or not (0 <= now - stat.st_mtime <= 30 * 60)
        or stat.st_mtime <= launched
    ):
        return None
    signature = f"{marker.name} {stat.st_mtime_ns} {stat.st_size}"
    seen = task / ".auto_continue_signature"
    if seen.exists() and seen.read_text(encoding="utf-8").strip() == signature:
        return None
    return signature


def continue_stopped_task(task: Path, watch_dir: Path, owner: Path) -> str:
    """Continue a stopped th-launch task, returning an event description if needed."""
    lock_path = task / ".auto_continue.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (task / "KILLED").exists() or classify_stop(task) != "self_stop":
            (task / ".th_daemon.settled").touch()
            return ""
        signature = _eligible_signature(task, watch_dir)
        if signature is None:
            return ""
        task_file = task / "task.md"
        try:
            text = task_file.read_text(encoding="utf-8")
        except OSError as exc:
            return f"자동 계속 실패: {exc}"
        if (
            (task / "FAILED").is_file()
            and not (task / "verify.sh").is_file()
            and re.search(r"(?m)^\s*kind:\s*['\"]?research['\"]?\s*(?:#.*)?$", text)
        ):
            return ""
        try:
            args = launch_arguments(task, watch_dir)
        except (FileNotFoundError, ValueError):
            return ""
        if quota_lock_active(watch_dir / f"quota_lock.{args[1]}"):
            return ""
        count_path = task / ".auto_continue_count"
        count = int(count_path.read_text().strip()) if count_path.exists() else 0
        if count >= 2:
            return "자동 계속 소진 — 작업 분할 필요"
        reason_path = task / ("FAILED" if (task / "FAILED").is_file() else "DONE")
        reason = reason_path.read_text(encoding="utf-8", errors="replace")
        section = f"\n\n## 자동 계속 ({count + 1}차)\n\n직전 사유:\n{reason}\n"
        section += "\n남은 항목을 이어서 끝까지 하라, 이미 된 것은 다시 하지 마라.\n"
        if (task / "VERIFY_FAILED").exists():
            result = task / ".th_verify_output.txt"
            if not result.exists():
                result = task / "VERIFY_FAILED"
            if result.exists():
                tail = "\n".join(
                    result.read_text(encoding="utf-8", errors="replace").splitlines()[
                        -20:
                    ]
                )
                section += f"\n검수 출력 마지막 20줄:\n{tail}\n"
        (task / ".auto_continue_signature").write_text(
            signature + "\n", encoding="utf-8"
        )
        with task_file.open("a", encoding="utf-8") as stream:
            stream.write(section)
        count_path.write_text(str(count + 1), encoding="utf-8")
        try:
            relaunch_worker(task, args, owner, auto_continue=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            return f"자동 계속 실패: {exc}"
        event_reason = " ".join(reason.splitlines()).strip()
        return f"자동 계속({count + 1}/2): {event_reason}"


def main() -> None:
    import sys

    task, watch_dir, owner = (Path(arg) for arg in sys.argv[1:])
    try:
        print(continue_stopped_task(task, watch_dir, owner))
    except (OSError, ValueError) as exc:
        print(f"자동 계속 실패: {exc}")


if __name__ == "__main__":
    main()
