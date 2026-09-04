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

    def __post_init__(self) -> None:
        raw_model = self.model
        if raw_model is not None and not raw_model.strip():
            object.__setattr__(self, "model", None)
        elif raw_model is not None:
            object.__setattr__(self, "model", raw_model.strip())

        raw_agent = self.agent_type.strip() if self.agent_type else ""
        if not raw_agent:
            raise ValueError("agent_type must not be empty")
        resolved = normalize_agent_type(raw_agent)
        object.__setattr__(self, "agent_type", resolved)


@dataclass(frozen=True)
class ProtocolConfig:
    """Configuration mapping protocol roles to agent specifications across MODE A/B/C."""

    mode_a_worker_1: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(agent_type="codex")
    )
    mode_a_worker_2: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(agent_type="antigravity")
    )
    mode_a_final: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(agent_type="codex")
    )
    mode_b_default: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(agent_type="codex")
    )
    mode_c_design: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(agent_type="claude")
    )
    mode_c_implement: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(agent_type="codex")
    )
    mode_c_review: ProtocolAgentSpec = field(
        default_factory=lambda: ProtocolAgentSpec(agent_type="antigravity")
    )


def _resolve_role_spec(
    runtime_arg: ProtocolAgentSpec | str | None,
    runtime_model: str | None,
    env_map: dict[str, str],
    agent_var: str,
    model_var: str,
    default_agent: str,
    default_model: str | None = None,
) -> ProtocolAgentSpec:
    """Resolve a single role's spec adhering to the precedence hierarchy.

    Precedence:
        1. Explicit runtime argument
        2. Environment variables (.env / os.environ)
        3. Protocol default
    """
    if isinstance(runtime_arg, ProtocolAgentSpec):
        agent_type = runtime_arg.agent_type
        model = runtime_model if runtime_model is not None else runtime_arg.model
        return ProtocolAgentSpec(agent_type=agent_type, model=model)
    elif isinstance(runtime_arg, str):
        agent_type = runtime_arg
        if runtime_model is not None:
            model = runtime_model
        else:
            model_val = env_map.get(model_var)
            model = (
                model_val.strip() if model_val and model_val.strip() else default_model
            )
        return ProtocolAgentSpec(agent_type=agent_type, model=model)

    agent_val = env_map.get(agent_var)
    model_val = env_map.get(model_var)

    agent_type = agent_val.strip() if agent_val and agent_val.strip() else default_agent
    if runtime_model is not None:
        model = runtime_model
    else:
        model = model_val.strip() if model_val and model_val.strip() else default_model

    return ProtocolAgentSpec(agent_type=agent_type, model=model)


def load_protocol_config(
    *,
    env_file: str | Path | None = None,
    env: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    mode_a_worker_1: ProtocolAgentSpec | str | None = None,
    mode_a_worker_1_model: str | None = None,
    mode_a_worker_2: ProtocolAgentSpec | str | None = None,
    mode_a_worker_2_model: str | None = None,
    mode_a_final: ProtocolAgentSpec | str | None = None,
    mode_a_final_model: str | None = None,
    mode_b_default: ProtocolAgentSpec | str | None = None,
    mode_b_default_model: str | None = None,
    mode_c_design: ProtocolAgentSpec | str | None = None,
    mode_c_design_model: str | None = None,
    mode_c_implement: ProtocolAgentSpec | str | None = None,
    mode_c_implement_model: str | None = None,
    mode_c_review: ProtocolAgentSpec | str | None = None,
    mode_c_review_model: str | None = None,
) -> ProtocolConfig:
    """Load protocol role configuration with full precedence resolution.

    Precedence:
        1. Explicit runtime arguments (e.g. mode_c_design=ProtocolAgentSpec(...) or string name)
        2. Environment variables (os.environ takes precedence over .env file)
        3. Protocol default specifications
    """
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
    )
    spec_a_w2 = _resolve_role_spec(
        mode_a_worker_2,
        mode_a_worker_2_model,
        merged_env,
        "HARNESS_MODE_A_WORKER_2_AGENT",
        "HARNESS_MODE_A_WORKER_2_MODEL",
        "antigravity",
    )
    spec_a_final = _resolve_role_spec(
        mode_a_final,
        mode_a_final_model,
        merged_env,
        "HARNESS_MODE_A_FINAL_AGENT",
        "HARNESS_MODE_A_FINAL_MODEL",
        "codex",
    )

    spec_b_def = _resolve_role_spec(
        mode_b_default,
        mode_b_default_model,
        merged_env,
        "HARNESS_MODE_B_DEFAULT_AGENT",
        "HARNESS_MODE_B_DEFAULT_MODEL",
        "codex",
    )

    spec_c_design = _resolve_role_spec(
        mode_c_design,
        mode_c_design_model,
        merged_env,
        "HARNESS_MODE_C_DESIGN_AGENT",
        "HARNESS_MODE_C_DESIGN_MODEL",
        "claude",
    )
    spec_c_impl = _resolve_role_spec(
        mode_c_implement,
        mode_c_implement_model,
        merged_env,
        "HARNESS_MODE_C_IMPLEMENT_AGENT",
        "HARNESS_MODE_C_IMPLEMENT_MODEL",
        "codex",
    )
    spec_c_rev = _resolve_role_spec(
        mode_c_review,
        mode_c_review_model,
        merged_env,
        "HARNESS_MODE_C_REVIEW_AGENT",
        "HARNESS_MODE_C_REVIEW_MODEL",
        "antigravity",
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
