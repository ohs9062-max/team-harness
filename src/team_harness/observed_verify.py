"""Run verification of an observed direct execution outside maintenance."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from team_harness.internal_python import command


def _message(task: Path, message: str) -> str:
    if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d \| [^|\n]+ \| [^|\n]+ \|", message):
        return message
    folder = str(task).replace("|", " ").replace("\n", " ") or "관찰 작업"
    return f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | {folder} | 관찰 종료 | {message}"


def launch(task: Path, ended_at: str, owner: str, route: str, message: str) -> bool:
    from team_harness.observation_events import _append

    message = _message(task, message)
    if os.environ.get("TH_OBSERVED_VERIFY") == "0":
        _append(owner, route, f"{message} | 검수 판정 보류 (결과 1): 검수 없음 | -")
        return True

    try:
        brief = (task / "task.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        _append(owner, route, f"{message} | 검수 판정 보류 (결과 1): 판독 불가 | -")
        return True
    match = re.search(
        r"(?ms)^##\s+검수[^\n]*\n(?:(?!^##\s).)*?^```bash[^\n]*\n(.*?)^```", brief
    )
    if not match or not match.group(1).strip():
        _append(owner, route, f"{message} | 검수 판정 보류 (결과 1): 검수 없음 | -")
        return True
    # The child owns all potentially slow work, including lock waits and digest.
    from team_harness.worker_digest import timeout_setting

    limit = (
        timeout_setting("TH_VERIFY_TIMEOUT") + timeout_setting("TH_DIGEST_TIMEOUT") + 15
    )
    subprocess.Popen(
        [
            "timeout",
            "--kill-after=2",
            str(limit),
            *command(
                "-m",
                "team_harness.observed_verify",
                str(task),
                ended_at,
                owner,
                route,
                message,
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return True


def run(task: Path, ended_at: str, owner: str, route: str, message: str) -> None:
    from team_harness.observation_events import _append
    from team_harness.worker_digest import digest
    from team_harness.worker_digest import verify

    message = _message(task, message)
    verdict = "판정 보류 (결과 1)"
    detail = ""
    try:
        brief = (task / "task.md").read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^##\s+검수[^\n]*\n(?:(?!^##\s).)*?^```bash[^\n]*\n(.*?)^```", brief
        )
        if not match or not match.group(1).strip():
            verdict = "판정 보류 (결과 1): 검수 없음"
        else:
            result_path = task / "verify_result.json"
            fresh = (
                result_path.is_file()
                and result_path.stat().st_mtime
                >= datetime.fromisoformat(ended_at).timestamp()
            )
            marker = task / "VERIFIED"
            marker_fresh = (
                marker.is_file()
                and marker.stat().st_mtime
                >= datetime.fromisoformat(ended_at).timestamp()
            )
            if fresh:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                text = (
                    (task / "verify_result.txt").read_text(encoding="utf-8")
                    if (task / "verify_result.txt").exists()
                    else ""
                )
                verification = (result, text)
            elif marker_fresh:
                text = marker.read_text(encoding="utf-8")
                result = {
                    "task_dir": str(task),
                    "timestamp": datetime.now().strftime("%F %T"),
                    "overall": "PASS",
                    "items": {},
                }
                result_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                verification = (result, text)
            else:
                (task / "verify.sh").write_text(match.group(1), encoding="utf-8")
                verification = verify(task, force=True)
            overall = verification[0].get("overall")
            if overall == "PASS":
                verdict = "PASS (결과 2)"
            elif overall == "FAIL":
                verdict = "FAIL (결과 0)"
            else:
                verdict = "판정 보류 (결과 1)"
            if overall == "FAIL":
                detail = (
                    verification[0].get("items", {}).get("verify", {}).get("reason", "")
                )
            if (task / "REPORT.md").is_file():
                try:
                    digest(task, "observed", ended_at, verification_result=verification)
                except Exception:
                    pass
    except Exception:
        verdict = "판정 보류 (결과 1): 판독 불가"
    _append(owner, route, f"{message} | 검수 {verdict} {detail} | -")
    try:
        from team_harness.state_index import update

        update(extra_tasks={task})
    except Exception:
        pass


if __name__ == "__main__":
    run(Path(sys.argv[1]), *sys.argv[2:])
