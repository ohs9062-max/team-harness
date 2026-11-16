"""Application-path detection and push gate."""

import os
from pathlib import Path
import subprocess

from team_harness.brief_check import check_brief_text
from team_harness.paths_check import has_paths_table
from team_harness.paths_check import needs_paths
from team_harness.worker_digest import verify

ROOT = Path(__file__).resolve().parents[2]
BRIEF = "---\nkind: code\n---\n## 작업 트리\nteam-harness\n## 할 일\n기능 추가\n"
TABLE = "## 적용 경로\n| 경로 | 붙는 지점 |\n|---|---|\n| th 밖(직접 실행) | hook |\n"


def test_detection() -> None:
    assert needs_paths(BRIEF)
    assert not needs_paths(BRIEF.replace("기능 추가", "버그 수정"))
    assert not needs_paths(BRIEF.replace("kind: code", "kind: docs"))
    assert not needs_paths(BRIEF.replace("kind: code", "kind: code\npaths: n/a 문서만"))
    assert not needs_paths(
        BRIEF.replace("kind: code", "kind: code\npaths: th-only 이유")
    )
    assert not needs_paths(BRIEF.replace("team-harness", "other-repo"))
    assert has_paths_table(TABLE)
    assert not has_paths_table(TABLE.replace("th 밖", "th 내부"))
    assert any(x.rule == "missing_paths" for x in check_brief_text(BRIEF))
    assert not any(x.rule == "missing_paths" for x in check_brief_text(BRIEF + TABLE))


def test_verify_warning(tmp_path: Path) -> None:
    (tmp_path / "task.md").write_text(BRIEF)
    (tmp_path / "DONE").write_text("done")
    result, text = verify(tmp_path)
    assert result["items"]["적용 경로"]["verdict"] == "WARN"
    assert "- 적용 경로: [WARN] th 밖(직접 실행) 열 없음" in text


def test_push_gate(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path), "TH_WATCH_DIR": str(tmp_path / "watch")}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    designs = tmp_path / "design/designs"
    designs.mkdir(parents=True)
    (tmp_path / "design/decisions.md").write_text("## TH-D1. old\n")
    (designs / "old.md").write_text(TABLE.replace("th 밖", "내부"))
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "unrelated").write_text("ok")
    git("add", ".")
    git("commit", "-qm", "unrelated")
    clean = git("rev-parse", "HEAD")

    def push(
        old: str, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / ".githooks/pre-push")],
            cwd=tmp_path,
            env={**env, **(extra or {})},
            input=f"refs/heads/main {git('rev-parse', 'HEAD')} refs/heads/main {old}\n",
            capture_output=True,
            text=True,
        )

    assert push(base).returncode == 0
    (designs / "old.md").write_text(TABLE.replace("th 밖", "내부") + "\nchange\n")
    git("add", ".")
    git("commit", "-qm", "change design")
    assert push(clean).returncode != 0
    assert push(clean, {"TH_PATHS_SKIP": "사유"}).returncode == 0
    assert "[paths skip] 사유" in (tmp_path / "watch/events.log").read_text()
    previous = git("rev-parse", "HEAD")
    (tmp_path / "design/decisions.md").write_text(
        "## TH-D1. old\n\n## TH-D2. new\n내용\n"
    )
    git("add", ".")
    git("commit", "-qm", "new decision")
    assert push(previous, {"TH_CROSSCHECK_SKIP": "test"}).returncode != 0
