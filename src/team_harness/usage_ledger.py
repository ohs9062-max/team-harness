"""Per-attempt, file-backed usage ledger for team-harness.

This intentionally has no third-party dependencies: installed ``th-usage`` can
run it with the system Python as well as the project's virtual environment.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import tzinfo
from datetime import UTC
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any
from typing import Iterable
import unicodedata
from zoneinfo import ZoneInfo

# Load this standard-library sibling by path so ``python3 usage_ledger.py``
# never imports the package's optional runtime dependencies.
_digest_spec = importlib.util.spec_from_file_location(
    "usage_ledger_worker_digest", Path(__file__).with_name("worker_digest.py")
)
if _digest_spec is None or _digest_spec.loader is None:  # pragma: no cover
    raise RuntimeError("worker_digest.py를 불러올 수 없습니다")
_digest = importlib.util.module_from_spec(_digest_spec)
_digest_spec.loader.exec_module(_digest)
terminal_status = _digest.terminal_status


JOURNAL_RE = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \| 지시 \| "
    r"(?P<task>.+?) \| worker=(?P<worker>[^ ]+) model=(?P<model>.+?) "
    r"task\.md=(?P<task_md>.+?) \| (?P<owner>[^|]+?)(?: \| (?P<override>.*))?$"
)
STATUS_TIME_RE = re.compile(
    r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)"
)
TOKEN_RE = re.compile(r"tokens used\s*\n\s*([\d,]+)", re.IGNORECASE)
LOCAL_JOURNAL_TZ = ZoneInfo("Asia/Seoul")


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(
    value: str | None, *, naive_tz: tzinfo = LOCAL_JOURNAL_TZ
) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or naive_tz).astimezone(UTC)


def format_time(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z") if value else None


def _main_checkout(candidate: Path) -> Path | None:
    """Resolve a path to its main checkout, sharing project_owner's rule."""

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(candidate),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip())
    if not common.name:
        return None
    return common.parent if common.name == ".git" else None


def project_name(owner: str | None, task: Path) -> str:
    """Identify the git project without mistaking a task leaf for one."""

    candidates: list[Path] = []
    for raw in (
        read_text(task / ".th_owner").strip(),
        read_text(task / ".th_worktree").strip(),
        owner,
        str(task),
    ):
        if raw:
            candidate = Path(raw).expanduser()
            if candidate not in candidates:
                candidates.append(candidate)
    for candidate in candidates:
        project = _main_checkout(candidate)
        if project is not None and project.name:
            return project.name
    return "(unknown)"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def status_times(task: Path) -> tuple[datetime | None, datetime | None, str]:
    values = []
    for line in read_text(task / "STATUS").splitlines():
        match = STATUS_TIME_RE.match(line.strip())
        if match and (value := parse_time(match.group(1), naive_tz=LOCAL_JOURNAL_TZ)):
            values.append(value)
    if values:
        return values[0], values[-1], "STATUS"
    start = mtime(task / "task.md")
    end = (
        mtime(task / "DONE") or mtime(task / "FAILED") or mtime(task / "VERIFY_FAILED")
    )
    return start, end, "mtime"


def task_result(task: Path) -> str:
    if (task / "VERIFY_FAILED").is_file():
        return "verify_failed"
    if (task / "VERIFIED").is_file():
        return "verified"
    state = terminal_status(task)
    if state == "DONE":
        return "done"
    if state == "FAILED":
        return "failed"
    if (task / "worker.pid").is_file() or (task / "job.pid").is_file():
        return "running"
    return "unknown"


def codex_tokens(task: Path, attempt: int) -> int | None:
    log = (
        task / "codex.log"
        if attempt == 1
        else task / f"prev_attempt{attempt - 1}" / "codex.log"
    )
    found = TOKEN_RE.search(read_text(log))
    return int(found.group(1).replace(",", "")) if found else None


@dataclass
class Attempt:
    project: str
    task: str
    attempt: int
    worker: str | None
    model: str | None
    effort: str | None
    started: str | None
    ended: str | None
    wall_min: float | None
    tokens: int | None
    result: str
    source: str
    owner: str | None = None
    override: str | None = None

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("owner")
        value.pop("override")
        return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    output = []
    for line in read_text(path).splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("v") == 1:
            output.append(record)
    return output


def worktree_roots(workspace: Path) -> set[Path]:
    """Return worktrees registered with the current repository, if available."""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "worktree", "list", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return set()
    return {
        Path(line.removeprefix("worktree ")).expanduser().resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    }


def orca_workspace_roots(home: Path) -> set[Path]:
    return {
        path.resolve()
        for path in (home / "orca" / "workspaces").glob("*/*")
        if path.is_dir()
    }


def journal_paths(workspace: Path, home: Path | None = None) -> list[Path]:
    roots = {workspace.resolve(), *worktree_roots(workspace)}
    roots.update(orca_workspace_roots(home or Path.home()))
    return sorted(
        {
            journal.resolve()
            for root in roots
            for journal in (root / ".claude").glob("work_log*.md")
        }
    )


def journal_items(workspace: Path) -> list[dict[str, str | None]]:
    """Read instruction records once, resolving relative task paths per journal."""
    records = []
    seen = set()
    for journal in journal_paths(workspace):
        journal_workspace = journal.parent.parent
        for line in read_text(journal).splitlines():
            found = JOURNAL_RE.match(line)
            if not found:
                continue
            item = found.groupdict()
            task = Path(str(item["task"])).expanduser()
            item["task"] = str(
                (task if task.is_absolute() else journal_workspace / task).resolve()
            )
            stamp = parse_time(item["at"], naive_tz=LOCAL_JOURNAL_TZ)
            key = (item["task"], format_time(stamp) if stamp else item["at"])
            if key not in seen:
                seen.add(key)
                records.append(item)
    return records


def task_dirs(watch_dir: Path, workspace: Path) -> set[Path]:
    output = {
        Path(line).expanduser().resolve()
        for line in read_text(watch_dir / "tasks.txt").splitlines()
        if line.strip()
    }
    # Journals preserve tasks removed from the current registry.
    output.update(
        Path(task) for item in journal_items(workspace) if (task := item.get("task"))
    )
    return output


def journal_records(
    task_paths: Iterable[Path], workspace: Path
) -> list[dict[str, Any]]:
    del (
        task_paths
    )  # Discovery is journal-based so deleted registry entries remain visible.
    return journal_items(workspace)


def build_attempts(watch_dir: Path, workspace: Path) -> list[Attempt]:
    tasks = task_dirs(watch_dir, workspace)
    launches = load_jsonl(watch_dir / "usage.jsonl")
    journals = journal_records(tasks, workspace)
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in launches:
        if record.get("task"):
            item = dict(record)
            item["task"] = str(Path(str(item["task"])).expanduser().resolve())
            by_task[item["task"]].append(item)
            tasks.add(Path(item["task"]))
    # Historical entries fill gaps only; matching task/worker/model is one attempt.
    for record in journals:
        existing = by_task[record["task"]]
        journal_at = parse_time(record["at"], naive_tz=LOCAL_JOURNAL_TZ)
        if any(
            e.get("worker") == record["worker"]
            and e.get("model") == record["model"]
            and parse_time(str(e.get("at", "")), naive_tz=LOCAL_JOURNAL_TZ)
            and journal_at
            and parse_time(str(e.get("at", "")), naive_tz=LOCAL_JOURNAL_TZ).replace(
                second=0, microsecond=0
            )
            == journal_at.replace(second=0, microsecond=0)
            for e in existing
        ):
            continue
        existing.append({**record, "source": "journal"})
        tasks.add(Path(record["task"]))

    output = []
    now = datetime.now(UTC)
    for task_s, records in by_task.items():
        task = Path(task_s)
        records.sort(key=lambda value: str(value.get("at", "")))
        first, last, time_source = status_times(task)
        for index, record in enumerate(records, 1):
            attempt = int(record.get("attempt") or index)
            started = (
                parse_time(str(record.get("at", "")), naive_tz=LOCAL_JOURNAL_TZ)
                or first
            )
            # Only the current attempt has an observable final status; archived attempts are interrupted unless logs say otherwise.
            current = attempt == max(
                int(x.get("attempt") or pos + 1) for pos, x in enumerate(records)
            )
            ended = (
                last
                if current
                else parse_time(
                    str(records[index].get("at", "")), naive_tz=LOCAL_JOURNAL_TZ
                )
                if index < len(records)
                else None
            )
            result = task_result(task) if current else "interrupted"
            invalid_duration = bool(
                ended and (ended > now or (started is not None and ended < started))
            )
            wall = (
                round((ended - started).total_seconds() / 60, 1)
                if started and ended and not invalid_duration
                else None
            )
            output.append(
                Attempt(
                    project=project_name(
                        str(
                            record.get("owner") or read_text(task / ".th_owner").strip()
                        ),
                        task,
                    ),
                    task=str(task),
                    attempt=attempt,
                    worker=record.get("worker"),
                    model=record.get("model"),
                    effort=record.get("effort"),
                    started=format_time(started),
                    ended=format_time(ended),
                    wall_min=wall,
                    tokens=codex_tokens(task, attempt)
                    if record.get("worker") == "codex"
                    else None,
                    result=result,
                    source=(
                        f"{record.get('source', 'usage')}+{time_source}"
                        + (
                            "+duration_invalid(ended_before_started)"
                            if invalid_duration
                            else ""
                        )
                    ),
                    owner=record.get("owner"),
                    override=record.get("override"),
                )
            )
    # Registrations without any launch history still deserve one visible unknown row.
    known = {entry.task for entry in output}
    for task in sorted(tasks):
        if str(task) in known:
            continue
        start, end, source = status_times(task)
        invalid_duration = bool(
            end and (end > now or (start is not None and end < start))
        )
        output.append(
            Attempt(
                project_name(read_text(task / ".th_owner").strip(), task),
                str(task),
                1,
                None,
                None,
                None,
                format_time(start),
                format_time(end),
                round((end - start).total_seconds() / 60, 1)
                if start and end and not invalid_duration
                else None,
                None,
                task_result(task),
                f"registry+{source}"
                + (
                    "+duration_invalid(ended_before_started)"
                    if invalid_duration
                    else ""
                ),
            )
        )
    return sorted(
        output, key=lambda row: (row.started or "", row.task, row.attempt), reverse=True
    )


def claude_usage(home: Path, since: datetime | None) -> list[dict[str, Any]]:
    root = home / ".claude" / "projects"
    items = []
    for path in root.glob("*/*.jsonl"):
        calls = input_tokens = cache_read = cache_create = output_tokens = 0
        stamps = []
        for line in read_text(path).splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "assistant" or not isinstance(
                row.get("message"), dict
            ):
                continue
            stamp = parse_time(row.get("timestamp"))
            if since and (not stamp or stamp < since):
                continue
            usage = row["message"].get("usage")
            if not isinstance(usage, dict):
                continue
            calls += 1
            stamps.append(stamp)
            input_tokens += int(usage.get("input_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            cache_create += int(usage.get("cache_creation_input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
        if calls:
            items.append(
                {
                    "project_dir": path.parent.name,
                    "session": path.name,
                    "started": format_time(min(s for s in stamps if s)),
                    "ended": format_time(max(s for s in stamps if s)),
                    "calls": calls,
                    "input_tokens": input_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_create,
                    "output_tokens": output_tokens,
                    "avg_per_call": round(
                        (input_tokens + cache_read + cache_create + output_tokens)
                        / calls,
                        1,
                    ),
                }
            )
    return items


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def clip(text: Any, size: int) -> str:
    value = "-" if text is None else str(text)
    result = ""
    for char in value:
        if width(result + char) > size - (1 if width(value) > size else 0):
            break
        result += char
    return result + ("…" if width(value) > size else "")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    term = shutil.get_terminal_size((120, 24)).columns
    sizes = [
        max(width(h), *(width(str(c)) for c in col))
        for h, col in zip(
            headers,
            zip(*rows, strict=True) if rows else [[]] * len(headers),
            strict=True,
        )
    ]
    total = sum(sizes) + 3 * (len(headers) - 1)
    while total > term and max(sizes) > 8:
        i = sizes.index(max(sizes))
        sizes[i] -= 1
        total -= 1

    def render(cells: list[Any]) -> str:
        return " | ".join(
            clip(value, size).ljust(size)
            for value, size in zip(cells, sizes, strict=True)
        )

    return "\n".join(
        [
            render(headers),
            "-+-".join("-" * s for s in sizes),
            *(render(row) for row in rows),
        ]
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="작업·모델별 사용량 장부를 표시합니다."
    )
    parser.add_argument("--since", help="YYYY-MM-DD 이후")
    parser.add_argument("--project", help="프로젝트 이름 일부")
    parser.add_argument("--tasks", action="store_true", help="시도별 전체 행")
    parser.add_argument("--claude", action="store_true", help="Claude 세션 사용량")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    parser.add_argument(
        "--watch-dir",
        default=os.environ.get(
            "TH_WATCH_DIR", str(Path.home() / ".team-harness/watch")
        ),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--home",
        default=os.environ.get("HOME", str(Path.home())),
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    since = (
        parse_time(args.since)
        if args.since
        else datetime.now(UTC) - timedelta(hours=24)
    )
    if args.since and since is None:
        raise SystemExit("--since은 YYYY-MM-DD 형식이어야 합니다")
    rows = [
        row
        for row in build_attempts(Path(args.watch_dir).expanduser(), Path.cwd())
        if not since or (parse_time(row.started) and parse_time(row.started) >= since)
    ]
    if args.project:
        rows = [row for row in rows if args.project.lower() in row.project.lower()]
    claude = claude_usage(Path(args.home).expanduser(), since) if args.claude else []
    if args.json:
        print(
            json.dumps(
                {
                    "since": format_time(since),
                    "attempts": [r.public() for r in rows],
                    "claude": claude,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.claude:
        print(
            table(
                [
                    "프로젝트 dir",
                    "기간",
                    "호출",
                    "입력",
                    "캐시 읽기",
                    "캐시 쓰기",
                    "출력",
                    "호출당 평균",
                ],
                [
                    [
                        r["project_dir"],
                        f"{r['started'] or '-'}~{r['ended'] or '-'}",
                        r["calls"],
                        r["input_tokens"],
                        r["cache_read_input_tokens"],
                        r["cache_creation_input_tokens"],
                        r["output_tokens"],
                        r["avg_per_call"],
                    ]
                    for r in claude
                ]
                or [["없음", "-", 0, 0, 0, 0, 0, "-"]],
            )
        )
    elif args.tasks:
        print(
            table(
                [
                    "project",
                    "task",
                    "시도",
                    "worker",
                    "model",
                    "시작",
                    "분",
                    "토큰",
                    "결과",
                    "출처",
                ],
                [
                    [
                        r.project,
                        Path(r.task).name,
                        r.attempt,
                        r.worker,
                        r.model,
                        r.started,
                        r.wall_min,
                        r.tokens,
                        r.result,
                        r.source,
                    ]
                    for r in rows
                ]
                or [["없음"] * 10],
            )
        )
    else:
        groups: dict[tuple[str, str | None], list[Attempt]] = defaultdict(list)
        for row in rows:
            groups[(row.project, row.model)].append(row)
        summary = []
        for (project, model), values in sorted(
            groups.items(), key=lambda item: (item[0][0], item[0][1] or "")
        ):
            known = [v.tokens for v in values if v.tokens is not None]
            walls = [v.wall_min for v in values if v.wall_min is not None]
            verified = [v for v in values if v.result in {"verified", "verify_failed"}]
            summary.append(
                [
                    project,
                    model,
                    len({v.task for v in values}),
                    len(values),
                    sum(v.attempt > 1 for v in values),
                    sum(known),
                    len(values) - len(known),
                    round(sum(walls) / len(walls), 1) if walls else "-",
                    f"{sum(v.result == 'verified' for v in verified)}/{len(verified)}"
                    if verified
                    else "-",
                ]
            )
        print(
            table(
                [
                    "프로젝트",
                    "모델",
                    "작업",
                    "시도",
                    "재시도",
                    "토큰 합",
                    "모름",
                    "평균 분",
                    "검수 통과",
                ],
                summary or [["(해당 없음)"] + [""] * 8],
            )
        )
        top = sorted(
            (r for r in rows if r.tokens is not None),
            key=lambda r: r.tokens or 0,
            reverse=True,
        )[:10]
        print("\n토큰 상위 10개 시도")
        print(
            table(
                ["project", "task", "시도", "model", "토큰", "결과"],
                [
                    [
                        r.project,
                        Path(r.task).name,
                        r.attempt,
                        r.model,
                        r.tokens,
                        r.result,
                    ]
                    for r in top
                ]
                or [["(해당 없음)"] + [""] * 5],
            )
        )
    print(
        "[th-usage] 사용자 확인용 화면 — 에이전트는 이 표를 다시 출력·요약하지 말 것(이상 있을 때만 한 줄)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
