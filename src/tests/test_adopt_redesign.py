from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

import pytest

from team_harness import crosscheck as cc
from team_harness import worker_digest as wd

ROOT = Path(__file__).resolve().parents[2]


def test_missing_verification_and_state_failure(tmp_path: Path) -> None:
    (tmp_path / "task.md").write_text("# 목적\n")
    (tmp_path / "DONE").write_text("완료")
    assert wd.verify(tmp_path)[0]["overall"] == "WARN 검증 없음"
    (tmp_path / "FAILED").write_text("실패")
    assert wd.verify(tmp_path)[0]["overall"] == "FAIL"


def test_adopt_brief_two_and_changed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("# 사용자 목적\n원문 보존")
    source = tmp_path / "source"
    source.write_text("원본")
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("TH_NO_DAEMON", "1")
    launches = []

    def launch(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        launches.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cc.subprocess, "run", launch)
    args = argparse.Namespace(
        folder=str(task), claim="결론", source=[str(source)], two=False
    )
    assert cc.adopt(args) == 0
    folder = task / ".th_adopt"
    brief = (folder / "terra/task.md").read_text()
    assert all(
        s in brief
        for s in (
            "원문 보존",
            "60000",
            "15분",
            "40줄",
            "기준을 스스로",
            str(task),
            str(source),
        )
    )
    assert "## 성공 기준" not in brief
    assert len(launches) == 1 and launches[0][2:5] == [
        "codex",
        "gpt-6.1-terra",
        "medium",
    ]
    (folder / "terra/DONE").write_text("done")
    (folder / "terra/VERDICT.md").write_text("기준: 원문\n반례: 없음\nVERDICT: PASS\n")
    assert cc.verdict(folder)[0] == 0
    decision = argparse.Namespace(
        folder=str(folder),
        adopt=True,
        reject=False,
        reason="근거 확인",
        override_reason=None,
    )
    assert cc.decide(decision) == 0
    assert "결정: 채택" in (folder / "CROSSCHECK.md").read_text()
    (task / "task.md").write_text("변경")
    assert cc.verdict(folder)[1]["terra"][0] == "UNAVAILABLE"
    other = tmp_path / "other"
    other.mkdir()
    (other / "task.md").write_text("# 원문")
    args.folder, args.two = str(other), True
    assert cc.adopt(args) == 0
    assert len(launches) == 3
    assert set(cc.load(other / ".th_adopt")["names"]) == {"sol", "argon"}
    for name, result in (("sol", "FAIL"), ("argon", "PASS")):
        own = other / ".th_adopt" / name
        (own / "DONE").write_text("done")
        (own / "VERDICT.md").write_text(f"기준\n반례: 불일치\nVERDICT: {result}\n")
    assert cc.verdict(other / ".th_adopt")[0] == 1


def test_adopt_restricted_source_copy_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("요청")
    source = tmp_path / "source"
    source.write_text("원본")
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("TH_NO_DAEMON", "1")
    monkeypatch.setattr(cc, "is_coordinator_zone", lambda *a, **kw: True)
    monkeypatch.setattr(
        cc.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    cc.adopt(
        argparse.Namespace(
            folder=str(task), claim="결론", source=[str(source)], two=False
        )
    )
    folder = task / ".th_adopt"
    item = cc.load(folder)["source"][0]
    copy = folder / "terra/source" / item["copy"]
    assert str(copy) in (folder / "terra/task.md").read_text()
    (folder / "terra/DONE").write_text("done")
    (folder / "terra/VERDICT.md").write_text("기준\n반례: 없음\nVERDICT: PASS\n")
    assert cc.verdict(folder)[0] == 0
    copy.write_text("변조")
    assert cc.verdict(folder)[1]["terra"][0] == "UNAVAILABLE"


def test_adopt_default_source_is_git_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    task = repo / "_outputs/task"
    task.mkdir(parents=True)
    (task / "task.md").write_text("원문")
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("TH_NO_DAEMON", "1")
    real_run = subprocess.run

    def run_or_launch(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        if str(cmd[0]).endswith("th-launch.sh"):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(cc.subprocess, "run", run_or_launch)
    cc.adopt(argparse.Namespace(folder=str(task), claim="결론", source=[], two=False))
    folder = task / ".th_adopt"
    assert cc.load(folder)["source"][0]["path"] == str(repo)
    (folder / "terra/DONE").write_text("done")
    (folder / "terra/VERDICT.md").write_text("기준\n반례: 없음\nVERDICT: PASS\n")
    assert cc.verdict(folder)[0] == 0


def test_daemon_started_without_holding_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("요청")
    source = tmp_path / "source"
    source.write_text("원본")
    watch = tmp_path / "watch"
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    monkeypatch.delenv("TH_NO_DAEMON", raising=False)
    monkeypatch.setattr(
        cc.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "", ""),
    )
    started = []

    def start_daemon(*args: Any, **kwargs: Any) -> None:
        with (watch / "watchd.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started.append(True)

    monkeypatch.setattr(cc.subprocess, "Popen", start_daemon)
    cc.adopt(
        argparse.Namespace(
            folder=str(task), claim="결론", source=[str(source)], two=False
        )
    )
    assert started == [True]


def test_launch_watchd_and_status(tmp_path: Path) -> None:
    workspace = tmp_path / "robot3"
    workspace.mkdir()
    task = workspace / "_outputs/task"
    task.mkdir(parents=True)
    (task / "task.md").write_text(
        "---\nkind: code\ntask_key: sample\nfollow_up: normal\n---\n# 사용자 요청\n정확히 검증"
    )
    source = tmp_path / "source"
    source.write_text("원본")
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "report.last").write_text(str(time.time()))
    (watch / "archive.last").write_text(str(time.time()))
    suffix = tmp_path / "suffix"
    suffix.write_text("작업 지시")
    bins = tmp_path / "bin"
    bins.mkdir()
    codex = bins / "codex"
    codex.write_text(
        "#!/bin/sh\nprintf '기준: 요청 충족\\n반례: 없음\\nVERDICT: PASS\\n' > VERDICT.md\nprintf done > DONE\n"
    )
    codex.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_")}
    env.update(
        {
            "PATH": f"{bins}:{env.get('PATH', '')}",
            "TH_WATCH_DIR": str(watch),
            "TH_WORKSPACE": str(workspace),
            "TH_WORKER_SUFFIX": str(suffix),
            "TH_NO_DAEMON": "1",
            "TH_ONCE": "1",
            "TH_JEV_WATCH": "off",
            "TH_DIGEST_COMMAND": "/bin/true",
            "CLAUDECODE": "1",
        }
    )
    run = subprocess.run(
        [
            str(ROOT / "scripts/th-adopt.sh"),
            str(task),
            "검증할 결론",
            "--source",
            str(source),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    folder = task / ".th_adopt"
    result = subprocess.run(
        [str(ROOT / "scripts/th-watchd.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    inbox = (workspace / ".claude/inbox.md").read_text()
    assert "채택 검증 PASS — 검증할 결론 — 반례: 없음" in inbox
    assert "종합: 채택 가능" in (folder / "CROSSCHECK.md").read_text()
    ordinary = workspace / "_outputs/ordinary"
    ordinary.mkdir()
    (ordinary / "task.md").write_text("# 요청")
    (ordinary / "DONE").write_text("done")
    (ordinary / ".th_owner").write_text(str(workspace))
    with (watch / "tasks.txt").open("a") as f:
        f.write(str(ordinary) + "\n")
    assert (
        subprocess.run(
            [str(ROOT / "scripts/th-watchd.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            [str(ROOT / "scripts/th-watchd.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        ).returncode
        == 0
    )
    assert "WARN 검증 없음" in (workspace / ".claude/inbox.md").read_text()
    assert "[WARN 검증 없음]" in (workspace / ".claude/workers.md").read_text()
    status = subprocess.run(
        [str(ROOT / "scripts/th-status.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "ℹ 검증 없음: robot3 1건" in status.stdout
    rerun_env = env | {"TH_CODEX_REASON": "test rerun"}
    relaunched = subprocess.run(
        [
            str(ROOT / "scripts/th-launch.sh"),
            str(task),
            "codex",
            "gpt-6.1-terra",
            "medium",
            str(task),
        ],
        env=rerun_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert relaunched.returncode == 0, relaunched.stderr
    assert (folder / "CROSSCHECK.md").is_file()
    old = time.time() - 8 * 86400
    os.utime(ordinary / "DONE", (old, old))
    status = subprocess.run(
        [str(ROOT / "scripts/th-status.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "ℹ 검증 없음:" not in status.stdout
    data = json.loads((ordinary / "verify_result.json").read_text())
    assert data["overall"] == "WARN 검증 없음"
    os.utime(ordinary / "DONE", None)
    (watch / "tasks.txt").write_text("")
    (watch / "tasks_done.txt").write_text(str(ordinary) + "\n")
    status = subprocess.run(
        [str(ROOT / "scripts/th-status.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "ℹ 검증 없음: robot3 1건" in status.stdout
