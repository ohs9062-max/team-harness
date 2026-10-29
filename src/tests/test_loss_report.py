from datetime import datetime
from datetime import timedelta
import json
from pathlib import Path

import pytest

from team_harness.loss_report import classify
from team_harness.loss_report import collect
from team_harness.loss_report import daily
from team_harness.loss_report import render


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("INCOMPLETE 남은 작업", "스스로 멈춤"),
        ("PreToolUse 거부", "가드 거부"),
        ("rate limit", "쿼터"),
        ("워커 중단(인계 없음)", "인계 없음"),
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


def test_invalid_utf8_events_and_inbox(tmp_path: Path) -> None:
    owner, task, watch, now = fixture_data(tmp_path)
    with (owner / ".claude" / "inbox.md").open("ab") as out:
        out.write(f"{now:%Y-%m-%d %H:%M} | {task} | 실패 | quota ".encode() + b"\xff\n")
    (watch / "events.log").write_bytes(
        f"{now:%Y-%m-%d %H:%M:%S} [실패] {task}: guard ".encode() + b"\xff\n"
    )
    row = collect(watch, {owner}, now.date(), now.date())[0]
    assert row["causes"]["쿼터"] == 1


def test_invalid_json_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    owner, _, watch, now = fixture_data(tmp_path)
    with (watch / "usage.jsonl").open("ab") as out:
        out.write(b"\n{bad json\n\xff\n")
    assert collect(watch, {owner}, now.date(), now.date())[0]["launch"] == 2
    assert "2" in capsys.readouterr().err


def test_missing_codex_log(tmp_path: Path) -> None:
    owner, task, watch, now = fixture_data(tmp_path)
    (task / "codex.log").unlink()
    assert collect(watch, {owner}, now.date(), now.date())[0]["tokens"] == 0


def test_incomplete_precedes_guard() -> None:
    assert classify("INCOMPLETE PreToolUse 거부") == "스스로 멈춤 (가드 거부 후)"
    assert classify("미완료 가드 거부") == "스스로 멈춤 (가드 거부 후)"
    assert classify("남은 항목") == "스스로 멈춤"


def test_done_file_before_stall(tmp_path: Path) -> None:
    owner, task, watch, now = fixture_data(tmp_path)
    done = task / "DONE"
    done.touch()
    import os

    os.utime(done, (now.timestamp(), now.timestamp()))
    later = now + timedelta(minutes=1)
    (watch / "events.log").write_text(
        f"{later:%Y-%m-%d %H:%M:%S} [멈춤] {task}: guard 거부\n"
    )
    row = collect(watch, {owner}, now.date(), now.date())[0]
    assert row["causes"] == {"멈춤 오탐": 1}


def test_render_task_details(tmp_path: Path) -> None:
    owner, task, watch, now = fixture_data(tmp_path)
    (task / "DONE").touch()
    later = now + timedelta(minutes=2)
    (watch / "events.log").write_text(
        f"{later:%Y-%m-%d %H:%M:%S} [완료] {task}: 완료\n"
    )
    output = render(collect(watch, {owner}, now.date(), now.date(), details=True))
    assert "| task | 2 | 1801 | 2.0 | DONE |" in output
    assert "{'" not in output


def test_render_top_15_and_causes() -> None:
    row = dict(
        date="2026-01-01",
        project="p",
        launch=16,
        retry=0,
        rollover=0,
        auto_continue=0,
        tokens=120,
        causes={"a": 1, "b": 4, "c": 3, "d": 2},
        completion_seconds={},
        task_tokens={f"/tmp/task{i}": i for i in range(16)},
    )
    output = render([row])
    assert "b 4, c 3, d 2" in output
    assert "| task0 |" not in output
    assert "| task15 |" in output


def test_json_collection_has_no_details(tmp_path: Path) -> None:
    owner, _, watch, now = fixture_data(tmp_path)
    row = collect(watch, {owner}, now.date(), now.date())[0]
    assert "task_details" not in row


def test_render_combines_guarded_self_stops() -> None:
    row = dict(
        date="2026-01-01",
        project="p",
        launch=0,
        retry=0,
        rollover=0,
        auto_continue=0,
        tokens=0,
        causes={"스스로 멈춤": 4, "스스로 멈춤 (가드 거부 후)": 3},
        completion_seconds={},
        task_tokens={},
    )
    assert "스스로 멈춤 7 (가드 거부 후 3)" in render([row])
