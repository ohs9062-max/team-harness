from pathlib import Path

import pytest

from team_harness.exec_map import classify


def test_default_mapping(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    for cmd in (
        "python train.py --epochs 1",
        "uv run python evaluate.py",
        "torchrun benchmark.py",
        "codex exec task",
        "claude -p hello",
        "agy -p hello",
    ):
        assert classify(cmd, missing)
    for cmd in (
        "ls -la",
        "git status",
        "python",
        "claude",
        "agy",
        "th-job-start.sh task -- python train.py",
    ):
        assert classify(cmd, missing) is None


def test_custom_and_invalid_config(tmp_path: Path) -> None:
    config = tmp_path / "map.toml"
    config.write_text('[[rules]]\nkind="experiment"\npattern="^custom "\n')
    assert classify("custom run", config) == "experiment"
    config.write_text(
        '[[rules]]\nkind="experiment"\npattern=' + "'" + '^custom "hi"$' + "'" + "\n"
    )
    assert classify('custom "hi"', config) == "experiment"
    config.write_text("invalid = [")
    assert classify("python train.py", config) == "experiment"


def test_hook_blocks_when_pre_stage_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util
    import json

    from team_harness.exec_pipeline import StageResult

    path = Path(__file__).resolve().parents[2] / "scripts/hooks/th-exec-pipeline.py"
    spec = importlib.util.spec_from_file_location("claude_exec_hook", path)
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path))
    monkeypatch.delenv("TH_ROLE", raising=False)
    monkeypatch.delenv("TH_EXEC_WRAP", raising=False)
    monkeypatch.setattr(hook, "ancestor_is_th", lambda: False)
    monkeypatch.setattr(
        hook, "run_pre", lambda event: [StageResult("fake", 1, "blocked test", True)]
    )
    hook.main(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "python train.py"},
            "tool_use_id": "test",
        }
    )
    result = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert result["permissionDecision"] == "deny"
    assert result["permissionDecisionReason"] == "blocked test"
    assert not (tmp_path / "claude-jobs").exists()


def test_codex_hook_rewrite_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util
    import json

    path = Path(__file__).resolve().parents[2] / "scripts/hooks/th-exec-pipeline.py"
    spec = importlib.util.spec_from_file_location("codex_exec_hook", path)
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setenv("TH_WATCH_DIR", str(tmp_path))
    monkeypatch.delenv("TH_ROLE", raising=False)
    monkeypatch.delenv("TH_EXEC_WRAP", raising=False)
    monkeypatch.setattr(hook, "ancestor_is_th", lambda: False)
    for i, command in enumerate(("codex exec hi", "python train.py", "ls")):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_use_id": str(i),
            "session_id": "s",
        }
        hook.main(payload, codex=True)
        output = capsys.readouterr().out
        if command == "ls":
            assert not output
            continue
        result = json.loads(output)["hookSpecificOutput"]
        assert result["permissionDecision"] == "allow"
        assert "--execute" in result["updatedInput"]["command"]
        state = hook.state_path(payload)
        assert state.exists()
        payload["hook_event_name"] = "PostToolUse"
        hook.main(payload, codex=True)
        assert not state.exists()
    monkeypatch.setenv("TH_ROLE", "worker")
    payload["hook_event_name"] = "PreToolUse"
    payload["tool_input"]["command"] = "codex exec hi"
    hook.main(payload, codex=True)
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd /x && python3 train.py", "experiment"),
        ("python3 train.py 2>&1 | tail -20", "experiment"),
        ("A=1 python train.py; echo done", "experiment"),
        ("env A=1 python train.py\necho done", "experiment"),
        ("cd x &&\npython train.py", "experiment"),
        ("python train.py &\n", None),
        ('cd x && codex exec "hi"', "agent_cli"),
        ("python train.py && codex exec hi", "agent_cli"),
        ("ls && echo hi", None),
        ("python3 train.py &", None),
        ("bash scripts/th-job-start.sh task && python train.py", None),
        ("python 'train.py", None),
    ],
)
def test_compound_commands(tmp_path: Path, command: str, expected: str | None) -> None:
    assert classify(command, tmp_path / "missing") == expected


def test_custom_rule_on_compound_segment(tmp_path: Path) -> None:
    config = tmp_path / "map.toml"
    config.write_text('[[rules]]\nkind="experiment"\npattern="^custom "\n')
    assert classify("cd x && env A=1 custom run", config) == "experiment"
