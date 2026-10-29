"""Daily loss metrics from launch records and watch events."""

from __future__ import annotations

from collections import Counter
from datetime import date
from datetime import datetime
from datetime import timedelta
import fcntl
import json
from pathlib import Path
import re
import sys
from typing import Any

from team_harness.workspace_archive import watch_dir

CLASSIFICATION = (
    ("스스로 멈춤", r"INCOMPLETE|미완료|남은"),
    ("가드 거부", r"PreToolUse|가드|guard|영역"),
    ("쿼터", r"quota|rate.limit|쿼터"),
    ("인계 없음", r"인계 없음|워커 중단|handoff|aborted"),
    ("멈춤", r"멈춤|stall"),
)


def classify(text: str) -> str:
    for name, pattern in CLASSIFICATION:
        if re.search(pattern, text, re.I):
            if name == "스스로 멈춤" and re.search(
                r"PreToolUse|가드|guard|영역", text, re.I
            ):
                return "스스로 멈춤 (가드 거부 후)"
            return name
    return "기타"


def owners(wd: Path, owner: Path | None = None) -> set[Path]:
    result = {owner.resolve()} if owner else set()
    registry = wd / "tasks.txt"
    if registry.exists():
        for line in registry.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = (
                    (Path(line) / ".th_owner")
                    .read_text(encoding="utf-8", errors="replace")
                    .strip()
                )
                if value:
                    result.add(Path(value).resolve())
            except OSError:
                continue
    return result


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _tokens(task: Path) -> int:
    lines = _lines(task / "codex.log")
    return sum(
        int(m.group().replace(",", ""))
        for i, line in enumerate(lines[:-1])
        if line.strip().lower() == "tokens used"
        if (m := re.search(r"[\d,]+", lines[i + 1]))
    )


def collect(
    wd: Path, roots: set[Path], start: date, end: date, *, details: bool = False
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    launches: dict[str, list[datetime]] = {}
    tasks: dict[str, tuple[str, str]] = {}
    rollovers: set[str] = set()

    def row(day: date, root: Path) -> dict[str, Any]:
        key = (day.isoformat(), str(root))
        return rows.setdefault(
            key,
            {
                "date": key[0],
                "project": key[1],
                "launch": 0,
                "retry": 0,
                "rollover": 0,
                "auto_continue": 0,
                "tokens": 0,
                "causes": {},
                "completion_seconds": {},
                "task_tokens": {},
            },
        )

    invalid_json = 0
    for line in _lines(wd / "usage.jsonl"):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            invalid_json += 1
            continue
        try:
            root = Path(entry["owner"]).resolve()
            if root not in roots:
                continue
            stamp = datetime.fromisoformat(
                entry["at"].replace("Z", "+00:00")
            ).astimezone()
            task = entry["task"]
            launches.setdefault(task, []).append(stamp)
            tasks[task] = (str(root), Path(task).name)
            if entry.get("rollover"):
                rollovers.add(task)
            if not start <= stamp.date() <= end:
                continue
            item = row(stamp.date(), root)
            item["launch"] += 1
            item["retry"] += int(entry.get("attempt", 1) > 1)
            item["rollover"] += int(bool(entry.get("rollover")))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    if invalid_json:
        print(f"건너뛴 잘못된 JSON 줄: {invalid_json}", file=sys.stderr)
    events: list[tuple[datetime, Path, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for root in roots:
        for line in _lines(root / ".claude" / "inbox.md"):
            parts = line.split(" | ")
            if len(parts) < 4:
                continue
            try:
                stamp = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").astimezone()
            except ValueError:
                continue
            matches = [
                p
                for p, (r, name) in tasks.items()
                if r == str(root)
                and (p.endswith(parts[1]) or name == Path(parts[1]).name)
            ]
            task = matches[0] if len(matches) == 1 else parts[1]
            events.append((stamp, root, task, parts[2], " | ".join(parts[2:])))
            seen.add((str(root), task, stamp.strftime("%Y-%m-%d %H:%M"), parts[2]))
    for line in _lines(wd / "events.log"):
        match = re.match(
            r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[([^]]+)\] (.*?): (.*)", line
        )
        if not match:
            continue
        try:
            stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").astimezone()
        except ValueError:
            continue
        matches = [
            p
            for p, (_, name) in tasks.items()
            if p.endswith(match[3]) or name == Path(match[3]).name
        ]
        if len(matches) != 1:
            continue
        task = matches[0]
        root = Path(tasks[task][0])
        key = (str(root), task, stamp.strftime("%Y-%m-%d %H:%M"), match[2])
        if key not in seen:
            events.append((stamp, root, task, match[2], match[4]))
    completed: set[str] = set()
    statuses: dict[str, str] = {}
    for stamp, root, task, kind, event in sorted(events):
        if stamp.date() <= end:
            statuses[task] = kind
        if "완료" in kind:
            completed.add(task)
        if not start <= stamp.date() <= end:
            continue
        item = row(stamp.date(), root)
        if "롤오버(" in event and task not in rollovers:
            item["rollover"] += 1
        if "자동 계속(" in event:
            item["auto_continue"] += 1
        if "완료" in kind and task in launches:
            seconds = (stamp - min(launches[task])).total_seconds()
            if seconds >= 0:
                item["completion_seconds"][task] = seconds
        if any(x in kind for x in ("실패", "중단", "멈춤")):
            cause = classify(kind + " " + event)
            done = Path(task) / "DONE"
            done_before = done.is_file() and done.stat().st_mtime <= stamp.timestamp()
            if "멈춤" in kind and (task in completed or "DONE" in event or done_before):
                cause = "멈춤 오탐"
            item["causes"][cause] = item["causes"].get(cause, 0) + 1
    for task, (root, _) in tasks.items():
        stamps = [s for s in launches[task] if start <= s.date() <= end]
        if stamps:
            item = row(min(stamps).date(), Path(root))
            count = _tokens(Path(task))
            item["task_tokens"][task] = count
            item["tokens"] += count
            if details:
                status = statuses.get(task, "진행 중")
                markers = [
                    p
                    for p in (Path(task) / "DONE", Path(task) / "FAILED")
                    if p.is_file()
                ]
                if markers:
                    status = max(markers, key=lambda p: p.stat().st_mtime).name
                item.setdefault("task_details", {})[task] = {
                    "attempts": len(stamps),
                    "status": status,
                }
    return [rows[k] for k in sorted(rows)]


def render(rows: list[dict[str, Any]]) -> str:
    result = [
        "| 날짜 | 프로젝트 | launch | 재실행 | 롤오버 | 자동 계속 | 토큰 | 원인 상위 3 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    tasks: dict[str, dict[str, Any]] = {}
    for r in rows:
        counts = Counter(r["causes"])
        guarded = counts.pop("스스로 멈춤 (가드 거부 후)", 0)
        if guarded:
            counts["스스로 멈춤"] += guarded
        causes = (
            ", ".join(
                f"{name} {n}"
                + (
                    f" (가드 거부 후 {guarded})"
                    if name == "스스로 멈춤" and guarded
                    else ""
                )
                for name, n in counts.most_common(3)
            )
            or "없음"
        )
        result.append(
            f"| {r['date']} | {r['project']} | {r['launch']} | {r['retry']} | {r['rollover']} | {r['auto_continue']} | {r['tokens']} | {causes} |"
        )
        for task, tokens in r["task_tokens"].items():
            detail = r.get("task_details", {}).get(task, {})
            item = tasks.setdefault(
                task, {"tokens": 0, "attempts": 0, "status": "-", "seconds": None}
            )
            item["tokens"] += tokens
            item["attempts"] += detail.get("attempts", 0)
            item["status"] = detail.get("status", item["status"])
        for task, seconds in r["completion_seconds"].items():
            if task in tasks:
                tasks[task]["seconds"] = seconds
    result.extend(
        [
            "",
            "| 작업 | 시도 수 | 토큰 | 완료까지 분 | 마지막 상태 |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for task, item in sorted(
        tasks.items(), key=lambda pair: (-pair[1]["tokens"], pair[0])
    )[:15]:
        minutes = f"{item['seconds'] / 60:.1f}" if item["seconds"] is not None else "-"
        result.append(
            f"| {Path(task).name} | {item['attempts']} | {item['tokens']} | {minutes} | {item['status']} |"
        )
    return "\n".join(result)


def daily(now: datetime, wd: Path | None = None) -> None:
    wd = wd or watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    with (wd / "report.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        last = wd / "report.last"
        try:
            if (
                now.timestamp()
                - float(last.read_text(encoding="utf-8", errors="replace"))
                < 86400
            ):
                return
        except (OSError, ValueError):
            pass
        day = now.date() - timedelta(days=1)
        for root in sorted(owners(wd)):
            rows = collect(wd, {root}, day, day, details=True)
            report = root / ".claude" / "reports" / f"loss_{day:%Y%m%d}.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                f"# 손실 리포트 {day}\n\n{render(rows)}\n", encoding="utf-8"
            )
            causes: Counter[str] = Counter()
            for r in rows:
                causes.update(r["causes"])
            top = (
                ", ".join(f"{name} {n}" for name, n in causes.most_common(2)) or "없음"
            )
            inbox = root / ".claude" / "inbox.md"
            with (root / ".claude" / ".inbox_cursor.lock").open("a") as inbox_lock:
                fcntl.flock(inbox_lock, fcntl.LOCK_EX)
                offset = inbox.stat().st_size if inbox.exists() else 0
                with (root / ".claude" / "inbox.routes").open("a") as routes:
                    routes.write(f"{offset}\t*\n")
                with inbox.open("a", encoding="utf-8") as out:
                    out.write(
                        f"{now:%Y-%m-%d %H:%M} | {root} | 손실 리포트 | 손실 리포트: launch {sum(r['launch'] for r in rows)}, 재실행 {sum(r['retry'] for r in rows)}({top}), 토큰 {sum(r['tokens'] for r in rows)} | -\n"
                    )
        last.write_text(str(now.timestamp()))


if __name__ == "__main__":
    try:
        daily(datetime.now().astimezone())
    except Exception as exc:
        wd = watch_dir()
        wd.mkdir(parents=True, exist_ok=True)
        with (wd / "events.log").open("a") as out:
            out.write(f"{datetime.now().isoformat()} report failed: {exc}\n")
        raise
