from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from team_harness.agents.rate_limits import parse_rate_limited_spawn_result
    from team_harness.caller_contract import CallerContext
    from team_harness.caller_contract import get_capabilities
    from team_harness.caller_contract import HarnessCapabilities
    from team_harness.caller_contract import TEAM_HARNESS_CAPABILITIES
    from team_harness.harness import AgentSummary
    from team_harness.harness import TeamHarness
    from team_harness.harness import TeamHarnessError
    from team_harness.harness import TeamHarnessResult

__all__ = [
    "AgentSummary",
    "CallerContext",
    "HarnessCapabilities",
    "TEAM_HARNESS_CAPABILITIES",
    "TeamHarness",
    "TeamHarnessError",
    "TeamHarnessResult",
    "get_capabilities",
    "parse_rate_limited_spawn_result",
]

_EXPORTS = {
    "parse_rate_limited_spawn_result": (
        "team_harness.agents.rate_limits",
        "parse_rate_limited_spawn_result",
    ),
    "CallerContext": ("team_harness.caller_contract", "CallerContext"),
    "get_capabilities": ("team_harness.caller_contract", "get_capabilities"),
    "HarnessCapabilities": ("team_harness.caller_contract", "HarnessCapabilities"),
    "TEAM_HARNESS_CAPABILITIES": (
        "team_harness.caller_contract",
        "TEAM_HARNESS_CAPABILITIES",
    ),
    "AgentSummary": ("team_harness.harness", "AgentSummary"),
    "TeamHarness": ("team_harness.harness", "TeamHarness"),
    "TeamHarnessError": ("team_harness.harness", "TeamHarnessError"),
    "TeamHarnessResult": ("team_harness.harness", "TeamHarnessResult"),
}


def __getattr__(name: str) -> object:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
