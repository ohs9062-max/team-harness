"""Limit test snapshots to pytest and its descendants, including status runs."""

from functools import wraps
import os
from pathlib import Path
import sys
from typing import Any

from team_harness import proc_observer


def external_ancestors() -> set[int]:
    result: set[int] = set()
    pid = os.getppid()
    while pid > 1 and pid not in result:
        result.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rfind(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return result


def bounded_observer(excluded: set[int]) -> Any:
    original = proc_observer.observe

    @wraps(original)
    def observe(proc_entries: list[Path] | None = None, shared: Any = None) -> Any:
        entries = (
            proc_entries if proc_entries is not None else list(Path("/proc").iterdir())
        )
        return original(
            [p for p in entries if not p.name.isdigit() or int(p.name) not in excluded],
            shared,
        )

    return observe


if __name__ == "__main__":
    from team_harness import status

    proc_observer.observe = bounded_observer(
        {int(pid) for pid in sys.argv[1].split(",") if pid}
    )
    raise SystemExit(
        status.main(
            [str(Path(status.__file__).with_name("worker_digest.py")), sys.argv[2], "0"]
        )
    )
