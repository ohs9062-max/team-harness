"""Launch-time quota and resource policy checks for worker invocations."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import NamedTuple

from team_harness.worker_context import parse_task_metadata
from team_harness.worker_context import WorkerContextError

ALLOWED_KINDS = frozenset({"code", "research", "data", "docs"})
DEFAULT_CODEX_5H_BUDGET = 2_000_000
RECENT_WINDOW_SECONDS = 5 * 3600
_REVERSE_CHUNK_BYTES = 65536


class Violation(NamedTuple):
    code: str
    message: str


def _parse_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    pid = int(raw)
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
            if stat_text.rsplit(") ", 1)[-1].split()[0] == "Z":
                return False
        except (OSError, IndexError):
            pass
    return True


def _parallel_limit(worker_type: str, env: Mapping[str, str]) -> int | None:
    override_raw = env.get("TH_MAX_PARALLEL", "").strip()
    if override_raw:
        try:
            parsed = int(override_raw)
            if parsed >= 0:
                return parsed
        except ValueError:
            pass
    if worker_type == "codex":
        return _parse_int(env.get("TH_MAX_PARALLEL_CODEX"), 2)
    if worker_type == "agy":
        return _parse_int(env.get("TH_MAX_PARALLEL_AGY"), 3)
    env_key = f"TH_MAX_PARALLEL_{worker_type.upper()}"
    if env_key in env:
        return _parse_int(env.get(env_key), 2)
    return None


def _active_workers(task_dir: Path, worker_type: str, watch_dir: Path) -> list[str]:
    tasks_file = watch_dir / "tasks.txt"
    if not tasks_file.is_file():
        return []
    try:
        lines = tasks_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    target_resolved = task_dir.resolve()
    seen: set[Path] = set()
    active: list[str] = []
    for raw in lines:
        entry = raw.strip()
        if not entry:
            continue
        candidate = Path(entry)
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == target_resolved or resolved in seen:
            continue
        seen.add(resolved)
        pid = _read_pid(candidate / "worker.pid")
        if pid is None or not _pid_alive(pid):
            continue
        if not (candidate / f"{worker_type}.log").is_file():
            continue
        active.append(candidate.name)
    return active


def _read_task_kind(task_md: Path) -> str | None:
    try:
        metadata = parse_task_metadata(task_md)
        kind = metadata.get("kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip()
        return None
    except WorkerContextError:
        pass
    try:
        lines = task_md.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() == "kind":
            values["kind"] = value.strip().strip("\"'")
    else:
        return None
    raw_kind = values.get("kind", "").strip()
    return raw_kind or None


def _extract_billable_tokens(event: dict[str, object]) -> int | None:
    event_type = event.get("type")
    if event_type not in (None, "event_msg", "token_count"):
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    payload_type = payload.get("type")
    if payload_type not in (None, "token_count"):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    if not any(key in usage for key in keys):
        return None
    values: dict[str, int] = {}
    for key in keys:
        raw = usage.get(key, 0)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            return None
        values[key] = raw
    billable = (
        values["input_tokens"] - values["cached_input_tokens"] + values["output_tokens"]
    )
    return max(0, billable)


def _read_rollout_window_tokens(path: Path, cutoff: float) -> int:
    """Return token-counter growth within the rolling time window."""
    latest: int | None = None
    before_window: int | None = None
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            remainder = b""
            while position > 0:
                read_size = min(_REVERSE_CHUNK_BYTES, position)
                position -= read_size
                stream.seek(position)
                chunk = stream.read(read_size) + remainder
                lines = chunk.split(b"\n")
                if position > 0:
                    remainder = lines[0]
                    candidates = lines[1:]
                else:
                    remainder = b""
                    candidates = lines
                for raw in reversed(candidates):
                    stripped = raw.strip()
                    if not stripped or b"token_count" not in stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    tokens = _extract_billable_tokens(event)
                    timestamp = event.get("timestamp")
                    if tokens is None or not isinstance(timestamp, str):
                        continue
                    try:
                        event_time = datetime.fromisoformat(
                            timestamp.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        continue
                    if event_time >= cutoff:
                        if latest is None:
                            latest = tokens
                    else:
                        before_window = tokens
                        break
    except OSError:
        return 0
    if latest is None:
        return 0
    return max(0, latest - (before_window or 0))


def _sessions_dir_from_env(env: Mapping[str, str]) -> Path:
    explicit = env.get("TH_CODEX_SESSIONS_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = env.get("HOME", "").strip()
    if home:
        return Path(home).expanduser() / ".codex" / "sessions"
    return Path.home() / ".codex" / "sessions"


def sum_recent_codex_tokens(
    sessions_dir: str | Path, window_seconds: int = RECENT_WINDOW_SECONDS
) -> int:
    root = Path(sessions_dir)
    if not root.exists():
        return 0
    cutoff = time.time() - window_seconds
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            total += _read_rollout_window_tokens(path, cutoff)
    return total


def check(
    task_dir: str | Path,
    worker_type: str,
    model: str,
    effort: str,
    watch_dir: str | Path,
    env: Mapping[str, str] | None = None,
) -> list[Violation]:
    actual_env: Mapping[str, str] = os.environ if env is None else env
    tdir = Path(task_dir)
    wdir = Path(watch_dir)
    violations: list[Violation] = []

    limit = _parallel_limit(worker_type, actual_env)
    if limit is not None:
        active = _active_workers(tdir, worker_type, wdir)
        if len(active) >= limit:
            joined = ", ".join(active)
            violations.append(
                Violation(
                    "parallel",
                    f"{worker_type} 동시 실행 {limit}개 상한: 이미 {len(active)}개 실행 중({joined}). "
                    f"끝난 뒤 띄우거나 TH_MAX_PARALLEL={len(active) + 1}",
                )
            )

    if worker_type == "codex":
        effort_norm = effort.strip().lower()
        if effort_norm in {"high", "xhigh"} or "sol" in model.lower():
            high_reason = actual_env.get("TH_HIGH_REASON", "").strip()
            if not high_reason:
                violations.append(
                    Violation(
                        "high",
                        f"codex 고비용 설정({model} {effort}) 거부: 기본은 gpt-5.6-terra medium. "
                        '꼭 필요하면 TH_HIGH_REASON="<이유>"',
                    )
                )

        kind = _read_task_kind(tdir / "task.md")
        if kind != "code":
            codex_reason = actual_env.get("TH_CODEX_REASON", "").strip()
            if not codex_reason:
                if kind is None:
                    msg = (
                        "codex 실행 거부: task.md front matter에 kind: code가 없음"
                        "(허용 값: code, research, data, docs). "
                        '코드 작업이면 kind: code를 추가하거나 TH_CODEX_REASON="<이유>"'
                    )
                elif kind not in ALLOWED_KINDS:
                    msg = (
                        f"codex 실행 거부: task.md kind: {kind}는 허용 값(code, research, data, docs)이 아님"
                        '(codex는 kind: code만 허용). 꼭 필요하면 TH_CODEX_REASON="<이유>"'
                    )
                else:
                    msg = (
                        f"codex 실행 거부: task.md kind: {kind}는 비코드 작업임"
                        "(codex는 kind: code만 허용, 비코드는 agy 사용). "
                        '꼭 필요하면 TH_CODEX_REASON="<이유>"'
                    )
                violations.append(Violation("kind", msg))

        ignore_budget = actual_env.get("TH_IGNORE_BUDGET", "").strip() == "1"
        if not ignore_budget:
            budget = _parse_int(
                actual_env.get("TH_CODEX_5H_BUDGET"), DEFAULT_CODEX_5H_BUDGET
            )
            sessions_dir = _sessions_dir_from_env(actual_env)
            total_tokens = sum_recent_codex_tokens(sessions_dir)
            if budget > 0 and total_tokens * 10 >= budget * 9:
                pct = (total_tokens * 100) // budget
                violations.append(
                    Violation(
                        "budget",
                        "codex 최근 5시간 토큰 사용량(캐시 제외, codex 'tokens used' 기준) "
                        f"90% 이상({total_tokens:,}/{budget:,}, {pct}%) 거부: "
                        "잠시 기다리거나 agy를 쓰거나 TH_IGNORE_BUDGET=1",
                    )
                )

    return violations


def check_warnings(worker_type: str, env: Mapping[str, str] | None = None) -> list[str]:
    actual_env: Mapping[str, str] = os.environ if env is None else env
    if worker_type != "codex":
        return []
    if actual_env.get("TH_IGNORE_BUDGET", "").strip() == "1":
        return []
    budget = _parse_int(actual_env.get("TH_CODEX_5H_BUDGET"), DEFAULT_CODEX_5H_BUDGET)
    if budget <= 0:
        return []
    sessions_dir = _sessions_dir_from_env(actual_env)
    total_tokens = sum_recent_codex_tokens(sessions_dir)
    if budget * 7 <= total_tokens * 10 < budget * 9:
        pct = (total_tokens * 100) // budget
        return [
            "경고: codex 최근 5시간 토큰 사용량(캐시 제외, codex 'tokens used' 기준) "
            f"70% 이상({total_tokens:,}/{budget:,}, {pct}%). "
            "쿼터 소진에 주의하세요"
        ]
    return []


def collect_overrides(env: Mapping[str, str] | None = None) -> list[str]:
    actual_env: Mapping[str, str] = os.environ if env is None else env
    overrides: list[str] = []
    max_parallel = actual_env.get("TH_MAX_PARALLEL", "").strip()
    if max_parallel:
        overrides.append(f"TH_MAX_PARALLEL={max_parallel}")
    high_reason = actual_env.get("TH_HIGH_REASON", "").strip()
    if high_reason:
        overrides.append(f"TH_HIGH_REASON={high_reason}")
    codex_reason = actual_env.get("TH_CODEX_REASON", "").strip()
    if codex_reason:
        overrides.append(f"TH_CODEX_REASON={codex_reason}")
    if actual_env.get("TH_IGNORE_BUDGET", "").strip() == "1":
        overrides.append("TH_IGNORE_BUDGET=1")
    return overrides


def main(
    argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 6 or args[0] != "check":
        print(
            "사용: python -m team_harness.launch_policy check <task_dir> <worker_type> <model> <effort> <watch_dir>",
            file=sys.stderr,
        )
        return 2
    _, task_dir, worker_type, model, effort, watch_dir = args
    actual_env: Mapping[str, str] = os.environ if env is None else env
    violations = check(task_dir, worker_type, model, effort, watch_dir, actual_env)
    if violations:
        for violation in violations:
            print(violation.message, file=sys.stderr)
        return 4
    for warning in check_warnings(worker_type, actual_env):
        print(warning, file=sys.stderr)
    for override in collect_overrides(actual_env):
        print(f"override: {override}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
