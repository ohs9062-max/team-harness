"""Command for starting harness subprocesses with the shared environment policy."""

from collections.abc import Mapping
import os
from pathlib import Path


def command(*args: str) -> list[str]:
    return [str(Path(__file__).resolve().parents[2] / "scripts/th-env.sh"), *args]


def caller_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Restore the caller environment for external workers and project checks."""
    result = dict(os.environ if env is None else env)
    if result.pop("TH_INTERNAL_ENV_SAVED", None) == "1":
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONNOUSERSITE"):
            present = result.pop(f"TH_INTERNAL_{name}_PRESENT", "")
            value = result.pop(f"TH_INTERNAL_{name}_VALUE", "")
            if present:
                result[name] = value
            else:
                result.pop(name, None)
    return result
