"""Best effort, bounded context additions for the journal hook."""

from __future__ import annotations

import os
import re
import signal
import sys

from team_harness import state_recall


def main() -> None:
    signal.alarm(2)
    if os.environ.get("TH_AUTO_RECALL") == "0":
        if sys.argv[1] == "events":
            print(sys.stdin.read(), end="")
        return
    if sys.argv[1] == "recent":
        try:
            sys.argv = [sys.argv[0], "recent", sys.argv[2]]
            state_recall.main()
        except Exception:
            pass
        return
    data = sys.stdin.read()
    result: list[str] = []
    count = 0
    additions = 0
    try:
        with state_recall.connect() as db:
            tasks = [row[0] for row in db.execute("SELECT task FROM tasks")]
        for line in data.splitlines():
            result.append(line)
            if count >= 2 or not re.search(
                r"실패|워커 중단|검증 실패|채택 검증\s*FAIL", line, re.I
            ):
                continue
            task = next(
                (
                    task
                    for task in tasks
                    if task in line or os.path.basename(task) in line
                ),
                None,
            )
            if task:
                extra = state_recall.why(task, 12).splitlines()[: 40 - additions]
                result.extend(extra)
                additions += len(extra)
                count += 1
        print("\n".join(result), end="\n" if data.endswith("\n") else "")
    except Exception:
        print(data, end="")


if __name__ == "__main__":
    main()
