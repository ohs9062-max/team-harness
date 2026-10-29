from datetime import datetime
from datetime import timedelta
import json
from pathlib import Path

import pytest

from team_harness.loss_report import classify
from team_harness.loss_report import collect
from team_harness.loss_report import daily


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("INCOMPLETE 남은 작업", "스스로 멈춤"),
        ("PreToolUse 거부", "가드 거부"),
        ("rate limit", "쿼터"),
        ("워커 중단(인계 없음)", "인계 없음/중단"),
        ("stall", "멈춤"),
        ("unknown", "기타"),
    ],
)
def test_classify(text: str, expected: str) -> None:
    assert classify(text) == expected


def fixture_data(tmp_path: Path) -> tuple[Path, Path, Path, datetime]:
    owner = tmp_path / "owner"
    task = owner / "task"
    task.mkdir(parents=True)
    watch = tmp_path / "watch"
    watch.mkdir()
    (owner / ".claude").mkdir()
    (task / ".th_owner").write_text(str(owner))
    (watch / "tasks.txt").write_text(str(task) + "\n")
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    entries = [
        dict(
            at=now.isoformat(),
            owner=str(owner),
            task=str(task),
            attempt=n,
            rollover=n == 2,
        )
        for n in (1, 2)
    ]
    (watch / "usage.jsonl").write_text("\n".join(json.dumps(x) for x in entries))
    (task / "codex.log").write_text("tokens used\n1,234\ntokens used\n567\n")
    (owner / ".claude" / "inbox.md").write_text(
        f"{now:%Y-%m-%d %H:%M} | {task} | 자동 계속(1/2) | 자동 계속(1/2) | -\n{now:%Y-%m-%d %H:%M} | {task} | 롤오버(1/3) | 롤오버(1/3) | -\n"
    )
    return owner, task, watch, now


def test_retries(tmp_path: Path) -> None:
    owner, _, watch, now = fixture_data(tmp_path)
    row = collect(watch, {owner}, now.date(), now.date())[0]
    assert (row["launch"], row["retry"]) == (2, 1)


def test_continuations(tmp_path: Path) -> None:
    owner, _, watch, now = fixture_data(tmp_path)
    row = collect(watch, {owner}, now.date(), now.date())[0]
    assert (row["rollover"], row["auto_continue"]) == (1, 1)


def test_tokens(tmp_path: Path) -> None:
    owner, task, watch, now = fixture_data(tmp_path)
    row = collect(watch, {owner}, now.date(), now.date())[0]
    assert row["tokens"] == row["task_tokens"][str(task)] == 1801


def test_date_range(tmp_path: Path) -> None:
    owner, _, watch, now = fixture_data(tmp_path)
    yesterday = now.date() - timedelta(days=1)
    assert collect(watch, {owner}, yesterday, yesterday) == []


def test_daily_once(tmp_path: Path) -> None:
    owner, _, watch, now = fixture_data(tmp_path)
    daily(now, watch)
    inbox = owner / ".claude" / "inbox.md"
    initial = inbox.read_text()
    daily(now + timedelta(hours=23), watch)
    assert inbox.read_text() == initial
    assert (
        owner
        / ".claude"
        / "reports"
        / f"loss_{now.date() - timedelta(days=1):%Y%m%d}.md"
    ).exists()


def test_events_and_false_stall(tmp_path: Path) -> None:
    owner, task, watch, now = fixture_data(tmp_path)
    later = now + timedelta(minutes=2)
    (watch / "events.log").write_text(
        f"{later:%Y-%m-%d %H:%M:%S} [완료] {task}: 완료\n"
        f"{later + timedelta(minutes=1):%Y-%m-%d %H:%M:%S} [멈춤] {task}: 멈춤\n"
    )
    row = collect(watch, {owner}, now.date(), now.date())[0]
    assert row["causes"]["멈춤 오탐"] == 1
    assert row["completion_seconds"][str(task)] == 120


def test_event_duplicate_not_counted_twice(tmp_path: Path) -> None:
    owner, task, watch, now = fixture_data(tmp_path)
    with (owner / ".claude" / "inbox.md").open("a") as out:
        out.write(f"{now:%Y-%m-%d %H:%M} | {task} | 실패 | INCOMPLETE | -\n")
    (watch / "events.log").write_text(
        f"{now:%Y-%m-%d %H:%M:%S} [실패] {task}: INCOMPLETE\n"
    )
    row = collect(watch, {owner}, now.date(), now.date())[0]
    assert row["causes"] == {"스스로 멈춤": 1}
