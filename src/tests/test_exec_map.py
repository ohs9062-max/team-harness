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
        "python train.py; ls",
    ):
        assert classify(cmd, missing) is None


def test_custom_and_invalid_config(tmp_path: Path) -> None:
    config = tmp_path / "map.toml"
    config.write_text('[[rules]]\nkind="experiment"\npattern="^custom "\n')
    assert classify("custom run", config) == "experiment"
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
