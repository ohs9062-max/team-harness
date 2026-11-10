"""Validate a per-decision adoption receipt without loading worker dependencies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def receipt_valid(folder: Path, repo: Path, revision: str) -> bool:
    """Bind approval to the reviewed document and the latest decision record."""
    try:
        receipt = json.loads((folder / "adoption.json").read_text(encoding="utf-8"))
        if receipt.get("schema") != 1 or receipt.get("adopted") is not True:
            return False
        for name, expected in receipt["records"].items():
            if Path(name).name != name or sha((folder / name).read_bytes()) != expected:
                return False
        if not {"CROSSCHECK.md", "crosscheck.json", "evidence.sha256"}.issubset(
            receipt["records"]
        ):
            return False
        for name, expected in receipt["verdicts"].items():
            if Path(name).name != name:
                return False
            if sha((folder / name / "VERDICT.md").read_bytes()) != expected:
                return False
        if not receipt["verdicts"]:
            return False
        content = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"{revision}:design/decisions.md"],
            stderr=subprocess.PIPE,
        )
        return receipt["subjects"].get("design/decisions.md") == sha(content)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ):
        return False


def record_receipt(
    folder: Path, data: dict, adopted: bool, override: str | None
) -> None:
    """Only bind source documents included in the frozen review evidence."""
    subjects = {}
    for source in data["source"]:
        path = Path(source["path"])
        rel = "design/decisions.md" if path.is_dir() else "."
        if path.is_file() and path.name != "decisions.md":
            continue
        expected = source["hashes"].get(rel)
        if expected:
            subjects["design/decisions.md"] = expected
    records = {
        name: sha((folder / name).read_bytes())
        for name in ("CROSSCHECK.md", "crosscheck.json", "evidence.sha256")
    }
    verdicts = {
        name: sha((folder / name / "VERDICT.md").read_bytes())
        for name in data.get("names", ("sol", "argon"))
        if (folder / name / "VERDICT.md").is_file()
    }
    receipt = {
        "schema": 1,
        "adopted": adopted,
        "override_reason": override,
        "subjects": subjects,
        "records": records,
        "verdicts": verdicts,
    }
    temporary = folder / ".adoption.json.tmp"
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(folder / "adoption.json")


if __name__ == "__main__":
    raise SystemExit(
        0 if receipt_valid(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]) else 1
    )
