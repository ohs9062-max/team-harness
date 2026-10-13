"""Protocol configuration layer for role-to-agent and model mapping."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import os
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from team_harness.protocol.models import normalize_agent_type

if TYPE_CHECKING:
    from team_harness.config import Config


@dataclass(frozen=True)
class ProtocolAgentSpec:
    """Specification of an agent backend and optional model for a protocol role."""

    agent_type: str
    model: str | None = None
    effort: str | None = None

    def __post_init__(self) -> None:
        raw_model = self.model
        if raw_model is not None and not raw_model.strip():
            object.__setattr__(self, "model", None)
        elif raw_model is not None:
            object.__setattr__(self, "model", raw_model.strip())

        raw_effort = self.effort
        if raw_effort is not None and not raw_effort.strip():
            object.__setattr__(self, "effort", None)
        elif raw_effort is not None:
            object.__setattr__(self, "effort", raw_effort.strip())

        raw_agent = self.agent_type.strip() if self.agent_type else ""
        if not raw_agent:
            raise ValueError("agent_type must not be empty")
        resolved = normalize_agent_type(raw_agent)
        object.__setattr__(self, "agent_type", resolved)


@dataclass(frozen=True)
class ProtocolConfig:
    """Configuration mapping protocol roles to agent specifications across MODE A/B/C."""

    # Defaults below deliberately pick the cheap/simple tier (TH-D17): Protocol
    # has no per-task complexity judgment of its own, so an unset model must
    # not silently fall through to each worker CLI's own default — for codex
    # that default is gpt-5.6-sol, the *expensive* tier. A caller who knows a
    # task is complex overrides via HARNESS_MODE_*_MODEL/_EFFORT or a runtime
    # ProtocolAgentSpec; the failure mode of an unconsidered default should be
    # "a bit weak on a hard task", not "burned quota on a trivial one".
    mode_a_worker_1: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(
            agent_type="codex", model="gpt-5.6-terra", effort="high"
        )
    )
    mode_a_worker_2: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(
            agent_type="antigravity", model="Gemini 3.8 Flash (High)"
        )
    )
    mode_a_final: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(
            agent_type="codex", model="gpt-5.6-terra", effort="high"
        )
    )
    mode_b_default: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(
            agent_type="codex", model="gpt-5.6-terra", effort="high"
        )
    )
    mode_c_design: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(
            agent_type="claude", model="claude-sonnet-5"
        )
    )
    mode_c_implement: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(
            agent_type="codex", model="gpt-5.6-terra", effort="high"
        )
    )
    mode_c_review: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(
            agent_type="antigravity", model="Gemini 3.8 Flash (High)"
        )
    )


# Every role the Harness Protocol maps onto an agent backend. Used to reject a
# misspelled role in config.toml or `--set-role` instead of silently ignoring
# it — a typo that quietly leaves the expensive default in place is exactly the
# failure TH-D17 was about.
PROTOCOL_ROLE_NAMES: tuple[str, ...] = (
    "mode_a_worker_1",
    "mode_a_worker_2",
    "mode_a_final",
    "mode_b_default",
    "mode_c_design",
    "mode_c_implement",
    "mode_c_review",
)

PROTOCOL_ROLE_FIELDS: tuple[str, ...] = ("agent", "model", "effort")


def validate_role_tables(tables: dict[str, dict[str, str]]) -> None:
    """Reject unknown role or field names before they are silently ignored."""
    for role_name, fields in tables.items():
        if role_name not in PROTOCOL_ROLE_NAMES:
            raise ValueError(
                f"Unknown protocol role {role_name!r}. "
                f"Known roles: {list(PROTOCOL_ROLE_NAMES)}"
            )
        for key in fields:
            if key not in PROTOCOL_ROLE_FIELDS:
                raise ValueError(
                    f"Unknown field {key!r} for protocol role {role_name!r}. "
                    f"Known fields: {list(PROTOCOL_ROLE_FIELDS)}"
                )


def _resolve_role_spec(
    runtime_arg: ProtocolAgentSpec | str | None,
    runtime_model: str | None,
    env_map: dict[str, str],
    agent_var: str,
    model_var: str,
    default_agent: str,
    default_model: str | None = None,
    *,
    runtime_effort: str | None = None,
    effort_var: str | None = None,
    default_effort: str | None = None,
    runtime_role: dict[str, str] | None = None,
    toml_role: dict[str, str] | None = None,
) -> ProtocolAgentSpec:
    """Resolve a single role's spec adhering to the precedence hierarchy.

    Precedence (agent, model and effort resolved independently, same order):
        1. Explicit runtime argument, then *runtime_role* (one `--set-role`
           style override table for this role)
        2. Environment variables (.env / os.environ)
        3. `[protocol.roles.<role>]` in local then global config.toml
        4. Protocol default

    Layer 3 mirrors the project-wide chain documented in CLAUDE.md, which the
    protocol layer previously skipped: roles were settable only by environment
    variable, so the config.toml a user already keeps their model-tier policy in
    had no say over them. *runtime_role* sits in layer 1 because a flag typed
    for one run must beat a stale variable left in the shell.
    """

    runtime_table = runtime_role or {}
    role_table = toml_role or {}

    def _resolve_value(
        runtime_value: str | None,
        spec_value: str | None,
        env_var: str | None,
        default_value: str | None,
        toml_key: str,
    ) -> str | None:
        if runtime_value is not None:
            return runtime_value
        if spec_value is not None:
            return spec_value
        runtime_val = runtime_table.get(toml_key)
        if runtime_val:
            return runtime_val
        env_val = env_map.get(env_var) if env_var else None
        if env_val and env_val.strip():
            return env_val.strip()
        toml_val = role_table.get(toml_key)
        if toml_val:
            return toml_val
        return default_value

    if isinstance(runtime_arg, ProtocolAgentSpec):
        agent_type = runtime_arg.agent_type
        model = _resolve_value(
            runtime_model, runtime_arg.model, model_var, default_model, "model"
        )
        effort = _resolve_value(
            runtime_effort, runtime_arg.effort, effort_var, default_effort, "effort"
        )
        return ProtocolAgentSpec(agent_type=agent_type, model=model, effort=effort)
    elif isinstance(runtime_arg, str):
        agent_type = runtime_arg
        model = _resolve_value(runtime_model, None, model_var, default_model, "model")
        effort = _resolve_value(
            runtime_effort, None, effort_var, default_effort, "effort"
        )
        return ProtocolAgentSpec(agent_type=agent_type, model=model, effort=effort)

    agent_type = (
        _resolve_value(None, None, agent_var, default_agent, "agent") or default_agent
    )
    model = _resolve_value(runtime_model, None, model_var, default_model, "model")
    effort = _resolve_value(runtime_effort, None, effort_var, default_effort, "effort")

    return ProtocolAgentSpec(agent_type=agent_type, model=model, effort=effort)


def load_protocol_config(
    *,
    env_file: str | Path | None = None,
    env: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    mode_a_worker_1: ProtocolAgentSpec | str | None = None,
    mode_a_worker_1_model: str | None = None,
    mode_a_worker_1_effort: str | None = None,
    mode_a_worker_2: ProtocolAgentSpec | str | None = None,
    mode_a_worker_2_model: str | None = None,
    mode_a_worker_2_effort: str | None = None,
    mode_a_final: ProtocolAgentSpec | str | None = None,
    mode_a_final_model: str | None = None,
    mode_a_final_effort: str | None = None,
    mode_b_default: ProtocolAgentSpec | str | None = None,
    mode_b_default_model: str | None = None,
    mode_b_default_effort: str | None = None,
    mode_c_design: ProtocolAgentSpec | str | None = None,
    mode_c_design_model: str | None = None,
    mode_c_design_effort: str | None = None,
    mode_c_implement: ProtocolAgentSpec | str | None = None,
    mode_c_implement_model: str | None = None,
    mode_c_implement_effort: str | None = None,
    mode_c_review: ProtocolAgentSpec | str | None = None,
    mode_c_review_model: str | None = None,
    mode_c_review_effort: str | None = None,
    role_tables: dict[str, dict[str, str]] | None = None,
    runtime_roles: dict[str, dict[str, str]] | None = None,
    config_start_dir: str | Path | None = None,
) -> ProtocolConfig:
    """Load protocol role configuration with full precedence resolution.

    Precedence:
        1. Explicit runtime arguments (e.g. mode_c_design=ProtocolAgentSpec(...)
           or string name), then *runtime_roles*
        2. Environment variables (os.environ takes precedence over .env file)
        3. `[protocol.roles.<role>]` tables in local then global config.toml
        4. Protocol default specifications

    *runtime_roles* carries per-run overrides as ``{role: {field: value}}``,
    which is how the CLI passes ``--set-role`` without needing one keyword
    argument per role-and-field pair.

    Pass *role_tables* to supply layer 3 directly (tests, embedded callers).
    When it is None the tables are read from disk, rooted at *config_start_dir*
    or the current directory, the same way the .env lookup already works.
    """

    if role_tables is None:
        from team_harness.config import load_protocol_role_tables

        role_tables = load_protocol_role_tables(
            Path(config_start_dir) if config_start_dir else None
        )
    validate_role_tables(role_tables)
    runtime_roles = runtime_roles or {}
    validate_role_tables(runtime_roles)
    env_source = env if env is not None else environ
    if env_source is not None:
        merged_env: dict[str, str] = dict(env_source)
    else:
        merged_env = {}
        target_file = Path(env_file) if env_file else Path(".env")
        if target_file.exists():
            file_vals = dotenv_values(target_file)
            for k, v in file_vals.items():
                if v is not None:
                    merged_env[k] = v
        for k, v in os.environ.items():
            merged_env[k] = v

    spec_a_w1 = _resolve_role_spec(
        mode_a_worker_1,
        mode_a_worker_1_model,
        merged_env,
        "HARNESS_MODE_A_WORKER_1_AGENT",
        "HARNESS_MODE_A_WORKER_1_MODEL",
        "codex",
        "gpt-5.6-terra",
        runtime_effort=mode_a_worker_1_effort,
        effort_var="HARNESS_MODE_A_WORKER_1_EFFORT",
        default_effort="high",
        runtime_role=runtime_roles.get("mode_a_worker_1"),
        toml_role=role_tables.get("mode_a_worker_1"),
    )
    spec_a_w2 = _resolve_role_spec(
        mode_a_worker_2,
        mode_a_worker_2_model,
        merged_env,
        "HARNESS_MODE_A_WORKER_2_AGENT",
        "HARNESS_MODE_A_WORKER_2_MODEL",
        "antigravity",
        "Gemini 3.8 Flash (High)",
        runtime_effort=mode_a_worker_2_effort,
        effort_var="HARNESS_MODE_A_WORKER_2_EFFORT",
        runtime_role=runtime_roles.get("mode_a_worker_2"),
        toml_role=role_tables.get("mode_a_worker_2"),
    )
    spec_a_final = _resolve_role_spec(
        mode_a_final,
        mode_a_final_model,
        merged_env,
        "HARNESS_MODE_A_FINAL_AGENT",
        "HARNESS_MODE_A_FINAL_MODEL",
        "codex",
        "gpt-5.6-terra",
        runtime_effort=mode_a_final_effort,
        effort_var="HARNESS_MODE_A_FINAL_EFFORT",
        default_effort="high",
        runtime_role=runtime_roles.get("mode_a_final"),
        toml_role=role_tables.get("mode_a_final"),
    )

    spec_b_def = _resolve_role_spec(
        mode_b_default,
        mode_b_default_model,
        merged_env,
        "HARNESS_MODE_B_DEFAULT_AGENT",
        "HARNESS_MODE_B_DEFAULT_MODEL",
        "codex",
        "gpt-5.6-terra",
        runtime_effort=mode_b_default_effort,
        effort_var="HARNESS_MODE_B_DEFAULT_EFFORT",
        default_effort="high",
        runtime_role=runtime_roles.get("mode_b_default"),
        toml_role=role_tables.get("mode_b_default"),
    )

    spec_c_design = _resolve_role_spec(
        mode_c_design,
        mode_c_design_model,
        merged_env,
        "HARNESS_MODE_C_DESIGN_AGENT",
        "HARNESS_MODE_C_DESIGN_MODEL",
        "claude",
        "claude-sonnet-5",
        runtime_effort=mode_c_design_effort,
        effort_var="HARNESS_MODE_C_DESIGN_EFFORT",
        runtime_role=runtime_roles.get("mode_c_design"),
        toml_role=role_tables.get("mode_c_design"),
    )
    spec_c_impl = _resolve_role_spec(
        mode_c_implement,
        mode_c_implement_model,
        merged_env,
        "HARNESS_MODE_C_IMPLEMENT_AGENT",
        "HARNESS_MODE_C_IMPLEMENT_MODEL",
        "codex",
        "gpt-5.6-terra",
        runtime_effort=mode_c_implement_effort,
        effort_var="HARNESS_MODE_C_IMPLEMENT_EFFORT",
        default_effort="high",
        runtime_role=runtime_roles.get("mode_c_implement"),
        toml_role=role_tables.get("mode_c_implement"),
    )
    spec_c_rev = _resolve_role_spec(
        mode_c_review,
        mode_c_review_model,
        merged_env,
        "HARNESS_MODE_C_REVIEW_AGENT",
        "HARNESS_MODE_C_REVIEW_MODEL",
        "antigravity",
        "Gemini 3.8 Flash (High)",
        runtime_effort=mode_c_review_effort,
        effort_var="HARNESS_MODE_C_REVIEW_EFFORT",
        runtime_role=runtime_roles.get("mode_c_review"),
        toml_role=role_tables.get("mode_c_review"),
    )

    return ProtocolConfig(
        mode_a_worker_1=spec_a_w1,
        mode_a_worker_2=spec_a_w2,
        mode_a_final=spec_a_final,
        mode_b_default=spec_b_def,
        mode_c_design=spec_c_design,
        mode_c_implement=spec_c_impl,
        mode_c_review=spec_c_rev,
    )


def validate_protocol_config(proto_cfg: ProtocolConfig, config: Config) -> None:
    """Validate that all configured agents in proto_cfg are registered in config."""
    from team_harness.agents.registry import resolve_template

    for spec in [
        proto_cfg.mode_a_worker_1,
        proto_cfg.mode_a_worker_2,
        proto_cfg.mode_a_final,
        proto_cfg.mode_b_default,
        proto_cfg.mode_c_design,
        proto_cfg.mode_c_implement,
        proto_cfg.mode_c_review,
    ]:
        resolve_template(agent_type=spec.agent_type, config=config)
