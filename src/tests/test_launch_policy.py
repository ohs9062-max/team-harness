"""Unit and CLI tests for team_harness.launch_policy."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import time

import pytest

from team_harness.launch_policy import check
from team_harness.launch_policy import check_warnings
from team_harness.launch_policy import collect_overrides
from team_harness.launch_policy import main
from team_harness.launch_policy import sum_recent_codex_tokens
from team_harness.launch_policy import Violation


def _make_task(
    root: Path,
    name: str = "task_a",
    *,
    kind: str | None = "code",
    front_matter: bool = True,
) -> Path:
    task_dir = root / name
    task_dir.mkdir(parents=True, exist_ok=True)
    if front_matter:
        lines = ["---", f"task_key: {name}", "follow_up: normal"]
        if kind is not None:
            lines.append(f"kind: {kind}")
        lines.extend(["---", f"# {name}", "Task body."])
        content = "\n".join(lines) + "\n"
    else:
        content = f"# {name}\nTask body without front matter.\n"
    (task_dir / "task.md").write_text(content, encoding="utf-8")
    return task_dir


def _write_rollout(
    sessions_dir: Path,
    name: str,
    total_tokens: int = 0,
    *,
    input_tokens: int | None = None,
    cached_input_tokens: int = 0,
    output_tokens: int = 100,
    mtime: float | None = None,
    timestamp: float | None = None,
) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / name
    actual_input = (
        total_tokens - output_tokens if input_tokens is None else input_tokens
    )
    actual_total = total_tokens if total_tokens > 0 else actual_input + output_tokens
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": name}}),
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": datetime.fromtimestamp(
                    time.time() if timestamp is None else timestamp, timezone.utc
                ).isoformat(),
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": actual_input,
                            "cached_input_tokens": cached_input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": actual_total,
                        }
                    },
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return {"TH_CODEX_SESSIONS_DIR": str(sessions_dir), **extra}


def _launch_records(watch_dir: Path, task: Path, count: int) -> None:
    watch_dir.mkdir(exist_ok=True)
    rows = [
        {
            "v": 1,
            "at": f"2026-10-23T00:0{index}:00Z",
            "task": str(task),
            "attempt": index + 1,
            "worker": "codex",
            "model": "gpt-5.6-terra",
            "effort": "medium",
            "owner": str(task.parent),
            "override": None,
        }
        for index in range(count)
    ]
    (watch_dir / "usage.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


@pytest.mark.parametrize(("max_retries", "allowed"), (("2", 3), ("1", 2)))
def test_retry_cap_allows_total_attempts_then_rejects_next(
    tmp_path: Path, max_retries: str, allowed: int
) -> None:
    watch_dir = tmp_path / "watch"
    task = _make_task(tmp_path, "retry_cap")
    env = _base_env(tmp_path, TH_MAX_RETRIES=max_retries)
    _launch_records(watch_dir, task, allowed - 1)
    assert check(task, "codex", "gpt-5.6-terra", "medium", watch_dir, env) == []
    _launch_records(watch_dir, task, allowed)
    violations = check(task, "codex", "gpt-5.6-terra", "medium", watch_dir, env)
    assert [item.code for item in violations] == ["retry_cap"]
    assert f"시도 {allowed}/{allowed}" in violations[0].message
    assert (
        main(
            ["check", str(task), "codex", "gpt-5.6-terra", "medium", str(watch_dir)],
            env=env,
        )
        == 3
    )


def test_retry_reason_overrides_cap_and_is_audited(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    task = _make_task(tmp_path, "retry_reason")
    _launch_records(watch_dir, task, 3)
    env = _base_env(tmp_path, TH_RETRY_REASON="새 스텁으로 원인 분리")
    assert check(task, "codex", "gpt-5.6-terra", "medium", watch_dir, env) == []
    assert collect_overrides(env) == ["TH_RETRY_REASON=새 스텁으로 원인 분리"]


def test_parallel_pass_when_below_limit_and_dead_pids_ignored(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    alive_task = _make_task(tmp_path, "alive_1")
    (alive_task / "worker.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (alive_task / "codex.log").write_text("running\n", encoding="utf-8")

    dead_task = _make_task(tmp_path, "dead_1")
    (dead_task / "worker.pid").write_text("99999999\n", encoding="utf-8")
    (dead_task / "codex.log").write_text("finished\n", encoding="utf-8")

    other_worker = _make_task(tmp_path, "agy_1")
    (other_worker / "worker.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (other_worker / "agy.log").write_text("running agy\n", encoding="utf-8")

    target_task = _make_task(tmp_path, "target")
    (watch_dir / "tasks.txt").write_text(
        f"{alive_task}\n{dead_task}\n{other_worker}\n{target_task}\n", encoding="utf-8"
    )

    violations = check(
        target_task, "codex", "gpt-5.6-terra", "medium", watch_dir, _base_env(tmp_path)
    )
    assert violations == []


def test_parallel_reject_when_at_or_above_limit(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    task_a = _make_task(tmp_path, "task_a")
    task_b = _make_task(tmp_path, "task_b")
    for item in (task_a, task_b):
        (item / "worker.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        (item / "codex.log").write_text("running\n", encoding="utf-8")

    target = _make_task(tmp_path, "task_c")
    (watch_dir / "tasks.txt").write_text(f"{task_a}\n{task_b}\n", encoding="utf-8")

    violations = check(
        target, "codex", "gpt-5.6-terra", "medium", watch_dir, _base_env(tmp_path)
    )
    assert len(violations) == 1
    assert violations[0] == Violation(
        "parallel",
        "codex 동시 실행 2개 상한: 이미 2개 실행 중(task_a, task_b). 끝난 뒤 띄우거나 TH_MAX_PARALLEL=3",
    )


def test_parallel_override_with_th_max_parallel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    for name in ("task_a", "task_b"):
        item = _make_task(tmp_path, name)
        (item / "worker.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        (item / "codex.log").write_text("running\n", encoding="utf-8")
        with (watch_dir / "tasks.txt").open("a", encoding="utf-8") as stream:
            stream.write(f"{item}\n")

    target = _make_task(tmp_path, "task_c")
    env = _base_env(tmp_path, TH_MAX_PARALLEL="3")
    violations = check(target, "codex", "gpt-5.6-terra", "medium", watch_dir, env)
    assert violations == []

    rc = main(
        ["check", str(target), "codex", "gpt-5.6-terra", "medium", str(watch_dir)],
        env=env,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "override: TH_MAX_PARALLEL=3" in captured.out


def test_high_pass_with_terra_medium(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, "task_normal")
    violations = check(
        target, "codex", "gpt-5.6-terra", "medium", watch_dir, _base_env(tmp_path)
    )
    assert violations == []


@pytest.mark.parametrize(
    ("model", "effort"),
    [("gpt-5.6-terra", "high"), ("gpt-5.6-terra", "xhigh"), ("gpt-6.1-sol", "medium")],
)
def test_high_reject_without_reason(tmp_path: Path, model: str, effort: str) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, "task_high")
    violations = check(
        target,
        "codex",
        model,
        effort,
        watch_dir,
        _base_env(tmp_path, TH_HIGH_REASON="   "),
    )
    assert [v.code for v in violations] == ["high"]
    assert "TH_HIGH_REASON" in violations[0].message


def test_high_override_with_th_high_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, "task_high_ok")
    env = _base_env(tmp_path, TH_HIGH_REASON="복잡한 동시성 버그 디버깅")
    violations = check(target, "codex", "gpt-6.1-sol", "high", watch_dir, env)
    assert violations == []

    rc = main(
        ["check", str(target), "codex", "gpt-6.1-sol", "high", str(watch_dir)], env=env
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "override: TH_HIGH_REASON=복잡한 동시성 버그 디버깅" in captured.out


def test_kind_pass_when_kind_is_code_or_non_codex_worker(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    code_task = _make_task(tmp_path, "task_code", kind="code")
    docs_task = _make_task(tmp_path, "task_docs", kind="docs")
    env = _base_env(tmp_path)

    assert check(code_task, "codex", "gpt-5.6-terra", "medium", watch_dir, env) == []
    assert (
        check(docs_task, "agy", "Gemini 3.8 Flash (High)", "none", watch_dir, env) == []
    )


@pytest.mark.parametrize("kind", [None, "research", "data", "docs", "invalid"])
def test_kind_reject_when_missing_or_not_code(tmp_path: Path, kind: str | None) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, f"task_{kind}", kind=kind)
    violations = check(
        target, "codex", "gpt-5.6-terra", "medium", watch_dir, _base_env(tmp_path)
    )
    assert [v.code for v in violations] == ["kind"]
    assert "TH_CODEX_REASON" in violations[0].message


def test_kind_override_with_th_codex_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, "task_docs_codex", kind="docs")
    env = _base_env(tmp_path, TH_CODEX_REASON="코드와 문서 동시 검증")
    violations = check(target, "codex", "gpt-5.6-terra", "medium", watch_dir, env)
    assert violations == []

    rc = main(
        ["check", str(target), "codex", "gpt-5.6-terra", "medium", str(watch_dir)],
        env=env,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "override: TH_CODEX_REASON=코드와 문서 동시 검증" in captured.out


def test_budget_pass_and_warning_and_old_rollout_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, "task_budget_ok")
    sessions_dir = tmp_path / "sessions"
    now = time.time()

    _write_rollout(
        sessions_dir,
        "rollout-old.jsonl",
        1_050_000,
        input_tokens=1_040_000,
        cached_input_tokens=100_000,
        output_tokens=10_000,
        mtime=now - 6 * 3600,
    )
    _write_rollout(
        sessions_dir,
        "rollout-recent-1.jsonl",
        500_000,
        input_tokens=490_000,
        cached_input_tokens=100_000,
        output_tokens=10_000,
        mtime=now - 600,
    )
    _write_rollout(
        sessions_dir,
        "rollout-recent-2.jsonl",
        450_000,
        input_tokens=440_000,
        cached_input_tokens=100_000,
        output_tokens=10_000,
        mtime=now - 300,
    )

    env = _base_env(tmp_path, TH_CODEX_5H_BUDGET="1000000")
    violations = check(target, "codex", "gpt-5.6-terra", "medium", watch_dir, env)
    assert violations == []
    warnings = check_warnings("codex", env)
    assert len(warnings) == 1
    assert (
        "토큰 사용량(캐시 제외, codex 'tokens used' 기준) 70% 이상(750,000/1,000,000, 75%)"
        in warnings[0]
    )

    rc = main(
        ["check", str(target), "codex", "gpt-5.6-terra", "medium", str(watch_dir)],
        env=env,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "70% 이상" in captured.err


def test_budget_pass_when_cached_input_tokens_large(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, "task_budget_cached_ok")
    sessions_dir = tmp_path / "sessions"
    _write_rollout(
        sessions_dir,
        "rollout-cached.jsonl",
        1_010_000,
        input_tokens=1_000_000,
        cached_input_tokens=950_000,
        output_tokens=10_000,
    )

    env = _base_env(tmp_path, TH_CODEX_5H_BUDGET="1000000")
    assert sum_recent_codex_tokens(sessions_dir) == 60_000
    assert check(target, "codex", "gpt-5.6-terra", "medium", watch_dir, env) == []
    assert check_warnings("codex", env) == []


def test_budget_counts_only_token_growth_inside_window(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    now = time.time()
    path = sessions_dir / "rollout-window.jsonl"
    path.parent.mkdir()

    def event(timestamp: float, total: int) -> str:
        return json.dumps(
            {
                "type": "event_msg",
                "timestamp": datetime.fromtimestamp(
                    timestamp, timezone.utc
                ).isoformat(),
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": total - 100,
                            "cached_input_tokens": 0,
                            "output_tokens": 100,
                        }
                    },
                },
            }
        )

    path.write_text(
        event(now - 6 * 3600, 900_000) + "\n" + event(now - 3600, 1_000_000) + "\n",
        encoding="utf-8",
    )
    assert sum_recent_codex_tokens(sessions_dir) == 100_000


def test_budget_reject_when_at_or_above_90_percent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, "task_budget_reject")
    sessions_dir = tmp_path / "sessions"
    _write_rollout(sessions_dir, "rollout-1.jsonl", 500_000)
    _write_rollout(sessions_dir, "rollout-2.jsonl", 400_000)

    env = _base_env(tmp_path, TH_CODEX_5H_BUDGET="1000000")
    violations = check(target, "codex", "gpt-5.6-terra", "medium", watch_dir, env)
    assert [v.code for v in violations] == ["budget"]
    assert (
        "토큰 사용량(캐시 제외, codex 'tokens used' 기준) 90% 이상"
        in violations[0].message
    )
    assert "TH_IGNORE_BUDGET=1" in violations[0].message

    rc = main(
        ["check", str(target), "codex", "gpt-5.6-terra", "medium", str(watch_dir)],
        env=env,
    )
    captured = capsys.readouterr()
    assert rc == 4
    assert "TH_IGNORE_BUDGET=1" in captured.err


def test_budget_override_with_th_ignore_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = _make_task(tmp_path, "task_budget_override")
    sessions_dir = tmp_path / "sessions"
    _write_rollout(sessions_dir, "rollout-1.jsonl", 950_000)

    env = _base_env(tmp_path, TH_CODEX_5H_BUDGET="1000000", TH_IGNORE_BUDGET="1")
    violations = check(target, "codex", "gpt-5.6-terra", "medium", watch_dir, env)
    assert violations == []

    rc = main(
        ["check", str(target), "codex", "gpt-5.6-terra", "medium", str(watch_dir)],
        env=env,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "override: TH_IGNORE_BUDGET=1" in captured.out
