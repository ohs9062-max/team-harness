"""Parse durable per-step START/END records from a task ``STATUS`` file.

The module intentionally depends on the Python standard library only: shell
launchers and watchers use it with the system interpreter as well as the
development virtualenv.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re

STEP_NAME = r"[A-Za-z0-9_.-]+"
TIMESTAMP = r"(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
START_RE = re.compile(
    rf"^{TIMESTAMP} START step=(?P<step>{STEP_NAME})"
    r"(?: replay=(?P<replay>safe|never|inspect))?\s*$"
)
END_RE = re.compile(
    rf"^{TIMESTAMP} END step=(?P<step>{STEP_NAME}) rc=(?P<rc>-?\d+)"
    r"(?: out=(?P<out>\S+))?\s*$"
)


@dataclass(frozen=True)
class StepRecord:
    """The final observed state for one named step."""

    name: str
    state: str
    replay: str
    at: str
    rc: int | None = None
    out: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


def parse_status_text(text: str) -> dict[str, StepRecord]:
    """Return final records, retaining all STATUS history across LAUNCH lines.

    A later START supersedes an earlier outcome for that name.  An END without
    a visible START is still useful evidence, so it is represented with the
    conservative ``inspect`` replay policy.
    """

    records: dict[str, StepRecord] = {}
    for line in text.splitlines():
        start = START_RE.match(line)
        if start:
            name = start["step"]
            at = start["at"]
            records[name] = StepRecord(
                name=name,
                state="open",
                replay=start["replay"] or "inspect",
                at=at,
                started_at=at,
            )
            continue

        end = END_RE.match(line)
        if not end:
            continue
        name = end["step"]
        rc = int(end["rc"])
        previous = records.get(name)
        records[name] = StepRecord(
            name=name,
            state="completed" if rc == 0 else "failed",
            replay=previous.replay if previous else "inspect",
            at=end["at"],
            rc=rc,
            out=end["out"],
            started_at=previous.started_at if previous else None,
            ended_at=end["at"],
        )
    return records


def parse_task(task_dir: Path) -> dict[str, StepRecord]:
    """Parse ``task_dir/STATUS``; a missing file has no step records."""

    try:
        text = (task_dir / "STATUS").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return parse_status_text(text)


def recovery_summary(records: dict[str, StepRecord], task_dir: Path) -> str:
    """Render the bounded, deterministic relaunch note for a task."""

    if not records:
        return ""

    completed = [record for record in records.values() if record.state == "completed"]
    failed = [record for record in records.values() if record.state == "failed"]
    safe_open = [
        record
        for record in records.values()
        if record.state == "open" and record.replay == "safe"
    ]
    cautious_open = [
        record
        for record in records.values()
        if record.state == "open" and record.replay != "safe"
    ]

    def names(items: list[StepRecord], include_replay: bool = False) -> str:
        result = []
        for record in items:
            label = record.name
            if include_replay:
                label += f"(replay={record.replay})"
            if record.out:
                label += f"(out={record.out})"
            result.append(label)
        return ", ".join(result)

    lines = ["## 이전 시도 복구 요약 (자동 생성, STATUS 단계 기록 기준)"]
    if completed:
        lines.append(f"- 완료(다시 하지 말 것): {names(completed)}")
    if safe_open:
        lines.append(f"- 중단 replay=safe → 이 단계부터 다시 실행: {names(safe_open)}")
    if cautious_open:
        lines.append(
            "- 중단 replay=never/inspect → 다시 실행 전 증거 확인 필수: "
            f"{names(cautious_open, include_replay=True)} "
            "(확인 방법: out 경로 존재, git log 등. 이미 됐으면 END 기록 후 진행, "
            "불확실하면 FAILED)"
        )
    if failed:
        lines.append(
            "- 실패: "
            + ", ".join(f"{record.name}(rc={record.rc})" for record in failed)
        )
    lines.append(
        f"- 인계 문서: {task_dir}/WORKLOG.md 만 읽는다. 이전 codex.log/agy.log·세션 기록은 읽지 않는다."
    )
    return "\n".join(lines[:20]) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("summary", "open-steps"))
    parser.add_argument("task_dir", type=Path)
    args = parser.parse_args(argv)
    task_dir = args.task_dir.resolve()
    records = parse_task(task_dir)
    if args.mode == "summary":
        print(recovery_summary(records, task_dir), end="")
    else:
        for record in sorted(records.values(), key=lambda item: item.at):
            if record.state == "open":
                print(
                    "\t".join((record.name, record.replay, record.at, record.out or ""))
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
