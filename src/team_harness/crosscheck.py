"""Independent, conservative claim cross-checks using existing worker launches."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

if __package__:
    from .coordinator_zone import is_coordinator_zone
else:
    import coordinator_zone as cz  # pyright: ignore[reportMissingImports]

    is_coordinator_zone = cz.is_coordinator_zone

ROOT = Path(__file__).resolve().parents[2]
NAMES = ("sol", "argon")
VALID = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")


def now() -> datetime:
    return datetime.now(timezone.utc)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def files(path: Path) -> list[Path]:
    if path.is_symlink():
        raise ValueError(f"심볼릭 링크는 사용할 수 없습니다: {path}")
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError(f"파일 또는 폴더가 없습니다: {path}")
    result = sorted(path.rglob("*"))
    if any(p.is_symlink() for p in result):
        raise ValueError(f"심볼릭 링크는 사용할 수 없습니다: {path}")
    return [p for p in result if p.is_file()]


def snapshot(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path) if path.is_dir() else Path(".")): digest(p)
        for p in files(path)
    }


def git_state(repo: str) -> dict[str, str]:
    def run(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", repo, *args], stderr=subprocess.PIPE
        )

    return {
        "head": run("rev-parse", "HEAD").decode().strip(),
        "status_sha256": hashlib.sha256(run("status", "--porcelain")).hexdigest(),
    }


def manifest_path(folder: Path) -> Path:
    return folder / "crosscheck.json"


def load(folder: Path) -> dict:
    return json.loads(manifest_path(folder).read_text(encoding="utf-8"))


def copy_input(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def make_brief(folder: Path, data: dict, name: str) -> str:
    own = folder / name
    evidence = "\n".join(f"- evidence/{item['copy']}" for item in data["evidence"])
    sources = "\n".join(
        f"- {('source/' + item['copy']) if item['copy'] else item['path']}"
        for item in data["source"]
    )
    return f"""---
kind: research
task_key: crosscheck_{name}
follow_up: short
allow_paths:
  - {own / "VERDICT.md"}
  - {own / "STATUS"}
  - {own / "WORKLOG.md"}
  - {own / "DONE"}
  - {own / "FAILED"}
---
# 결론 반증 검토
너의 역할은 결론을 지지하는 것이 아니라 틀렸을 가능성을 찾는 것이다. 코드를 고치지 않는다. 작업 트리 전체와 이 작업 폴더 쓰기 허용은 기본 범위이나 이 지시서는 VERDICT.md와 작업 기록만 쓰도록 제한한다.

## 읽을 문서
- 아래 증거 전부와 필요시 명시된 원본. 다른 작업 폴더는 읽지 않는다.
{evidence}

## 검토할 결론
{data["claim"]}

## 성공 기준
{data["criteria"]}

## 원본 위치
{sources}

## 검토 방법
원본에서 직접 반례, 논리 오류, 누락 조건과 인과관계를 조사하고 증거 선별 누락 여부를 살펴라. 수치는 직접 계산하라. 반례가 없다는 이유만으로 PASS하지 않는다.

## 기대 산출물
- `VERDICT.md`: 근거로 뒷받침되는 사실, 근거가 부족한 주장, 실제로 발견한 반례, 추가 확인이 필요한 항목, 증거 선별 누락 여부 순으로 작성한다. 마지막 비어 있지 않은 줄은 정확히 VERDICT: PASS, VERDICT: FAIL, VERDICT: INSUFFICIENT_EVIDENCE 중 하나다.

## 검수
```bash
test -s VERDICT.md
```
"""


def start(args: argparse.Namespace) -> int:
    folder = Path(args.folder).expanduser().resolve()
    if folder.exists() and any(folder.iterdir()):
        raise ValueError(f"대상 폴더가 비어 있지 않습니다: {folder}")
    evidence = [Path(x).expanduser().resolve() for x in args.evidence]
    sources = [Path(x).expanduser().resolve() for x in args.source]
    claim = Path(args.claim).read_text(encoding="utf-8")
    criteria = Path(args.criteria).read_text(encoding="utf-8")
    ev = [
        {"path": str(p), "copy": f"{i:03d}-{p.name}", "hashes": snapshot(p)}
        for i, p in enumerate(evidence)
    ]
    source_data = []
    for i, p in enumerate(sources):
        hashes = snapshot(p)
        restricted = any(
            is_coordinator_zone(f, workspace=ROOT, output_dir=folder / name)
            for name in NAMES
            for f in files(p)
        )
        source_data.append(
            {
                "path": str(p),
                "copy": f"{i:03d}-{p.name}" if restricted else "",
                "hashes": hashes,
            }
        )
    total = 2 * sum(p.stat().st_size for p in evidence for p in files(p))
    total += 2 * sum(
        f.stat().st_size
        for p, item in zip(sources, source_data, strict=True)
        if item["copy"]
        for f in files(p)
    )
    limit = float(os.environ.get("TH_CROSSCHECK_MAX_MB", "50")) * 1024 * 1024
    if total > limit:
        raise ValueError(f"복사 크기 초과: {total} > {int(limit)} bytes")
    fingerprint = hashlib.sha256(
        json.dumps(
            [claim, [x["hashes"] for x in ev]], ensure_ascii=False, sort_keys=True
        ).encode()
    ).hexdigest()
    if not args.force and folder.parent.exists():
        for existing in folder.parent.iterdir():
            try:
                if (
                    existing != folder
                    and load(existing).get("fingerprint") == fingerprint
                ):
                    raise ValueError(f"동일 결론과 증거의 기존 검증: {existing}")
            except (OSError, json.JSONDecodeError, NotADirectoryError):
                continue
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        "claim": claim,
        "criteria": criteria,
        "evidence": ev,
        "source": source_data,
        "fingerprint": fingerprint,
        "started": now().isoformat(),
        "repo": str(Path(args.repo).resolve()) if args.repo else None,
        "models": {
            "sol": os.environ.get("TH_CROSSCHECK_SOL_MODEL", "gpt-6.1-sol"),
            "argon": os.environ.get(
                "TH_CROSSCHECK_ARGON_MODEL", "gemini-4-argon-medium"
            ),
        },
    }
    for name in NAMES:
        own = folder / name
        own.mkdir()
        for p, item in zip(evidence, ev, strict=True):
            copy_input(p, own / "evidence" / item["copy"])
        for p, item in zip(sources, source_data, strict=True):
            if item["copy"]:
                copy_input(p, own / "source" / item["copy"])
        (own / "task.md").write_text(make_brief(folder, data, name), encoding="utf-8")
    lines = []
    for item in ev:
        for rel, sha in item["hashes"].items():
            lines.append(f"{sha}  {item['copy']}{'/' + rel if rel != '.' else ''}")
    (folder / "evidence.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    data["evidence_manifest_hash"] = digest(folder / "evidence.sha256")
    data["repo_state"] = git_state(data["repo"]) if data["repo"] else None
    manifest_path(folder).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    def launch(name: str) -> None:
        env = os.environ.copy()
        env.pop("TH_BRIEF_OK", None)
        env.pop("TH_IGNORE_BUDGET", None)
        env["TH_CODEX_REASON"] = "independent claim crosscheck"
        env["TH_HIGH_REASON"] = "independent claim crosscheck"
        worker = "codex" if name == "sol" else "agy"
        effort = os.environ.get(f"TH_CROSSCHECK_{name.upper()}_EFFORT", "medium")
        own = folder / name
        try:
            result = subprocess.run(
                [
                    str(ROOT / "scripts/th-launch.sh"),
                    str(own),
                    worker,
                    data["models"][name],
                    effort,
                    str(own),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                (own / "LAUNCH_FAILED").write_text(
                    f"rc={result.returncode}\n{result.stdout}{result.stderr}",
                    encoding="utf-8",
                )
        except OSError as exc:
            (own / "LAUNCH_FAILED").write_text(str(exc), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(launch, NAMES))
    return 0


def changed(path: Path, expected: dict[str, str]) -> bool:
    try:
        return snapshot(path) != expected
    except (OSError, ValueError):
        return True


def alive(path: Path) -> bool:
    try:
        pid = int(path.read_text().strip())
        if pid <= 0:
            return False
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return (
            not stat.exists()
            or stat.read_text().split(") ", 1)[1].startswith("Z") is False
        )
    except (OSError, ValueError, IndexError):
        return False


def evaluate(folder: Path, data: dict) -> tuple[dict[str, tuple[str, str]], bool]:
    common = []
    try:
        if digest(folder / "evidence.sha256") != data["evidence_manifest_hash"]:
            common.append("증거 해시 목록 변경")
    except OSError:
        common.append("증거 해시 목록 없음")
    for item in data["evidence"]:
        if changed(Path(item["path"]), item["hashes"]):
            common.append(f"원본 증거 변경: {item['path']}")
    for item in data["source"]:
        if changed(Path(item["path"]), item["hashes"]):
            common.append(f"원본 변경: {item['path']}")
    if data["repo"]:
        try:
            if git_state(data["repo"]) != data["repo_state"]:
                common.append("저장소 HEAD 또는 작업 트리 상태 변경")
        except subprocess.CalledProcessError:
            common.append("저장소 상태 확인 실패")
    timeout = float(os.environ.get("TH_CROSSCHECK_TIMEOUT_MIN", "30")) * 60
    expired = (
        now() - datetime.fromisoformat(data["started"])
    ).total_seconds() > timeout
    results: dict[str, tuple[str, str]] = {}
    pending = False
    for name in NAMES:
        own = folder / name
        reasons = common.copy()
        for kind in ("evidence", "source"):
            for item in data[kind]:
                if item["copy"] and changed(own / kind / item["copy"], item["hashes"]):
                    reasons.append(f"{kind} 복사본 변경: {item['copy']}")
        if (own / "LAUNCH_FAILED").exists():
            reasons.append("LAUNCH_FAILED")
        if (own / "FAILED").exists():
            reasons.append("FAILED")
        if expired:
            reasons.append("시간 초과")
        done = (own / "DONE").is_file()
        if not done and not alive(own / "worker.pid"):
            reasons.append("DONE 없이 프로세스 종료")
        if reasons:
            results[name] = ("UNAVAILABLE", "; ".join(reasons))
        elif not done:
            results[name] = ("RUNNING", "실행 중")
            pending = True
        else:
            try:
                lines = (own / "VERDICT.md").read_text(encoding="utf-8").splitlines()
                last = next(line for line in reversed(lines) if line.strip())
                match = re.fullmatch(
                    r"VERDICT: (PASS|FAIL|INSUFFICIENT_EVIDENCE)", last
                )
            except (OSError, StopIteration):
                match = None
            results[name] = (
                (match.group(1), "완료")
                if match
                else ("UNAVAILABLE", "마지막 판정 줄이 유효하지 않음")
            )
    return results, pending


def verdict(folder: Path) -> tuple[int, dict[str, tuple[str, str]], dict]:
    data = load(folder)
    results, pending = evaluate(folder, data)
    accepted = all(results[name][0] == "PASS" for name in NAMES)
    rc = 3 if pending else (0 if accepted else 1)
    result_lines = "\n".join(
        f"- {name} ({data['models'][name]}): {results[name][0]} — {results[name][1]}; {folder / name / 'VERDICT.md'}"
        for name in NAMES
    )
    report = f"# 독립 반증 검증\n\n대상: {data['claim'].strip()}\n\n시각: {now().isoformat()}\n\n증거 해시: {data['evidence_manifest_hash']}\n\n{result_lines}\n\n종합: {'실행 중' if pending else '채택 가능' if accepted else '채택 보류'}\n\n코디네이터가 근거를 원본과 대조해야 함. 각 VERDICT.md의 반례와 미해결 항목을 확인한다.\n"
    output = folder / "CROSSCHECK.md"
    # Preserve decisions already appended across repeated checks.
    if output.exists():
        prior = output.read_text(encoding="utf-8")
        marker = "\n## 코디네이터 결정\n"
        if marker in prior:
            report += marker + prior.split(marker, 1)[1]
    output.write_text(report, encoding="utf-8")
    return rc, results, data


def decide(args: argparse.Namespace) -> int:
    folder = Path(args.folder).resolve()
    rc, results, data = verdict(folder)
    if rc == 3:
        raise ValueError("검증 실행 중에는 결정할 수 없습니다")
    if args.adopt and rc != 0 and not (args.override_reason or "").strip():
        raise ValueError("채택 보류를 채택하려면 --override-reason이 필요합니다")
    if not args.reason.strip():
        raise ValueError("결정 이유가 필요합니다")
    details = []
    for name in NAMES:
        path = folder / name / "VERDICT.md"
        text = path.read_text(encoding="utf-8") if path.is_file() else "결과 없음"
        details.append(
            f"### {name}: {results[name][0]} ({data['models'][name]})\n경로: {path}\n\n{text}"
        )
    with (folder / "CROSSCHECK.md").open("a", encoding="utf-8") as f:
        f.write(
            f"\n## 코디네이터 결정\n시각: {now().isoformat()}\n결정: {'채택' if args.adopt else '기각'}\n이유: {args.reason}\n"
        )
        if args.override_reason:
            f.write(f"보류 override 이유: {args.override_reason}\n")
        f.write(
            "\n반례와 미해결 항목 (검증자 보고 원문):\n\n" + "\n\n".join(details) + "\n"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("start")
    p.add_argument("folder")
    p.add_argument("--claim", required=True)
    p.add_argument("--criteria", required=True)
    p.add_argument("--evidence", nargs="+", required=True)
    p.add_argument("--source", nargs="+", required=True)
    p.add_argument("--repo")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("verdict")
    p.add_argument("folder")
    p = sub.add_parser("decide")
    p.add_argument("folder")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--adopt", action="store_true")
    group.add_argument("--reject", action="store_true")
    p.add_argument("--reason", required=True)
    p.add_argument("--override-reason")
    args = parser.parse_args()
    try:
        if args.action == "start":
            return start(args)
        if args.action == "verdict":
            return verdict(Path(args.folder).resolve())[0]
        return decide(args)
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"crosscheck: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
