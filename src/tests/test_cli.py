# pyright: reportMissingParameterType=false, reportArgumentType=false

import json
from types import SimpleNamespace

import click
from click.testing import CliRunner
import pytest

from team_harness import config as config_module
from team_harness.cli import _repl
from team_harness.cli import _run
from team_harness.cli import main
from team_harness.config import Config
from team_harness.coordinator.system_prompt import COORDINATOR_PROMPT
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER
from team_harness.harness import _warn_provider_startup
from team_harness.protocol.config import ProtocolConfig


def test_help_uses_th_prog_name():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"], prog_name="th")
    assert result.exit_code == 0
    assert "Usage: th" in result.output
    assert "th \u2014 multi-agent AI orchestration harness." in result.output


def test_team_harness_alias_still_works():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"], prog_name="team-harness")
    assert result.exit_code == 0
    assert "Usage: team-harness" in result.output


def test_logs_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["logs"], prog_name="th")
    assert result.exit_code == 0
    assert "No runs yet." in result.output


def test_init_creates_local_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init"])

    config_path = tmp_path / ".team-harness" / "config.toml"
    coordinator_prompt_path = (
        tmp_path / ".team-harness" / "coordinator_system_message.md"
    )
    worker_suffix_path = tmp_path / ".team-harness" / "worker_suffix.md"
    worker_footer_path = tmp_path / ".team-harness" / "worker_footer.md"
    assert result.exit_code == 0
    assert config_path.exists()
    assert coordinator_prompt_path.read_text(encoding="utf-8") == COORDINATOR_PROMPT
    assert worker_suffix_path.read_text(encoding="utf-8") == ""
    assert worker_footer_path.read_text(encoding="utf-8") == DEFAULT_WORKER_FOOTER
    assert "Project-level team-harness config." in config_path.read_text()
    assert str(config_path) in result.output


def test_init_refuses_overwrite_without_force(monkeypatch, tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("existing")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init"])

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert config_path.read_text() == "existing"


def test_init_force_overwrites_local(monkeypatch, tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("existing")
    coordinator_prompt_path = config_path.parent / "coordinator_system_message.md"
    worker_suffix_path = config_path.parent / "worker_suffix.md"
    worker_footer_path = config_path.parent / "worker_footer.md"
    coordinator_prompt_path.write_text("keep coordinator prompt", encoding="utf-8")
    worker_suffix_path.write_text("keep worker suffix", encoding="utf-8")
    worker_footer_path.write_text("keep worker footer", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--force"])

    assert result.exit_code == 0
    assert "Project-level team-harness config." in config_path.read_text()
    assert (
        coordinator_prompt_path.read_text(encoding="utf-8") == "keep coordinator prompt"
    )
    assert worker_suffix_path.read_text(encoding="utf-8") == "keep worker suffix"
    assert worker_footer_path.read_text(encoding="utf-8") == "keep worker footer"


def test_init_force_creates_missing_sidecars(monkeypatch, tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("existing", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--force"])

    assert result.exit_code == 0
    assert (config_path.parent / "coordinator_system_message.md").read_text(
        encoding="utf-8"
    ) == (COORDINATOR_PROMPT)
    assert (config_path.parent / "worker_suffix.md").read_text(encoding="utf-8") == ""
    assert (config_path.parent / "worker_footer.md").read_text(
        encoding="utf-8"
    ) == DEFAULT_WORKER_FOOTER


def test_init_global_creates_global_config(monkeypatch, tmp_path):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    monkeypatch.setattr("team_harness.cli.CONFIG_PATH", global_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--global"])

    assert result.exit_code == 0
    assert global_path.exists()
    assert 'model = "gpt-5.6-sol"' in global_path.read_text()
    assert (global_path.parent / "coordinator_system_message.md").read_text(
        encoding="utf-8"
    ) == (COORDINATOR_PROMPT)
    assert (global_path.parent / "worker_suffix.md").read_text(encoding="utf-8") == ""
    assert (global_path.parent / "worker_footer.md").read_text(
        encoding="utf-8"
    ) == DEFAULT_WORKER_FOOTER
    assert str(global_path) in result.output


def test_init_global_refuses_overwrite_without_force(monkeypatch, tmp_path):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("existing")
    monkeypatch.setattr("team_harness.cli.CONFIG_PATH", global_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--global"])

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert global_path.read_text() == "existing"


def test_init_global_force_overwrites(monkeypatch, tmp_path):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("existing")
    monkeypatch.setattr("team_harness.cli.CONFIG_PATH", global_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--global", "--force"])

    assert result.exit_code == 0
    generated = global_path.read_text()
    # Legacy single-string form is removed.
    import re

    assert re.search(r"(?m)^template\s*=\s*\"", generated) is None
    # Structured form is visible.
    assert "[agents.codex]" in generated
    assert 'command = ["codex", "exec"]' in generated
    assert "[agents.codex.session_capture]" in generated


@pytest.mark.asyncio
async def test_run_without_config_prints_no_config_hint(monkeypatch, tmp_path):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    printed_messages: list[str] = []

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            printed_messages.append(message)

        def begin_turn(self, n):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    async def fake_run(messages, config, run_log, ui, tool_registry, client, ctx):
        return None

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.harness.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.make_console", lambda **_: FakeConsole())
    monkeypatch.setattr("team_harness.harness.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.harness.build_system_prompt", lambda *args, **kwargs: "system"
    )
    monkeypatch.setattr("team_harness.harness.run", fake_run)

    await _run(
        task="hello",
        task_file=None,
        cwd=str(project_dir),
        api_base="http://localhost:11434/v1",
    )

    no_config_messages = [m for m in printed_messages if "No config file found" in m]
    assert len(no_config_messages) == 1


@pytest.mark.asyncio
async def test_repl_creates_session_output_dir_and_passes_it_to_prompt(
    monkeypatch, tmp_path
):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    async def fake_read_user_input(session):
        return None

    captured: dict[str, str] = {}
    output_root = tmp_path / "project" / "artifacts"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    def fake_build_system_prompt(*, config, allowed_types, skills, session_output_dir):
        captured["session_output_dir"] = session_output_dir
        return "system"

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: FakeConsole())
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", fake_build_system_prompt
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)

    local_config = project_dir / ".team-harness" / "config.toml"
    local_config.parent.mkdir()
    local_config.write_text('[coordinator]\noutput_dir = "artifacts"\n')

    await _repl(cwd=str(project_dir), api_base="http://localhost:11434/v1")

    session_output_dir = config_module.Path(captured["session_output_dir"])
    assert "session_output_dir" in captured
    assert session_output_dir.parent == output_root
    assert session_output_dir.is_dir()
    manifest = session_output_dir / "worker_sessions.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text())["workers"] == []


@pytest.mark.asyncio
async def test_repl_exception_path_still_writes_worker_manifest(monkeypatch, tmp_path):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    async def fake_read_user_input(session):
        raise RuntimeError("input failed")

    captured: dict[str, str] = {}
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    def fake_build_system_prompt(*, config, allowed_types, skills, session_output_dir):
        captured["session_output_dir"] = session_output_dir
        return "system"

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: FakeConsole())
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", fake_build_system_prompt
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)

    with pytest.raises(RuntimeError, match="input failed"):
        await _repl(cwd=str(project_dir), api_base="http://localhost:11434/v1")

    manifest = (
        config_module.Path(captured["session_output_dir"]) / "worker_sessions.json"
    )
    assert manifest.exists()
    assert json.loads(manifest.read_text())["workers"] == []


def test_run_command_passes_provider_flags(monkeypatch):
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("team_harness.cli._run", fake_run)
    runner = CliRunner()

    result = runner.invoke(
        main, ["run", "--provider", "codex", "--codex-auth-path", "auth.json", "hello"]
    )

    assert result.exit_code == 0
    assert captured["provider"] == "codex"
    assert captured["codex_auth_path"] == "auth.json"
    assert captured["task"] == "hello"


def test_warn_provider_startup_for_codex_unknown_model():
    messages: list[str] = []

    class _CaptureUI:
        def print(self, msg: str) -> None:
            messages.append(msg)

    _warn_provider_startup(
        Config(provider="codex", model="custom-codex-model"), ui=_CaptureUI()
    )

    combined = "\n".join(messages)
    assert "provider=codex is experimental" in combined
    assert "context tracking may be inaccurate" in combined


@pytest.mark.asyncio
async def test_clear_command_dispatches_locally(monkeypatch, tmp_path):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def __init__(self):
            self.reset_count = 0
            self.messages: list[str] = []

        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            self.messages.append(message)

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_compaction(self):
            return None

        def end_compaction(self, before_tokens, after_tokens):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            self.reset_count += 1

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    inputs = iter(["/clear", None])
    fake_console = FakeConsole()
    run_calls: list[int] = []

    async def fake_read_user_input(session):
        return next(inputs)

    async def fake_run_one_turn(**kwargs):
        run_calls.append(1)
        return False, kwargs["last_logged_index"]

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: fake_console)
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", lambda *args, **kwargs: "system"
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)
    monkeypatch.setattr("team_harness.cli.run_one_turn", fake_run_one_turn)

    await _repl(cwd=str(tmp_path), api_base="http://localhost:11434/v1")

    assert run_calls == []
    assert fake_console.reset_count == 1
    assert "Context reset. Agent state and run log preserved." in fake_console.messages


@pytest.mark.asyncio
async def test_reset_alias_dispatches_same_clear_handler(monkeypatch, tmp_path):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def __init__(self):
            self.reset_count = 0
            self.messages: list[str] = []

        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            self.messages.append(message)

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_compaction(self):
            return None

        def end_compaction(self, before_tokens, after_tokens):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            self.reset_count += 1

        def print_user_prompt(self, text):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    inputs = iter(["first", "/reset", "second", None])
    fake_console = FakeConsole()
    last_logged_inputs: list[int] = []
    message_snapshots: list[list[dict]] = []
    token_snapshots: list[tuple[int, int]] = []

    async def fake_read_user_input(session):
        return next(inputs)

    async def fake_run_one_turn(**kwargs):
        last_logged_inputs.append(kwargs["last_logged_index"])
        message_snapshots.append([message.copy() for message in kwargs["messages"]])
        token_snapshots.append(
            (kwargs["ctx"].prompt_tokens, kwargs["ctx"].completion_tokens)
        )
        kwargs["ctx"].prompt_tokens = 222
        kwargs["ctx"].completion_tokens = 111
        kwargs["messages"].append({"role": "assistant", "content": "done"})
        return False, len(kwargs["messages"])

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: fake_console)
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", lambda *args, **kwargs: "system"
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)
    monkeypatch.setattr("team_harness.cli.run_one_turn", fake_run_one_turn)

    await _repl(cwd=str(tmp_path), api_base="http://localhost:11434/v1")

    assert last_logged_inputs == [0, 0]
    assert fake_console.reset_count == 1
    assert "Context reset. Agent state and run log preserved." in fake_console.messages
    assert message_snapshots[1] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "second"},
    ]
    assert token_snapshots[1] == (0, 0)


@pytest.mark.asyncio
async def test_compact_command_dispatches_manual_compaction_without_focus(
    monkeypatch, tmp_path
):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_compaction(self):
            return None

        def end_compaction(self, before_tokens, after_tokens, success=True):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_user_prompt(self, text):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    inputs = iter(["first", "/compact", "second", None])
    fake_console = FakeConsole()
    compact_calls: list[dict] = []
    run_last_logged_indices: list[int] = []
    message_snapshots: list[list[dict]] = []

    async def fake_read_user_input(session):
        return next(inputs)

    async def fake_run_one_turn(**kwargs):
        run_last_logged_indices.append(kwargs["last_logged_index"])
        message_snapshots.append([message.copy() for message in kwargs["messages"]])
        kwargs["messages"].append({"role": "assistant", "content": "done"})
        return False, len(kwargs["messages"])

    async def fake_perform_manual_compaction(*, messages, client, ctx, ui, focus_text):
        compact_calls.append(
            {
                "messages": [message.copy() for message in messages],
                "focus_text": focus_text,
            }
        )
        messages[:] = [
            messages[0],
            {"role": "system", "content": "boundary"},
            {"role": "user", "content": "summary"},
        ]
        return True

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: fake_console)
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", lambda *args, **kwargs: "system"
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)
    monkeypatch.setattr("team_harness.cli.run_one_turn", fake_run_one_turn)
    monkeypatch.setattr(
        "team_harness.cli._perform_manual_compaction", fake_perform_manual_compaction
    )

    await _repl(cwd=str(tmp_path), api_base="http://localhost:11434/v1")

    assert len(compact_calls) == 1
    assert compact_calls[0]["focus_text"] is None
    assert compact_calls[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done"},
    ]
    assert run_last_logged_indices == [0, 0]
    assert message_snapshots == [
        [{"role": "system", "content": "system"}, {"role": "user", "content": "first"}],
        [
            {"role": "system", "content": "system"},
            {"role": "system", "content": "boundary"},
            {"role": "user", "content": "summary"},
            {"role": "user", "content": "second"},
        ],
    ]


@pytest.mark.asyncio
async def test_compact_command_dispatches_manual_compaction_with_focus(
    monkeypatch, tmp_path
):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_compaction(self):
            return None

        def end_compaction(self, before_tokens, after_tokens, success=True):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_user_prompt(self, text):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    inputs = iter(
        [
            "seed",
            "/compact   focus area   ",
            "after-fail",
            "/compact done",
            "after-success",
            None,
        ]
    )
    fake_console = FakeConsole()
    compact_calls: list[dict] = []
    run_last_logged_indices: list[int] = []
    message_snapshots: list[list[dict]] = []
    compact_results = iter([False, True])

    async def fake_read_user_input(session):
        return next(inputs)

    async def fake_run_one_turn(**kwargs):
        run_last_logged_indices.append(kwargs["last_logged_index"])
        message_snapshots.append([message.copy() for message in kwargs["messages"]])
        kwargs["messages"].append({"role": "assistant", "content": "done"})
        return False, len(kwargs["messages"])

    async def fake_perform_manual_compaction(*, messages, client, ctx, ui, focus_text):
        compact_calls.append(
            {
                "messages": [message.copy() for message in messages],
                "focus_text": focus_text,
            }
        )
        if next(compact_results):
            messages[:] = [
                messages[0],
                {"role": "system", "content": "boundary"},
                {"role": "user", "content": "summary"},
            ]
            return True
        return False

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: fake_console)
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", lambda *args, **kwargs: "system"
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)
    monkeypatch.setattr("team_harness.cli.run_one_turn", fake_run_one_turn)
    monkeypatch.setattr(
        "team_harness.cli._perform_manual_compaction", fake_perform_manual_compaction
    )

    await _repl(cwd=str(tmp_path), api_base="http://localhost:11434/v1")

    assert [call["focus_text"] for call in compact_calls] == ["focus area", "done"]
    assert all(
        all(
            message.get("content") not in {"/compact   focus area   ", "/compact done"}
            for message in call["messages"]
        )
        for call in compact_calls
    )
    assert run_last_logged_indices == [0, 3, 0]
    assert message_snapshots == [
        [{"role": "system", "content": "system"}, {"role": "user", "content": "seed"}],
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "after-fail"},
        ],
        [
            {"role": "system", "content": "system"},
            {"role": "system", "content": "boundary"},
            {"role": "user", "content": "summary"},
            {"role": "user", "content": "after-success"},
        ],
    ]


@pytest.mark.asyncio
async def test_compact_command_with_only_trailing_whitespace_passes_none_focus(
    monkeypatch, tmp_path
):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_compaction(self):
            return None

        def end_compaction(self, before_tokens, after_tokens, success=True):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    inputs = iter(["/compact   ", None])
    fake_console = FakeConsole()
    compact_calls: list[dict] = []

    async def fake_read_user_input(session):
        return next(inputs)

    async def fake_run_one_turn(**kwargs):
        raise AssertionError("run_one_turn should not be called for /compact")

    async def fake_perform_manual_compaction(*, messages, client, ctx, ui, focus_text):
        compact_calls.append(
            {
                "messages": [message.copy() for message in messages],
                "focus_text": focus_text,
            }
        )
        return False

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: fake_console)
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", lambda *args, **kwargs: "system"
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)
    monkeypatch.setattr("team_harness.cli.run_one_turn", fake_run_one_turn)
    monkeypatch.setattr(
        "team_harness.cli._perform_manual_compaction", fake_perform_manual_compaction
    )

    await _repl(cwd=str(tmp_path), api_base="http://localhost:11434/v1")

    assert len(compact_calls) == 1
    assert compact_calls[0]["focus_text"] is None
    assert compact_calls[0]["messages"] == [{"role": "system", "content": "system"}]


@pytest.mark.asyncio
async def test_compact_command_tab_separator_is_not_recognized_as_compact(
    monkeypatch, tmp_path
):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_compaction(self):
            return None

        def end_compaction(self, before_tokens, after_tokens, success=True):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_user_prompt(self, text):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    inputs = iter(["/compact\tfoo", None])
    fake_console = FakeConsole()
    run_message_snapshots: list[list[dict]] = []
    compact_calls: list[dict] = []

    async def fake_read_user_input(session):
        return next(inputs)

    async def fake_run_one_turn(**kwargs):
        run_message_snapshots.append([message.copy() for message in kwargs["messages"]])
        kwargs["messages"].append({"role": "assistant", "content": "done"})
        return False, len(kwargs["messages"])

    async def fake_perform_manual_compaction(*, messages, client, ctx, ui, focus_text):
        compact_calls.append(
            {
                "messages": [message.copy() for message in messages],
                "focus_text": focus_text,
            }
        )
        return False

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: fake_console)
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", lambda *args, **kwargs: "system"
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)
    monkeypatch.setattr("team_harness.cli.run_one_turn", fake_run_one_turn)
    monkeypatch.setattr(
        "team_harness.cli._perform_manual_compaction", fake_perform_manual_compaction
    )

    await _repl(cwd=str(tmp_path), api_base="http://localhost:11434/v1")

    assert compact_calls == []
    assert run_message_snapshots == [
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "/compact\tfoo"},
        ]
    ]


@pytest.mark.asyncio
async def test_clear_preserves_system_prompt_and_resets_last_logged_index(
    monkeypatch, tmp_path
):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_compaction(self):
            return None

        def end_compaction(self, before_tokens, after_tokens):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_user_prompt(self, text):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    inputs = iter(["first", "/clear", "second", None])
    last_logged_inputs: list[int] = []
    message_snapshots: list[list[dict]] = []

    async def fake_read_user_input(session):
        return next(inputs)

    async def fake_run_one_turn(**kwargs):
        last_logged_inputs.append(kwargs["last_logged_index"])
        kwargs["messages"].append({"role": "assistant", "content": "done"})
        message_snapshots.append([message.copy() for message in kwargs["messages"]])
        return False, len(kwargs["messages"])

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: FakeConsole())
    monkeypatch.setattr("team_harness.cli.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt",
        lambda *args, **kwargs: "system prompt text",
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)
    monkeypatch.setattr("team_harness.cli.run_one_turn", fake_run_one_turn)

    await _repl(cwd=str(tmp_path), api_base="http://localhost:11434/v1")

    assert last_logged_inputs == [0, 0]
    assert message_snapshots[0][0] == {
        "role": "system",
        "content": "system prompt text",
    }
    assert [message["role"] for message in message_snapshots[0]] == [
        "system",
        "user",
        "assistant",
    ]
    assert message_snapshots[1] == [
        {"role": "system", "content": "system prompt text"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "done"},
    ]


def test_protocol_set_role_parses_into_role_tables():
    from team_harness.cli import _parse_set_role

    tables = _parse_set_role(
        (
            "mode_c_implement.model=gpt-5.6-sol",
            "mode_c_implement.effort=medium",
            "mode_a_final.agent=claude",
        )
    )

    assert tables == {
        "mode_c_implement": {"model": "gpt-5.6-sol", "effort": "medium"},
        "mode_a_final": {"agent": "claude"},
    }


@pytest.mark.parametrize(
    "value",
    [
        "mode_c_implement.model",
        "mode_c_implement=gpt-5.6-sol",
        "mode_c_implement.model=",
        "mode_c_implemnt.model=x",
        "mode_c_implement.modle=x",
    ],
)
def test_protocol_set_role_rejects_malformed_or_unknown_overrides(value):
    """A typo must fail loudly, not leave the expensive default in place."""
    from team_harness.cli import _parse_set_role

    with pytest.raises(click.BadParameter):
        _parse_set_role((value,))


def test_protocol_run_forwards_role_and_timeout_overrides(monkeypatch, tmp_path):
    """`th protocol run` must actually pass its new flags to the mode function."""
    captured: dict[str, object] = {}

    async def fake_run_mode_c(**kwargs):
        captured.update(kwargs)
        state = SimpleNamespace(status="DONE", stage="FINAL")
        return state

    class FakeRunner:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr("team_harness.protocol.mode_c.run_mode_c", fake_run_mode_c)
    monkeypatch.setattr(
        "team_harness.protocol.mode_c.TeamHarnessAgentRunner", FakeRunner
    )
    monkeypatch.setattr("team_harness.cli._print_protocol_state", lambda *a, **k: None)
    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "protocol",
            "run",
            "do the thing",
            "--repo",
            str(tmp_path),
            "--no-visible",
            "--agent-timeout",
            "42",
            "--check-timeout",
            "7",
            "--set-role",
            "mode_c_implement.model=gpt-5.6-sol",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["agent_timeout_sec"] == 42
    assert captured["check_timeout_sec"] == 7
    proto_cfg = captured["protocol_config"]
    assert isinstance(proto_cfg, ProtocolConfig)
    assert proto_cfg.mode_c_implement.model == "gpt-5.6-sol"


def test_protocol_run_rejects_a_zero_timeout():
    runner = CliRunner()
    result = runner.invoke(
        main, ["protocol", "run", "task", "--agent-timeout", "0", "--no-visible"]
    )

    assert result.exit_code != 0
    assert "--agent-timeout" in result.output


def _save_protocol_state(run_dir, **fields):
    from team_harness.protocol.models import ProtocolState
    from team_harness.protocol.state import ProtocolStateManager

    run_dir.mkdir(parents=True, exist_ok=True)
    ProtocolStateManager(run_dir).save_state(ProtocolState(**fields))
    return run_dir


def test_protocol_status_prints_stages_and_the_failure_reason(tmp_path):
    run_dir = _save_protocol_state(
        tmp_path / "protocol-run1",
        task_id="TASK-1",
        mode="C",
        stage="REVIEW",
        status="BLOCKED",
        base_branch="main",
        base_commit="a" * 40,
        target_repo=str(tmp_path / "repo"),
        blocker="REVIEW stage failed",
        worktrees={"pipeline": {"branch": "task/TASK-1/pipeline", "path": "/tmp/wt"}},
        handoffs=[
            {
                "stage": "DESIGN",
                "agent_type": "claude",
                "effective_model": "claude-sonnet-5",
                "success": True,
                "spawned": True,
            },
            {
                "stage": "REVIEW",
                "agent_type": "antigravity",
                "success": False,
                "spawned": False,
                "failure_classification": {
                    "category": "rate_limit",
                    "resets_at": "2026-07-23T13:00:00+00:00",
                },
            },
        ],
    )

    result = CliRunner().invoke(main, ["protocol", "status", "--run-dir", str(run_dir)])

    assert result.exit_code == 0, result.output
    assert "MODE C" in result.output
    assert "DESIGN" in result.output and "claude-sonnet-5" in result.output
    # A stage the harness refused to launch reads as such, with the cause.
    assert "실행되지 않음" in result.output
    assert "rate_limit" in result.output
    assert "2026-07-23T13:00:00+00:00" in result.output
    assert "blocker: REVIEW stage failed" in result.output
    # An unfinished MODE C run tells you how to continue it.
    assert "th protocol resume" in result.output


def test_protocol_status_lists_recent_runs_without_a_run_dir(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    _save_protocol_state(runs / "protocol-aaa", task_id="T-A", mode="C", status="DONE")
    _save_protocol_state(
        runs / "protocol-bbb", task_id="T-B", mode="A", status="WAITING_USER"
    )
    monkeypatch.setattr("team_harness.cli.RUNS_DIR", runs)

    result = CliRunner().invoke(main, ["protocol", "status"])

    assert result.exit_code == 0, result.output
    assert "protocol-bbb" in result.output and "WAITING_USER" in result.output
    assert "protocol-aaa" in result.output and "DONE" in result.output


def test_protocol_status_reports_a_missing_state_file(tmp_path):
    result = CliRunner().invoke(
        main, ["protocol", "status", "--run-dir", str(tmp_path / "nope")]
    )

    assert result.exit_code != 0
    assert "No protocol state found" in result.output


def test_protocol_resume_routes_a_mode_c_run_to_resume_mode_c(monkeypatch, tmp_path):
    run_dir = _save_protocol_state(
        tmp_path / "protocol-c", task_id="T-C", mode="C", status="BLOCKED"
    )
    captured: dict[str, object] = {}

    async def fake_resume_mode_c(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status="DONE",
            stage="FINAL",
            blocker=None,
            user_selection=None,
            merge_status="PENDING",
            active_branch=None,
            checkpoint=None,
        )

    async def fail_resume_mode_a(**kwargs):
        raise AssertionError("MODE A resume must not run for a MODE C run")

    monkeypatch.setattr(
        "team_harness.protocol.mode_c.resume_mode_c", fake_resume_mode_c
    )
    monkeypatch.setattr(
        "team_harness.protocol.mode_a.resume_mode_a", fail_resume_mode_a
    )
    monkeypatch.setattr(
        "team_harness.protocol.mode_c.TeamHarnessAgentRunner", lambda **kwargs: object()
    )

    result = CliRunner().invoke(
        main,
        [
            "protocol",
            "resume",
            "--task-id",
            "T-C",
            "--run-dir",
            str(run_dir),
            "--repo",
            str(tmp_path),
            "--no-visible",
            "--from-stage",
            "IMPLEMENT",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["from_stage"] == "IMPLEMENT"


def test_protocol_resume_rejects_mismatched_mode_flags(tmp_path):
    mode_c = _save_protocol_state(
        tmp_path / "protocol-c2", task_id="T-C", mode="C", status="BLOCKED"
    )
    mode_a = _save_protocol_state(
        tmp_path / "protocol-a2", task_id="T-A", mode="A", status="WAITING_USER"
    )
    runner = CliRunner()
    base = [
        "protocol",
        "resume",
        "--task-id",
        "T",
        "--no-visible",
        "--repo",
        str(tmp_path),
    ]

    selection_on_c = runner.invoke(
        main, base + ["--run-dir", str(mode_c), "--selection", "SELECT_WORKER_1"]
    )
    assert selection_on_c.exit_code != 0
    assert "--selection applies to MODE A" in selection_on_c.output

    stage_on_a = runner.invoke(
        main,
        base
        + ["--run-dir", str(mode_a), "--selection", "REWORK", "--from-stage", "REVIEW"],
    )
    assert stage_on_a.exit_code != 0
    assert "--from-stage applies to MODE C" in stage_on_a.output

    missing_selection = runner.invoke(main, base + ["--run-dir", str(mode_a)])
    assert missing_selection.exit_code != 0
    assert "--selection is required" in missing_selection.output


def test_protocol_status_prints_the_spend_rollup(tmp_path):
    run_dir = _save_protocol_state(
        tmp_path / "protocol-spend",
        task_id="TASK-S",
        mode="C",
        stage="FINAL",
        status="DONE",
        handoffs=[
            {
                "stage": "DESIGN",
                "agent_type": "claude",
                "effective_model": "claude-sonnet-5",
                "success": True,
                "spawned": True,
                "duration_sec": 12.0,
                "usage": {"input_tokens": 900, "output_tokens": 100, "cost_usd": 0.25},
            },
            {
                "stage": "IMPLEMENT",
                "agent_type": "codex",
                "effective_model": "gpt-5.6-terra",
                "success": True,
                "spawned": True,
                "duration_sec": 48.0,
                "usage": None,
            },
        ],
    )

    result = CliRunner().invoke(main, ["protocol", "status", "--run-dir", str(run_dir)])

    assert result.exit_code == 0, result.output
    assert "실행된 stage 2개" in result.output
    assert "60.0초" in result.output
    assert "codex 48.0s" in result.output and "claude 12.0s" in result.output
    assert "1,000" in result.output  # derived token total
    assert "$0.2500" in result.output
    # The partial total must announce that it covers 1 of 2 stages.
    assert "2개 stage 중 1개만 반영" in result.output
    # Per-stage durations show up on the stage lines too.
    assert "48.0s" in result.output


def test_protocol_status_says_so_when_no_worker_reported_usage(tmp_path):
    run_dir = _save_protocol_state(
        tmp_path / "protocol-nousage",
        task_id="TASK-N",
        mode="C",
        status="DONE",
        handoffs=[
            {
                "stage": "REVIEW",
                "agent_type": "antigravity",
                "success": True,
                "spawned": True,
                "duration_sec": 5.0,
            }
        ],
    )

    result = CliRunner().invoke(main, ["protocol", "status", "--run-dir", str(run_dir)])

    assert result.exit_code == 0, result.output
    assert "워커가 보고한 사용량 없음" in result.output
    # No cost or token figure is invented.
    assert "USD" not in result.output


def test_protocol_status_separates_refused_stages_from_run_ones(tmp_path):
    run_dir = _save_protocol_state(
        tmp_path / "protocol-refused",
        task_id="TASK-R",
        mode="C",
        status="BLOCKED",
        handoffs=[
            {
                "stage": "IMPLEMENT",
                "agent_type": "codex",
                "success": True,
                "spawned": True,
                "duration_sec": 9.0,
            },
            {
                "stage": "REVIEW",
                "agent_type": "antigravity",
                "success": False,
                "spawned": False,
                "duration_sec": 0.0,
                "failure_classification": {"category": "rate_limit"},
            },
        ],
    )

    result = CliRunner().invoke(main, ["protocol", "status", "--run-dir", str(run_dir)])

    assert result.exit_code == 0, result.output
    assert "실행된 stage 1개" in result.output
    assert "거부(실행 안 됨) 1개" in result.output
