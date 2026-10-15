# pyright: reportMissingParameterType=false

from pathlib import Path

from team_harness.config import Config
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.coordinator.system_prompt import COORDINATOR_PROMPT
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER
from team_harness.skills.loader import SkillMetadata


def test_system_prompt_contains_coordinator_identity():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex", "gemini"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "You are the coordinator for team-harness." in prompt
    assert "You are not a coding agent." in prompt


def test_system_prompt_contains_values_and_monitoring_guidance():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex", "gemini"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "Clarity:" in prompt
    assert "Pragmatism:" in prompt
    assert "Rigor:" in prompt
    assert "Patience:" in prompt
    assert "`wait_for_any`" in prompt
    assert "Patience Protocol" in prompt
    assert "HARD FLOOR" in prompt
    assert "STDERR GROWTH RULE" in prompt
    assert "Use the todo tools aggressively." in prompt


def test_system_prompt_includes_runtime_context_and_output_directory():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex", "gemini"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "/repo/_outputs/run_123" in prompt
    assert "Artifact review:" in prompt


def test_system_prompt_directs_journal_writes_to_the_run_directory():
    config = Config(cwd="/repo")
    config.run_dir = Path("/runs/run_123")

    prompt = build_system_prompt(
        config=config,
        allowed_types=["codex"],
        skills=[],
        session_output_dir="/artifacts/run_123",
    )

    assert "/runs/run_123/journal/current_status.md" in prompt
    assert "/runs/run_123/journal/work_log.md" in prompt
    assert "/repo/.claude/current_status.md" in prompt
    assert "Never write, edit, append, or replace them." in prompt


def test_system_prompt_excludes_legacy_agent_summary_filenames():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "AGENT_SUMMARY.md" not in prompt
    assert "AGENT_PROGRESS.md" not in prompt


def test_system_prompt_appends_extensions_and_skills():
    prompt = build_system_prompt(
        config=Config(cwd="/repo", system_prompt_extension="Extra config rules"),
        allowed_types=["codex"],
        skills=[
            SkillMetadata(
                name="skill-a",
                description="First skill",
                path=Path("/repo/.agents/skills/skill-a/SKILL.md"),
                skill_dir=Path("/repo/.agents/skills/skill-a"),
            ),
            SkillMetadata(
                name="skill-b",
                description="Second skill",
                path=Path("/repo/.agents/skills/skill-b/SKILL.md"),
                skill_dir=Path("/repo/.agents/skills/skill-b"),
            ),
        ],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "Extra config rules" in prompt
    assert "## Skills" in prompt
    assert "**skill-a**: First skill" in prompt
    assert "**skill-b**: Second skill" in prompt
    assert "To use a skill: read its SKILL.md file" in prompt


def test_build_system_prompt_uses_config_coordinator_prompt():
    config = Config(
        coordinator_prompt=(
            "custom base prompt {allowed_agent_types} {cwd} "
            "{current_utc_time} {session_output_dir}"
        ),
        cwd="/tmp/project",
    )

    prompt = build_system_prompt(config, ["codex"], [], session_output_dir="/tmp/out")

    assert "custom base prompt" in prompt
    assert COORDINATOR_PROMPT not in prompt


def test_build_system_prompt_includes_suffix_note_when_present():
    config = Config(worker_suffix="Follow the repo conventions.", cwd="/tmp/project")

    prompt = build_system_prompt(config, ["codex"], [], session_output_dir="/tmp/out")

    assert (
        "The following suffix is automatically appended to every worker prompt."
        in prompt
    )
    assert "DO NOT duplicate these instructions in spawn_agent prompts." in prompt
    assert "Follow the repo conventions." in prompt


def test_build_system_prompt_omits_suffix_section_when_empty():
    config = Config(worker_suffix="", cwd=".")

    prompt = build_system_prompt(config, ["codex"], [], session_output_dir="/tmp/out")

    assert (
        "The following suffix is automatically appended to every worker prompt."
        not in prompt
    )
    assert "DO NOT duplicate these instructions in spawn_agent prompts." not in prompt


def test_config_defaults_use_builtin_base_prompt():
    config = Config()

    assert config.coordinator_prompt == COORDINATOR_PROMPT
    assert config.worker_suffix == ""
    assert config.worker_footer == DEFAULT_WORKER_FOOTER


def test_default_worker_footer_constant_is_exposed():
    assert "{session_output_dir}" in DEFAULT_WORKER_FOOTER


def test_build_system_prompt_with_default_config_uses_coordinator_prompt():
    config = Config(cwd="/tmp")

    prompt = build_system_prompt(config, ["codex"], [], session_output_dir="/tmp/out")

    assert "You are the coordinator for team-harness." in prompt


def test_system_prompt_contains_api_error_failover_protocol():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex", "claude"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "API Error Failover Protocol" in prompt
    assert "failure_classification" in prompt
    assert "DIFFERENT agent type" in prompt


def test_system_prompt_respawn_prohibition_references_failover():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "Exception: API error failovers" in prompt


def test_build_system_prompt_prefix_invariant_across_runs():
    config = Config(cwd="/repo")
    allowed_types = ["codex", "gemini"]
    skills = []

    prompt1 = build_system_prompt(
        config, allowed_types, skills, session_output_dir="/repo/run_1"
    )
    prompt2 = build_system_prompt(
        config, allowed_types, skills, session_output_dir="/repo/run_2"
    )

    # Split by the trailing Run context section
    prefix1 = prompt1.split("## Run context")[0]
    prefix2 = prompt2.split("## Run context")[0]

    assert prefix1 == prefix2
    assert "## Run context" in prompt1
    assert "## Run context" in prompt2
    assert "/repo/run_1" not in prefix1
    assert "/repo/run_2" not in prefix2


def test_build_system_prompt_handles_unescaped_braces_in_custom_prompt():
    class CustomConfig:
        cwd = "/my/repo"
        coordinator_system_message = (
            'Custom prompt with JSON example: {"action": "spawn", "name": "worker"}. '
            "Working directory is {cwd}."
        )

    prompt = build_system_prompt(
        config=CustomConfig(),
        allowed_types=["codex"],
        skills=[],
        session_output_dir="/repo/run_1",
    )
    assert '{"action": "spawn", "name": "worker"}' in prompt
    assert "Working directory is /my/repo." in prompt


def test_system_prompt_worker_model_selection_in_fixed_prefix():
    config = Config(cwd="/repo")
    prompt = build_system_prompt(
        config=config,
        allowed_types=["codex", "gemini"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "Worker model selection:" in prompt
    assert "## Run context" in prompt

    prefix, _ = prompt.split("## Run context", 1)
    assert "Worker model selection:" in prefix
    assert "gpt-5.6-terra" in prefix
    assert 'effort="medium"' in prefix
    assert 'effort="high"' in prefix
    assert "gpt-6.1-sol" in prefix
    assert "antigravity" in prefix
    assert "spawn_agent" in prefix

    section_chunk = [
        chunk
        for chunk in prefix.split("\n\n")
        if chunk.startswith("Worker model selection:")
    ][0]
    section_lines = [
        line for line in section_chunk.strip().splitlines() if line.strip()
    ]
    assert len(section_lines) <= 6
    assert "gpt-5.6-terra" in section_chunk
    assert (
        'Codex `gpt-5.6-terra` with `effort="medium"` for ordinary implementation and `effort="high"` for complex implementation or debugging.'
        in section_chunk
    )
    assert '`gpt-6.1-sol` with `effort="medium"`' in section_chunk
    assert (
        '`gpt-6.1-sol` with `effort="high"` for hard errors or important design.'
        in section_chunk
    )
