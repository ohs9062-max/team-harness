"""Deploy one pushed checkout to global commands, policies and worktrees."""

from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, content: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.deploy.{os.getpid()}")
    try:
        temporary.write_bytes(content)
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def backup(path: Path, home: Path) -> None:
    if path.is_file():
        key = hashlib.sha256(str(path).encode()).hexdigest()[:12]
        target = (
            home
            / ".team-harness/deployment-backups"
            / (f"{datetime.now():%Y%m%dT%H%M%S}-{key}-{digest(path)[:12]}-{path.name}")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)


def install_file(source: Path, target: Path, home: Path) -> None:
    if target.is_file() and source.read_bytes() == target.read_bytes():
        return
    backup(target, home)
    atomic_write(target, source.read_bytes(), source.stat().st_mode & 0o777)


def _coordinator_body(text: str) -> tuple[int, int] | None:
    """Find real table boundaries, ignoring examples inside TOML strings/arrays.

    A header-looking line inside a value has an incomplete TOML prefix. Let
    the standard parser distinguish it instead of inventing a second lexer.
    """
    start = None
    for match in re.finditer(r"(?m)^[ \t]*\[[^\n]*", text):
        try:
            tomllib.loads(text[: match.start()])
            header = tomllib.loads(match.group())
        except tomllib.TOMLDecodeError:
            continue
        if start is not None:
            return start, match.start()
        if header == {"coordinator": {}}:
            start = match.end()
    return (start, len(text)) if start is not None else None


def merge_config(path: Path, home: Path) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else "[coordinator]\n"
    data = tomllib.loads(text)
    coordinator = data.get("coordinator", {})
    if not isinstance(coordinator, dict):
        raise ValueError("coordinator must be a TOML table")
    bounds = _coordinator_body(text)
    if bounds is None:
        text += "\n[coordinator]\n"
        bounds = _coordinator_body(text)
    assert bounds is not None
    start, end = bounds
    section = text[start:end]
    prompt = coordinator.get("system_prompt", "")
    # Migrate only the identifiable legacy managed prompt in THIS table.
    # Never match another table's field or an example inside a quoted value.
    if isinstance(prompt, str) and prompt.lstrip().startswith(
        "## Coordinator policy: minimize tokens"
    ):
        experiment = re.search(r"(?ms)^- Experiment design check.*?(?=^- |\Z)", prompt)
        retained = experiment.group().strip() if experiment else ""
        for match in re.finditer(r"(?m)^[ \t]*system_prompt[ \t]*=[ \t]*", section):
            try:
                tomllib.loads(section[: match.start()])
            except tomllib.TOMLDecodeError:
                continue
            # Find the complete parsed value, not the first quote-like
            # delimiter: escaped triple quotes can occur inside a prompt.
            value_end = None
            for quotes in re.finditer(r"[\"']+", section[match.end() :]):
                candidate = match.end() + quotes.end()
                try:
                    prefix = tomllib.loads(section[:candidate])
                except tomllib.TOMLDecodeError:
                    continue
                if prefix.get("system_prompt") == prompt:
                    value_end = candidate
                    break
            if value_end is None:
                raise ValueError("cannot safely locate the managed prompt value")
            section = (
                section[: match.end()] + json.dumps(retained) + section[value_end:]
            )
            break
    for name, value in (
        ("operating_policy_file", "operating_policy.md"),
        ("worker_suffix_file", "worker_suffix.md"),
    ):
        if name not in coordinator:
            section = section.rstrip() + f'\n{name} = "{value}"\n'
    updated = text[:start] + section + text[end:]
    tomllib.loads(updated)  # Validate before replacing a working configuration.
    if not path.exists() or path.read_text() != updated:
        backup(path, home)
        atomic_write(path, updated.encode(), 0o600)


def worktrees(root: Path) -> list[Path]:
    result = git(root, "worktree", "list", "--porcelain", "-z")
    if result.returncode:
        raise RuntimeError(result.stderr)
    return [
        Path(item.removeprefix("worktree "))
        for item in result.stdout.split("\0")
        if item.startswith("worktree ")
    ]


def sync_worktrees(root: Path, revision: str) -> list[dict[str, str]]:
    results = []
    for path in worktrees(root):
        head = git(path, "rev-parse", "HEAD").stdout.strip()
        if head == revision:
            results.append({"path": str(path), "state": "current"})
            continue
        if not git(path, "merge-base", "--is-ancestor", revision, "HEAD").returncode:
            results.append(
                {
                    "path": str(path),
                    "state": "contains_revision",
                    "reason": "deployed revision present; local commits preserved",
                }
            )
            continue
        if git(path, "merge-base", "--is-ancestor", "HEAD", revision).returncode:
            results.append(
                {
                    "path": str(path),
                    "state": "blocked",
                    "reason": "diverged history; global commands are current",
                }
            )
            continue
        # Git refuses overlapping edits and untracked-file collisions. Never
        # stash, reset, force-checkout or merge a diverged branch automatically.
        result = git(path, "merge", "--ff-only", "--no-edit", revision)
        results.append(
            {
                "path": str(path),
                "state": "updated" if not result.returncode else "blocked",
                "reason": (result.stderr or result.stdout).strip()[-1000:],
            }
        )
    return results


def apply(root: Path = ROOT, home: Path | None = None) -> dict:
    home = home or Path.home()
    directory = home / ".team-harness"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        revision = git(root, "rev-parse", "HEAD").stdout.strip()
        if not revision or git(root, "diff", "--quiet", "HEAD").returncode:
            raise ValueError(
                "commit and validate tracked changes before global deployment"
            )
        if git(
            root, "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main"
        ).returncode:
            raise ValueError(
                "push the candidate to origin/main before global deployment"
            )
        installed = {}
        links = {}
        for relative, target in (
            ("design/designs/operating_policy.md", directory / "operating_policy.md"),
            ("design/user_config/worker_suffix.md", directory / "worker_suffix.md"),
        ):
            install_file(root / relative, target, home)
            installed[str(target)] = digest(target)
        for skill in ("orca-harness-protocol", "team-harness"):
            for client in (".agents", ".claude", ".codex"):
                target = home / client / "skills" / skill / "SKILL.md"
                install_file(root / "skills" / skill / "SKILL.md", target, home)
                installed[str(target)] = digest(target)
        merge_config(directory / "config.toml", home)
        # Include user overrides in the installed-state fingerprint without
        # exposing their contents or copying credentials into the repository.
        installed[str(directory / "config.toml")] = digest(directory / "config.toml")
        bin_dir = home / ".local/bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted((root / "scripts").glob("th-*.sh")):
            for name in (source.name, source.stem):
                target = bin_dir / name
                links[str(target)] = str(source.resolve())
                if target.is_symlink() and target.resolve() == source.resolve():
                    continue
                backup(target, home)
                temporary = target.with_name(f".{name}.deploy.{os.getpid()}")
                temporary.unlink(missing_ok=True)
                temporary.symlink_to(source)
                temporary.replace(target)
        target = bin_dir / "ths"
        temporary = bin_dir / ".ths.deploy"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(root / "scripts/th-status.sh")
        temporary.replace(target)
        links[str(target)] = str((root / "scripts/th-status.sh").resolve())
        python = root / ".venv/bin/python"
        if not python.exists():
            python = Path(sys.executable)
        wrapper = (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            f"export PYTHONPATH={shlex.quote(str(root / 'src'))}"
            "${PYTHONPATH:+:$PYTHONPATH}\n"
            f'exec {shlex.quote(str(python))} -m team_harness "$@"\n'
        )
        backup(bin_dir / "th", home)
        atomic_write(bin_dir / "th", wrapper.encode(), 0o755)
        installed[str(bin_dir / "th")] = digest(bin_dir / "th")
        for relative in (
            ".claude/CLAUDE.md",
            ".codex/AGENTS.md",
            ".claude/settings.json",
            ".codex/hooks.json",
        ):
            backup(home / relative, home)
        for name in (
            "install-journal-rule.sh",
            "install-image-guard.sh",
            "install-coordinator-zone-guard.sh",
        ):
            script = root / "scripts" / name
            if not script.is_file():
                raise ValueError(f"deployment installer missing: {script}")
            environment = dict(
                os.environ,
                HOME=str(home),
                TH_CLAUDE_HOOKS_DIR=str(home / ".claude/hooks"),
                TH_CLAUDE_SETTINGS=str(home / ".claude/settings.json"),
                TH_CODEX_HOOKS=str(home / ".codex/hooks.json"),
                TH_CODEX_HOOKS_BACKUP=str(home / ".codex/hooks.json.before-deployment"),
            )
            setup = subprocess.run(
                ["bash", str(script)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if setup.returncode:
                raise RuntimeError(f"{name}: {setup.stderr.strip()}")
        for path in (home / ".claude/CLAUDE.md", home / ".codex/AGENTS.md"):
            installed[str(path)] = digest(path)
        result = {
            "schema": 1,
            "source": str(root),
            "revision": revision,
            "deployed_at": datetime.now().astimezone().isoformat(),
            "files": installed,
            "links": links,
            "worktrees": sync_worktrees(root, revision),
        }
        atomic_write(
            directory / "deployment.json",
            (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(),
            0o600,
        )
        return result


def status(home: Path | None = None) -> dict:
    home = home or Path.home()
    manifest = home / ".team-harness/deployment.json"
    if not manifest.exists():
        return {"state": "not deployed"}
    data = json.loads(manifest.read_text(encoding="utf-8"))
    drift = [
        name
        for name, expected in data["files"].items()
        if not Path(name).is_file() or digest(Path(name)) != expected
    ]
    drift.extend(
        name
        for name, expected in data.get("links", {}).items()
        if not Path(name).is_symlink() or str(Path(name).resolve()) != expected
    )
    source = Path(data["source"])
    source_dirty = bool(git(source, "diff", "--name-only", "HEAD").stdout)
    data["source_dirty"] = source_dirty
    data["source_head"] = git(source, "rev-parse", "HEAD").stdout.strip()
    data["source_matches_deployment"] = data["source_head"] == data["revision"]
    for row in data.get("worktrees", []):
        path = Path(row["path"])
        current = git(path, "rev-parse", "HEAD")
        row["head"] = current.stdout.strip()
        row["tracked_dirty"] = bool(
            git(path, "status", "--porcelain", "--untracked-files=no").stdout
        )
        row["at_deployed_revision"] = row["head"] == data["revision"]
        row["contains_deployed_revision"] = not git(
            path, "merge-base", "--is-ancestor", data["revision"], "HEAD"
        ).returncode
    data["drift"] = drift
    data["state"] = (
        "drift"
        if drift or source_dirty or not data["source_matches_deployment"]
        else "current"
    )
    return data


def auto_sync() -> None:
    manifest = Path.home() / ".team-harness/deployment.json"
    if not manifest.exists():
        return
    data = json.loads(manifest.read_text(encoding="utf-8"))
    root = Path(data["source"])
    revision = git(root, "rev-parse", "HEAD").stdout.strip()
    if (
        revision != data["revision"]
        and not git(
            root, "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main"
        ).returncode
        and not git(root, "diff", "--quiet", "HEAD").returncode
    ):
        apply(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--home", type=Path)
    args = parser.parse_args()
    try:
        result = apply(home=args.home) if args.apply else status(args.home)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"deployment failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
