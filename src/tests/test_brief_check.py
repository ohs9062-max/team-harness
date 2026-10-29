from pathlib import Path

from team_harness.brief_check import check_brief_text

FIXTURES = Path(__file__).parent / "fixtures" / "brief_check"
DATA = Path(__file__).parent / "data" / "brief_check"


def test_real_briefs():
    for task in sorted(DATA.glob("good_*")):
        assert not [
            f
            for f in check_brief_text((task / "task.md").read_text(), task)
            if f.level == "error"
        ], task
    task = DATA / "warn_rollover_fix"
    findings = check_brief_text((task / "task.md").read_text(), task)
    assert not [f for f in findings if f.level == "error"]
    assert any(f.rule == "foreign_path" and f.level == "warn" for f in findings)
    task = DATA / "bad_done_linger"
    assert any(
        f.rule == "foreign_path" and f.level == "error" and f.line == 25
        for f in check_brief_text((task / "task.md").read_text(), task)
    )


def test_path_severity_and_shell_syntax():
    findings = check_brief_text(
        "## 배경\n`_outputs/other/report` 기록\n## 읽을 문서\n`_outputs/other/report`\n## 할 일\nread `_outputs/other/report`\n읽지 않는다 `_outputs/other/report`",
        Path("/tmp/current"),
    )
    assert [(f.level, f.line) for f in findings if f.rule == "foreign_path"] == [
        ("warn", 2),
        ("error", 4),
        ("error", 6),
    ]
    shell = "## 검수\n```bash\nS=foo && for d in $S/*; do ! true || { echo fail; exit 1; }; done && test ok && [[ -n $S ]]\n```"
    assert not [f for f in check_brief_text(shell) if f.rule == "missing_command"]


def test_section_prefixes_and_read_instructions():
    text = "\n".join(
        [
            "read `_outputs/other/a`",
            "## 재현 (예시)",
            "cat `_outputs/other/b`",
            "## 현재 문제 부연",
            "read `_outputs/other/c`",
            "## 읽을 문서(필수)",
            "`_outputs/other/d`",
            "## 할 일 (순서)",
            "1. read `_outputs/other/e`",
            "2. `_outputs/other/f` 기록",
            "## 배경",
            "read `_outputs/other/g`",
        ]
    )
    assert [
        (f.level, f.line)
        for f in check_brief_text(text, Path("/tmp/current"))
        if f.rule == "foreign_path"
    ] == [
        ("warn", 1),
        ("warn", 3),
        ("warn", 5),
        ("error", 7),
        ("error", 9),
        ("warn", 10),
        ("warn", 12),
    ]


def rules(text: str) -> set[str]:
    return {f.rule for f in check_brief_text(text, Path("/tmp/current"))}


def sample(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_bad_rollover():
    assert {"foreign_path", "escape_phrase"} <= rules(
        sample("sample_bad_rollover_fix.md")
    )


def test_bad_archive():
    assert "foreign_path" in rules(sample("sample_bad_doc_archive.md"))


def test_bad_linger():
    assert "foreign_path" in rules(sample("sample_bad_done_linger.md"))


def test_good_sample():
    assert not [
        f
        for f in check_brief_text(sample("sample_good_job_tag_leak.md"))
        if f.level == "error"
    ]


def test_foreign_output():
    assert "foreign_path" in rules("read `_outputs/other/report.md`")


def test_own_output():
    assert "foreign_path" not in rules("read `_outputs/current/report.md`")


def test_forbidden():
    assert "foreign_path" not in rules("다른 `_outputs/other/report.md`는 읽지 않는다.")


def test_watch():
    assert "foreign_path" in rules("`~/.team-harness/watch/events.log`에서 확인")


def test_missing_command():
    assert "missing_command" in rules("## 검수\n```bash\nth_nonexistent_cmd_123\n```")


def test_uv_subcommand_excluded():
    assert "missing_command" not in rules("## 검수\n```bash\nuv run imaginary\n```")


def test_weak():
    assert "weak_acceptance" in rules(
        "---\nkind: code\n---\n## 검수\n```bash\ngit log | grep foo\n```"
    )


def test_missing_acceptance():
    assert "missing_acceptance" in rules("---\nkind: code\n---\n내용")


def test_escape():
    assert "escape_phrase" in rules("완료한 단계까지 검증한다")


def test_no_escape():
    assert "escape_phrase" not in rules("일부만 완료하지 않는다")


def test_allow_paths():
    assert "allow_paths_ambiguous" in rules("---\nallow_paths:\n  - foo\n---")


def test_allow_paths_explicit():
    assert "allow_paths_ambiguous" not in rules(
        "---\nallow_paths:\n  - foo\n---\n작업 트리 전체와 이 작업 폴더는 쓰기 허용"
    )


def test_launch_rejects_and_overrides(tmp_path: Path):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[2]
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text(
        "---\nkind: code\ntask_key: example\n---\n## 검수\n```bash\nth_missing_command_xyz\n```\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)
    suffix = tmp_path / "suffix.md"
    suffix.write_text("worker")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TH_WORKSPACE": str(tmp_path),
        "TH_WATCH_DIR": str(tmp_path / "watch"),
        "TH_WORKER_SUFFIX": str(suffix),
        "TH_NO_DAEMON": "1",
        "TH_DIGEST_COMMAND": "/bin/true",
        "TEAM_HARNESS_JEV": "0",
    }
    env.pop("TH_BRIEF_OK", None)
    cmd = [str(root / "scripts/th-launch.sh"), str(task), "codex", str(root)]
    rejected = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    assert rejected.returncode == 2
    assert "missing_command" in rejected.stderr
    accepted = subprocess.run(
        cmd,
        env={**env, "TH_BRIEF_OK": "test reason"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


def test_sibling_task_path():
    assert "foreign_path" in rules("read /tmp/other/report.md")


def test_sibling_task_excluded_from_worktree_add():
    assert "foreign_path" not in rules("git worktree add /tmp/other -b branch")
