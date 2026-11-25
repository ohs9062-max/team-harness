from datetime import datetime
from datetime import timedelta
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from team_harness.loss_report import classify
from team_harness.loss_report import collect
from team_harness.loss_report import daily
from team_harness.loss_report import owners
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


@pytest.mark.parametrize("registry", ["maintenance_owners.jsonl", "tasks_done.txt"])
def test_daily_includes_sdk_and_retired_workspace(
    tmp_path: Path, registry: str
) -> None:
    owner, task, watch, now = fixture_data(tmp_path)
    (watch / "tasks.txt").unlink()
    value = json.dumps(str(owner)) if registry.endswith("jsonl") else str(task)
    (watch / registry).write_text(value + "\n")
    tomorrow = now + timedelta(days=1)
    daily(tomorrow, watch)
    report = owner / ".claude/reports" / f"loss_{now:%Y%m%d}.md"
    assert "| 2 | 1 |" in report.read_text()
    assert "손실 리포트: launch 2, 재실행 1" in (owner / ".claude/inbox.md").read_text()


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


def _init_git_repo_with_worktree(base: Path) -> tuple[Path, Path]:
    main_repo = base / "a_main_repo"
    main_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main_repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    wt = base / "z_worktree"
    subprocess.run(
        [
            "git",
            "-C",
            str(main_repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "wt-branch",
            str(wt),
        ],
        check=True,
    )
    return main_repo, wt


def test_owners_normalizes_worktree_file_and_skips_non_repo_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from team_harness import docs_migrate

    main_repo, wt = _init_git_repo_with_worktree(tmp_path)
    (main_repo / ".claude").mkdir()
    wt_file = wt / "design" / "decisions.md"
    wt_file.parent.mkdir(parents=True)
    wt_file.write_text("# Decisions\n", encoding="utf-8")
    orphan_file = tmp_path / "z_orphan.md"
    orphan_file.write_text("orphan\n", encoding="utf-8")

    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))

    task_wt_file = tmp_path / "task_wt_file"
    task_orphan = tmp_path / "task_orphan"
    task_wt_file.mkdir()
    task_orphan.mkdir()
    (task_wt_file / ".th_owner").write_text(str(wt_file) + "\n", encoding="utf-8")
    (task_orphan / ".th_owner").write_text(str(orphan_file) + "\n", encoding="utf-8")
    (watch / "tasks.txt").write_text(
        f"{task_wt_file}\n{task_orphan}\n", encoding="utf-8"
    )

    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    yesterday = now - timedelta(days=1)
    (watch / "usage.jsonl").write_text(
        json.dumps(
            {
                "at": yesterday.isoformat(),
                "owner": str(wt_file),
                "task": str(task_wt_file),
                "attempt": 1,
                "rollover": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    daily(now, watch)
    daily(now, watch)

    inbox_lines = [
        line
        for line in (main_repo / ".claude" / "inbox.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if "손실 리포트" in line
    ]
    assert len(inbox_lines) == 1
    assert "launch 1" in inbox_lines[0]
    assert (watch / "report.last").is_file()
    events = (watch / "events.log").read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    assert str(orphan_file) in events[0]
    assert owners(watch) == {main_repo.resolve()}
    assert docs_migrate.owners() == {main_repo.resolve()}


def test_daily_isolates_failing_root_and_prevents_same_day_duplicate(
    tmp_path: Path,
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    root_a = tmp_path / "a_owner"
    root_b = tmp_path / "b_broken"
    root_c = tmp_path / "c_owner"
    for root in (root_a, root_b, root_c):
        root.mkdir()
    (root_a / ".claude").mkdir()
    # Make .claude a regular file in root_b so report directory creation fails.
    (root_b / ".claude").write_text("not a directory\n", encoding="utf-8")
    (root_c / ".claude").mkdir()

    (watch / "maintenance_owners.jsonl").write_text(
        "\n".join(json.dumps(str(r)) for r in (root_a, root_b, root_c)) + "\n",
        encoding="utf-8",
    )

    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    daily(now, watch)

    assert (watch / "report.last").is_file()
    assert (
        len((root_a / ".claude" / "inbox.md").read_text(encoding="utf-8").splitlines())
        == 1
    )
    assert (
        len((root_c / ".claude" / "inbox.md").read_text(encoding="utf-8").splitlines())
        == 1
    )
    events = (watch / "events.log").read_text(encoding="utf-8")
    assert f"report failed ({root_b.resolve()})" in events

    # Even if report.last is removed on the same day and root_b is repaired,
    # root_a and root_c must not receive duplicate daily lines on the same day.
    (watch / "report.last").unlink()
    (root_b / ".claude").unlink()
    (root_b / ".claude").mkdir()
    daily(now + timedelta(minutes=5), watch)

    assert (
        len((root_a / ".claude" / "inbox.md").read_text(encoding="utf-8").splitlines())
        == 1
    )
    assert (
        len((root_b / ".claude" / "inbox.md").read_text(encoding="utf-8").splitlines())
        == 1
    )
    assert (
        len((root_c / ".claude" / "inbox.md").read_text(encoding="utf-8").splitlines())
        == 1
    )


def test_direct_module_execution_with_file_owner(tmp_path: Path) -> None:
    main_repo, wt = _init_git_repo_with_worktree(tmp_path)
    (main_repo / ".claude").mkdir()
    wt_file = wt / "README.md"
    wt_file.write_text("hi\n", encoding="utf-8")

    watch = tmp_path / "watch"
    watch.mkdir()
    task = tmp_path / "task"
    task.mkdir()
    (task / ".th_owner").write_text(str(wt_file) + "\n", encoding="utf-8")
    (watch / "tasks.txt").write_text(str(task) + "\n", encoding="utf-8")

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TH_WATCH_DIR": str(watch),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-m", "team_harness.loss_report"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr

    inbox = (
        (main_repo / ".claude" / "inbox.md").read_text(encoding="utf-8").splitlines()
    )
    assert len([line for line in inbox if "손실 리포트" in line]) == 1
    assert (watch / "report.last").is_file()


def test_th_owner_writers_reject_or_normalize_file_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from team_harness import crosscheck
    from team_harness import maintenance
    from team_harness.registry_hygiene import clean

    main_repo, wt = _init_git_repo_with_worktree(tmp_path)
    wt_file = wt / "src.py"
    wt_file.write_text("x = 1\n", encoding="utf-8")
    orphan_file = tmp_path / "orphan.txt"
    orphan_file.write_text("orphan\n", encoding="utf-8")

    task = main_repo / "task"
    task.mkdir()
    (task / "task.md").write_text("---\nkind: code\n---\n# Title\n", encoding="utf-8")

    # 1. crosscheck_owner normalizes a file path in .th_owner or TH_WORKSPACE to main repo,
    #    and ignores an orphan non-git file path in favor of the task's repo workspace.
    (task / ".th_owner").write_text(str(wt_file) + "\n", encoding="utf-8")
    assert crosscheck.crosscheck_owner(task) == str(main_repo.resolve())

    (task / ".th_owner").write_text(str(orphan_file) + "\n", encoding="utf-8")
    monkeypatch.setenv("TH_WORKSPACE", str(orphan_file))
    assert crosscheck.crosscheck_owner(task) == str(main_repo.resolve())

    monkeypatch.setenv("TH_WORKSPACE", str(wt_file))
    (task / ".th_owner").unlink()
    assert crosscheck.crosscheck_owner(task) == str(main_repo.resolve())

    # 2. th-launch.sh rejects a non-git file TH_WORKSPACE before writing .th_owner.
    launch_script = Path(__file__).resolve().parents[2] / "scripts" / "th-launch.sh"
    watch = tmp_path / "watch"
    watch.mkdir()
    launch_env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TH_WATCH_DIR": str(watch),
        "TH_WORKSPACE": str(orphan_file),
        "TH_PREFLIGHT_ONLY": "1",
    }
    proc = subprocess.run(
        ["bash", str(launch_script), str(task), "codex", str(main_repo)],
        env=launch_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "not a directory" in proc.stderr
    assert not (task / ".th_owner").exists()

    # 3. maintenance.start and registry_hygiene.clean normalize worktree files and skip orphan files.
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.delenv("TH_NO_MAINTENANCE", raising=False)
    monkeypatch.delenv("TH_ONCE", raising=False)
    with (watch / "maintenance.lock").open("a") as held_lock:
        fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        maintenance.start(orphan_file)
        assert maintenance.registered_owners(watch) == set()
        maintenance.start(wt_file)
        assert maintenance.registered_owners(watch) == {main_repo.resolve()}

    watch2 = tmp_path / "watch2"
    watch2.mkdir()
    old_wt_task = tmp_path / "old_wt"
    old_orphan_task = tmp_path / "old_orphan"
    then = (datetime.now() - timedelta(days=40)).timestamp()
    for t, val in ((old_wt_task, wt_file), (old_orphan_task, orphan_file)):
        t.mkdir()
        (t / "DONE").touch()
        (t / "VERIFIED").touch()
        (t / ".th_owner").write_text(str(val) + "\n", encoding="utf-8")
        os.utime(t / "DONE", (then, then))
        os.utime(t / "VERIFIED", (then, then))
    (watch2 / "tasks.txt").write_text(
        f"{old_wt_task}\n{old_orphan_task}\n", encoding="utf-8"
    )
    clean(watch2, datetime.now().astimezone())
    assert maintenance.registered_owners(watch2) == {main_repo.resolve()}


def test_daily_deduplicates_same_content_across_watch_dirs(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    (owner / ".claude").mkdir(parents=True)
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    for name in ("watch1", "watch2"):
        watch = tmp_path / name
        watch.mkdir()
        (watch / "maintenance_owners.jsonl").write_text(json.dumps(str(owner)) + "\n")
        daily(now, watch)
    lines = (owner / ".claude" / "inbox.md").read_text().splitlines()
    assert len(lines) == 1
