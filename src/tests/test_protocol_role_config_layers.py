"""The protocol role chain: --set-role > env > config.toml > protocol default.

Before this the protocol layer read roles only from HARNESS_MODE_* variables,
so the config.toml layers CLAUDE.md documents for every other setting had no
say over which agent and model tier a role used.
"""

# pyright: reportMissingParameterType=false

from pathlib import Path

import pytest

from team_harness.config import load_protocol_role_tables
from team_harness.protocol.config import load_protocol_config
from team_harness.protocol.config import PROTOCOL_ROLE_FIELDS
from team_harness.protocol.config import PROTOCOL_ROLE_NAMES
from team_harness.protocol.config import validate_role_tables


def _write_config(directory: Path, body: str) -> Path:
    config_dir = directory / ".team-harness"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_role_tables_are_read_from_a_project_config(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        """
[protocol.roles.mode_c_implement]
agent = "claude"
model = "claude-sonnet-5"

[protocol.roles.mode_c_review]
effort = "medium"
""",
    )
    monkeypatch.setattr("team_harness.config.CONFIG_PATH", tmp_path / "absent.toml")

    tables = load_protocol_role_tables(tmp_path)

    assert tables == {
        "mode_c_implement": {"agent": "claude", "model": "claude-sonnet-5"},
        "mode_c_review": {"effort": "medium"},
    }


def test_project_config_overrides_the_global_one_per_field(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    global_path = global_dir / "config.toml"
    global_path.write_text(
        """
[protocol.roles.mode_c_implement]
agent = "codex"
model = "gpt-5.6-sol"
effort = "high"
""",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    _write_config(
        project,
        """
[protocol.roles.mode_c_implement]
model = "gpt-5.6-terra"
""",
    )
    monkeypatch.setattr("team_harness.config.CONFIG_PATH", global_path)

    tables = load_protocol_role_tables(project)

    # Only `model` is overridden; the sibling fields survive the deep merge.
    assert tables["mode_c_implement"] == {
        "agent": "codex",
        "model": "gpt-5.6-terra",
        "effort": "high",
    }


def test_config_toml_beats_the_protocol_default():
    proto_cfg = load_protocol_config(
        env={},
        role_tables={"mode_c_design": {"agent": "codex", "model": "gpt-5.6-terra"}},
    )

    assert proto_cfg.mode_c_design.agent_type == "codex"
    assert proto_cfg.mode_c_design.model == "gpt-5.6-terra"
    # Untouched roles keep their cheap-tier defaults (TH-D17).
    assert proto_cfg.mode_c_implement.model == "gpt-5.6-terra"
    assert proto_cfg.mode_c_implement.effort == "high"


def test_env_beats_config_toml():
    proto_cfg = load_protocol_config(
        env={
            "HARNESS_MODE_C_IMPLEMENT_MODEL": "from-env",
            "HARNESS_MODE_C_IMPLEMENT_AGENT": "claude",
        },
        role_tables={
            "mode_c_implement": {
                "agent": "antigravity",
                "model": "from-toml",
                "effort": "from-toml-effort",
            }
        },
    )

    assert proto_cfg.mode_c_implement.agent_type == "claude"
    assert proto_cfg.mode_c_implement.model == "from-env"
    # effort had no env var, so the config.toml layer still supplies it.
    assert proto_cfg.mode_c_implement.effort == "from-toml-effort"


def test_runtime_roles_beat_env_and_config_toml():
    """A --set-role flag typed for one run outranks a stale shell variable."""

    proto_cfg = load_protocol_config(
        env={
            "HARNESS_MODE_C_IMPLEMENT_MODEL": "from-env",
            "HARNESS_MODE_C_IMPLEMENT_AGENT": "antigravity",
            "HARNESS_MODE_C_IMPLEMENT_EFFORT": "from-env-effort",
        },
        role_tables={"mode_c_implement": {"model": "from-toml"}},
        runtime_roles={
            "mode_c_implement": {
                "agent": "claude",
                "model": "from-flag",
                "effort": "from-flag-effort",
            }
        },
    )

    assert proto_cfg.mode_c_implement.agent_type == "claude"
    assert proto_cfg.mode_c_implement.model == "from-flag"
    assert proto_cfg.mode_c_implement.effort == "from-flag-effort"


def test_explicit_runtime_argument_still_outranks_runtime_roles():
    proto_cfg = load_protocol_config(
        env={},
        mode_c_implement_model="from-explicit-kwarg",
        runtime_roles={"mode_c_implement": {"model": "from-flag"}},
    )

    assert proto_cfg.mode_c_implement.model == "from-explicit-kwarg"


@pytest.mark.parametrize("role", PROTOCOL_ROLE_NAMES)
def test_every_role_honors_the_config_toml_layer(role):
    """No role may be left out of the chain — a missed one silently keeps its
    default, which is the failure mode TH-D17 was written about."""

    proto_cfg = load_protocol_config(
        env={}, role_tables={role: {"agent": "claude", "model": "layered"}}
    )

    assert getattr(proto_cfg, role).agent_type == "claude"
    assert getattr(proto_cfg, role).model == "layered"


def test_unknown_role_or_field_fails_loudly():
    with pytest.raises(ValueError, match="Unknown protocol role 'mode_c_implemnt'"):
        validate_role_tables({"mode_c_implemnt": {"model": "x"}})
    with pytest.raises(ValueError, match="Unknown field 'modle'"):
        validate_role_tables({"mode_c_implement": {"modle": "x"}})
    with pytest.raises(ValueError, match="Unknown protocol role"):
        load_protocol_config(env={}, role_tables={"nope": {"model": "x"}})


def test_role_and_field_registries_match_the_config_shape():
    proto_cfg = load_protocol_config(env={}, role_tables={})

    for role in PROTOCOL_ROLE_NAMES:
        assert hasattr(proto_cfg, role), role
    assert PROTOCOL_ROLE_FIELDS == ("agent", "model", "effort")


def test_blank_and_non_string_table_values_are_ignored(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        """
[protocol.roles.mode_c_implement]
model = "   "
effort = "high"
agent = 7
""",
    )
    monkeypatch.setattr("team_harness.config.CONFIG_PATH", tmp_path / "absent.toml")

    tables = load_protocol_role_tables(tmp_path)

    assert tables == {"mode_c_implement": {"effort": "high"}}
