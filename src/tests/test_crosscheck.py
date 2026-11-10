from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from team_harness import crosscheck as cc
from team_harness import worker_digest as wd


@pytest.fixture
def sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, argparse.Namespace]:
    claim = tmp_path / "claim"
    criteria = tmp_path / "criteria"
    evidence = tmp_path / "evidence"
    source = tmp_path / "source"
    claim.write_text("A claim")
    criteria.write_text("Correct according to the evidence")
    evidence.write_text("evidence")
    source.write_text("complete source")
    monkeypatch.setattr(
        cc.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    args = argparse.Namespace(
        folder=str(tmp_path / "check"),
        claim=str(claim),
        criteria=str(criteria),
        evidence=[str(evidence)],
        source=[str(source)],
        repo=None,
        force=False,
    )
    assert cc.start(args) == 0
    return tmp_path / "check", args


def finish(folder: Path, name: str, verdict: str) -> None:
    own = folder / name
    (own / "DONE").write_text("done")
    (own / "VERDICT.md").write_text(f"반례와 확인 사항\nVERDICT: {verdict}\n")


@pytest.mark.parametrize(
    "left", ["PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "UNAVAILABLE"]
)
@pytest.mark.parametrize(
    "right", ["PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "UNAVAILABLE"]
)
def test_all_verdicts(
    sample: tuple[Path, argparse.Namespace], left: str, right: str
) -> None:
    folder, _ = sample
    finish(folder, "sol", left)
    finish(folder, "argon", right)
    rc, results, _ = cc.verdict(folder)
    assert rc == (0 if left == right == "PASS" else 1)
    assert results["sol"][0] == left
    assert results["argon"][0] == right


def test_duplicate_force_and_independent_brief(
    sample: tuple[Path, argparse.Namespace],
) -> None:
    folder, args = sample
    sol = (folder / "sol/task.md").read_text()
    argon = (folder / "argon/task.md").read_text()
    assert sol.replace("sol", "argon") == argon
    assert "A claim" in sol and "Argon" not in sol and "코디네이터의 추론" not in sol
    args.folder = str(folder.parent / "second")
    with pytest.raises(ValueError, match="기존 검증"):
        cc.start(args)
    args.force = True
    assert cc.start(args) == 0


def test_pending_and_failures(
    sample: tuple[Path, argparse.Namespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, _ = sample
    monkeypatch.setattr(cc, "alive", lambda path: True)
    assert cc.verdict(folder)[0] == 3
    (folder / "sol/LAUNCH_FAILED").write_text("failed")
    finish(folder, "argon", "PASS")
    assert cc.verdict(folder)[1]["sol"][0] == "UNAVAILABLE"
    monkeypatch.setattr(cc, "alive", lambda path: False)
    (folder / "sol/LAUNCH_FAILED").unlink()
    assert cc.verdict(folder)[1]["sol"][0] == "UNAVAILABLE"
    finish(folder, "sol", "PASS")
    (folder / "sol/VERDICT.md").write_text("VERDICT: PASS\nextra\n")
    assert cc.verdict(folder)[1]["sol"][0] == "UNAVAILABLE"
    monkeypatch.setenv("TH_CROSSCHECK_TIMEOUT_MIN", "0")
    assert all(x[0] == "UNAVAILABLE" for x in cc.verdict(folder)[1].values())


def test_tamper_and_override(sample: tuple[Path, argparse.Namespace]) -> None:
    folder, _ = sample
    for name in cc.NAMES:
        finish(folder, name, "PASS")
    assert cc.verdict(folder)[0] == 0
    (folder / "sol/evidence/000-evidence").write_text("changed")
    assert cc.verdict(folder)[1]["sol"][0] == "UNAVAILABLE"
    args = argparse.Namespace(
        folder=str(folder),
        adopt=True,
        reject=False,
        reason="test",
        override_reason=None,
    )
    with pytest.raises(ValueError, match="override-reason"):
        cc.decide(args)
    args.override_reason = "justified"
    assert cc.decide(args) == 0
    assert "결정: 채택" in (folder / "CROSSCHECK.md").read_text()


def test_repo_change_invalidates_both(
    sample: tuple[Path, argparse.Namespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, _ = sample
    for name in cc.NAMES:
        finish(folder, name, "PASS")
    data = cc.load(folder)
    data["repo"] = str(folder)
    data["repo_state"] = {"head": "old"}
    cc.manifest_path(folder).write_text(json.dumps(data))
    monkeypatch.setattr(cc, "git_state", lambda repo: {"head": "new"})
    assert all(x[0] == "UNAVAILABLE" for x in cc.verdict(folder)[1].values())


def test_artifact_verdict_text_is_not_a_path(tmp_path: Path) -> None:
    (tmp_path / "task.md").write_text(
        "## 기대 산출물\n- `VERDICT.md`: `VERDICT: PASS`\n"
    )
    assert wd.check_artifacts(tmp_path)["verdict"] == "FAIL"
    (tmp_path / "VERDICT.md").write_text("ok")
    assert wd.check_artifacts(tmp_path)["verdict"] == "PASS"


def test_push_guard(tmp_path: Path) -> None:
    import os

    root = tmp_path / "repo"
    root.mkdir()
    env = os.environ | {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "TH_WATCH_DIR": str(tmp_path / "watch"),
    }

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], env=env, text=True
        ).strip()

    git("init", "-q")
    (root / "design").mkdir()
    decisions = root / "design/decisions.md"
    decisions.write_text("## TH-D1. Existing\nOld\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    hook = cc.ROOT / ".githooks/pre-push"

    def push(
        old: str, new: str, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(hook)],
            cwd=root,
            env=env | (extra or {}),
            input=f"refs/heads/main {new} refs/heads/main {old}\n",
            text=True,
            capture_output=True,
            check=False,
        )

    decisions.write_text("## TH-D1. Existing\nEdited\n")
    git("commit", "-qam", "edit")
    edited = git("rev-parse", "HEAD")
    assert push(base, edited).returncode == 0
    decisions.write_text("## TH-D1. Existing\nEdited\n\n## TH-D2. New\nText\n")
    git("commit", "-qam", "new decision")
    new = git("rev-parse", "HEAD")
    assert push(edited, new).returncode != 0
    assert push(edited, new, {"TH_CROSSCHECK_SKIP": "   "}).returncode != 0
    assert push(edited, new, {"TH_CROSSCHECK_SKIP": "reason"}).returncode == 0
    assert "reason" in (tmp_path / "watch/events.log").read_text()
    approved = tmp_path / "approved"
    approved.mkdir()
    (approved / "CROSSCHECK.md").write_text(
        "## 코디네이터 결정\n시각: now\n결정: 채택\n"
    )
    git("commit", "--amend", "-qm", f"new decision\n\nCrosscheck: {approved}")
    # A legacy Markdown approval cannot authorize a new, unrelated decision.
    assert push(edited, git("rev-parse", "HEAD")).returncode != 0
    from team_harness.adoption_gate import record_receipt
    from team_harness.adoption_gate import sha

    (approved / "crosscheck.json").write_text("{}")
    (approved / "evidence.sha256").write_text("evidence")
    (approved / "terra").mkdir()
    (approved / "terra/VERDICT.md").write_text("VERDICT: PASS")
    data = {
        "names": ["terra"],
        "source": [
            {
                "path": str(root),
                "hashes": {"design/decisions.md": sha(decisions.read_bytes())},
            }
        ],
    }
    record_receipt(approved, data, True, None)
    assert push(edited, git("rev-parse", "HEAD")).returncode == 0
    # A subsequent rejection revokes approval; tampering also invalidates it.
    record_receipt(approved, data, False, None)
    assert push(edited, git("rev-parse", "HEAD")).returncode != 0
    record_receipt(approved, data, True, None)
    with (approved / "CROSSCHECK.md").open("a") as out:
        out.write("\n## 코디네이터 결정\n시각: later\n결정: 기각\n")
    assert push(edited, git("rev-parse", "HEAD")).returncode != 0
    record_receipt(approved, data, True, None)
    decisions.write_text(decisions.read_text() + "\n## TH-D3. Unreviewed\ntext\n")
    git("commit", "-qam", f"another decision\n\nCrosscheck: {approved}")
    assert push(edited, git("rev-parse", "HEAD")).returncode != 0


def test_generated_briefs_pass_checker(sample: tuple[Path, argparse.Namespace]) -> None:
    from team_harness.brief_check import check_brief_text

    folder, _ = sample
    for name in cc.NAMES:
        path = folder / name
        assert not check_brief_text((path / "task.md").read_text(), path)


def test_launch_failure_does_not_prevent_other(
    sample: tuple[Path, argparse.Namespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, args = sample
    args.folder = str(folder.parent / "other")
    args.force = True
    workers: list[str] = []

    def fake_launch(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        workers.append(cmd[2])
        return subprocess.CompletedProcess(
            cmd, 1 if cmd[2] == "codex" else 0, "", "error"
        )

    monkeypatch.setattr(cc.subprocess, "run", fake_launch)
    assert cc.start(args) == 0
    assert set(workers) == {"codex", "agy"}
    assert (Path(args.folder) / "sol/LAUNCH_FAILED").is_file()
    assert not (Path(args.folder) / "argon/LAUNCH_FAILED").exists()


def test_real_launch_preflight(
    sample: tuple[Path, argparse.Namespace],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    folder, _ = sample
    monkeypatch.undo()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "TH_PREFLIGHT_ONLY": "1",
            "TH_WATCH_DIR": str(tmp_path / "watch"),
            "TH_WORKSPACE": str(tmp_path),
            "TH_CODEX_REASON": "independent claim crosscheck",
            "TH_HIGH_REASON": "independent claim crosscheck",
            "TH_CODEX_SESSIONS_DIR": str(tmp_path / "empty-sessions"),
        }
    )
    for name, worker, model in (
        ("sol", "codex", "gpt-6.1-sol"),
        ("argon", "agy", "gemini-4-argon-medium"),
    ):
        own = folder / name
        subprocess.check_output(
            [
                str(cc.ROOT / "scripts/th-launch.sh"),
                str(own),
                worker,
                model,
                "medium",
                str(own),
            ],
            env=env,
            stderr=subprocess.STDOUT,
        )


def test_real_launch_with_fake_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the actual launcher, using disposable workspace and fake CLIs."""
    import os

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("TH_IGNORE_BUDGET", raising=False)
    monkeypatch.setenv("TH_NO_DAEMON", "1")
    monkeypatch.setenv("TEAM_HARNESS_JEV", "0")
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path / "watch"))
    monkeypatch.setenv("TH_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("TH_WORKERS_FILE", str(tmp_path / "workers.md"))
    (tmp_path / "workspace").mkdir()
    suffix = tmp_path / "suffix.md"
    suffix.write_text("Complete the assigned work.\n")
    monkeypatch.setenv("TH_WORKER_SUFFIX", str(suffix))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = '#!/bin/sh\nprintf "VERDICT: PASS\\n" > "$TH_WORKER_OUTPUT_DIR/VERDICT.md"\nprintf done > "$TH_WORKER_OUTPUT_DIR/DONE"\n'
    for executable in ("codex", "agy"):
        path = bin_dir / executable
        path.write_text(fake)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    for filename in ("claim", "criteria", "evidence", "source"):
        (tmp_path / filename).write_text(filename)
    args = argparse.Namespace(
        folder=str(tmp_path / "out"),
        claim=str(tmp_path / "claim"),
        criteria=str(tmp_path / "criteria"),
        evidence=[str(tmp_path / "evidence")],
        source=[str(tmp_path / "source")],
        repo=None,
        force=False,
    )
    assert cc.start(args) == 0
    result, details, _ = cc.verdict(Path(args.folder))
    assert result == 0, details


def test_adopt_single_agy_uses_explicit_backend_without_budget_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text("Inspect the source")
    source = tmp_path / "source"
    source.mkdir()
    (source / "original").write_text("original")
    commands = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append((cmd, kwargs.get("env", {})))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    monkeypatch.setenv("TH_NO_DAEMON", "1")
    monkeypatch.setenv("TH_IGNORE_BUDGET", "1")
    args = argparse.Namespace(
        folder=str(task), claim="claim", source=[str(source)], two=False, worker="agy"
    )
    assert cc.adopt(args) == 0
    data = cc.load(task / ".th_adopt")
    assert data["names"] == ["agy"]
    launched = next(
        (cmd, env) for cmd, env in commands if str(cmd[0]).endswith("th-launch.sh")
    )
    assert launched[0][2] == "agy"
    assert "TH_IGNORE_BUDGET" not in launched[1]


def test_push_guard_existing_heading_in_large_document(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    (root / "design").mkdir()
    document = root / "design/decisions.md"
    document.write_text("## TH-D1. Original\n" + "history line\n" * 20000)
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    document.write_text(
        document.read_text().replace("TH-D1. Original", "TH-D1. Renamed")
    )
    git("commit", "-qam", "rename existing heading")
    head = git("rev-parse", "HEAD")
    result = subprocess.run(
        [str(cc.ROOT / ".githooks/pre-push")],
        cwd=root,
        input=f"refs/heads/main {head} refs/heads/main {base}\n",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
