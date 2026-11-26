"""Receipt subjects must match the decision document in the target commit."""

import json
from pathlib import Path
import subprocess

import pytest

from team_harness.adoption_gate import receipt_valid
from team_harness.adoption_gate import record_receipt
from team_harness.adoption_gate import sha


@pytest.mark.parametrize("repo_name", ["repo", "design"])
def test_directory_and_file_receipt_subjects(tmp_path: Path, repo_name: str) -> None:
    repo = tmp_path / repo_name
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    design = repo / "design"
    design.mkdir()
    decisions = design / "decisions.md"
    decisions.write_text("## TH-D99. New\n")
    git("add", "design/decisions.md")
    git("commit", "-qm", "decision")
    review = tmp_path / "review"
    review.mkdir()
    for name in ("CROSSCHECK.md", "crosscheck.json", "evidence.sha256"):
        (review / name).write_text(name)
    (review / "terra").mkdir()
    (review / "terra" / "VERDICT.md").write_text("PASS")
    digest = sha(decisions.read_bytes())

    def check(path: Path, hashes: dict[str, str], expected: bool) -> None:
        data = {"names": ["terra"], "source": [{"path": str(path), "hashes": hashes}]}
        record_receipt(review, data, True, None)
        subjects = json.loads((review / "adoption.json").read_text())["subjects"]
        assert subjects == ({"design/decisions.md": digest} if expected else {})
        assert receipt_valid(review, repo, "HEAD") is expected

    check(repo, {"design/decisions.md": digest}, True)
    check(design, {"decisions.md": digest}, True)
    check(decisions, {".": digest}, True)
    other = repo / "src"
    other.mkdir()
    check(other, {}, False)
    unrelated = tmp_path / "unrelated" / "design"
    unrelated.mkdir(parents=True)
    record_receipt(
        review,
        {
            "names": ["terra"],
            "source": [
                {"path": str(unrelated), "hashes": {"decisions.md": sha(b"unrelated")}}
            ],
        },
        True,
        None,
    )
    assert not receipt_valid(review, repo, "HEAD")
    decisions.write_text("## TH-D99. Changed\n")
    git("commit", "-qam", "changed")
    record_receipt(
        review,
        {
            "names": ["terra"],
            "source": [{"path": str(design), "hashes": {"decisions.md": digest}}],
        },
        True,
        None,
    )
    assert not receipt_valid(review, repo, "HEAD")
