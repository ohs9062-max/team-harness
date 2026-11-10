"""Reclassify historical verification results without running checks."""

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path

from team_harness.worker_digest import atomic_write
from team_harness.worker_digest import locked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args()
    watch = Path(
        os.environ.get("TH_WATCH_DIR", str(Path.home() / ".team-harness/watch"))
    )
    registries = (
        [args.registry]
        if args.registry
        else [watch / "tasks.txt", watch / "tasks_done.txt"]
    )
    seen: set[Path] = set()
    for registry in registries:
        if not registry.exists():
            if args.registry:
                parser.error(f"registry not found: {registry}")
            continue
        for line in registry.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            task = Path(line.strip())
            if task in seen or not task.is_dir():
                continue
            seen.add(task)
            with nullcontext() if args.dry_run else locked(task / ".th_verify.lock"):
                path = task / "verify_result.json"
                try:
                    result = json.loads(path.read_text(encoding="utf-8"))
                    items = result["items"]
                    checks = [v for k, v in items.items() if k != "상태"]
                    if (
                        result["overall"] != "PASS"
                        or not checks
                        or not all(v["verdict"] == "SKIP" for v in checks)
                    ):
                        continue
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
                print(task, flush=True)
                if not args.dry_run:
                    result["overall"] = "WARN 검증 없음"
                    atomic_write(
                        path, json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                    )
                    old = task / "VERIFIED"
                    content = (
                        old.read_text(encoding="utf-8")
                        if old.is_file()
                        else "검증 없음\n"
                    )
                    for name in ("VERIFIED", "VERIFY_FAILED"):
                        (task / name).unlink(missing_ok=True)
                    atomic_write(task / "VERIFY_NONE", content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
