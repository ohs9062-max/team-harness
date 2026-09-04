"""Prompt builders for each Harness Protocol stage.

Each function returns only what the target Worker needs — we never inject
the full MODES.md into every prompt.
"""

from __future__ import annotations


def build_design_prompt(
    *, task_id: str, user_request: str, worktree_path: str, base_commit: str
) -> str:
    """Build the prompt for the DESIGN stage (default: Claude, configurable)."""
    return (
        f"MODE C — DESIGN stage.\n"
        f"TASK-ID: {task_id}\n"
        f"ROLE: Architect / Designer (read-only planner)\n"
        f"BASE-COMMIT: {base_commit}\n"
        f"WORKTREE: {worktree_path}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        "Analyze the user request and existing codebase. Produce a design document "
        "covering: requirements analysis, proposed changes, file impact, constraints, "
        "verification criteria, and edge cases.\n\n"
        "Do not implement code. Focus on clear, actionable design.\n"
        "Write the design to a file named DESIGN.md in the worktree root."
    )


def build_implement_prompt(
    *,
    task_id: str,
    user_request: str,
    design_output: str,
    worktree_path: str,
    base_commit: str,
) -> str:
    """Build the prompt for the IMPLEMENT stage (default: Codex, configurable)."""
    design_excerpt = design_output[:30_000] if design_output else "(no design output)"
    return (
        f"MODE C — IMPLEMENT stage.\n"
        f"TASK-ID: {task_id}\n"
        f"ROLE: Implementer (write access)\n"
        f"BASE-COMMIT: {base_commit}\n"
        f"WORKTREE: {worktree_path}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        f"DESIGN:\n{design_excerpt}\n\n"
        "Implement the changes described in the design. Write actual code.\n"
        "After implementation, run basic verification if possible.\n"
        "Record changed files and test results."
    )


def build_review_prompt(
    *,
    task_id: str,
    user_request: str,
    design_output: str,
    diff_text: str,
    check_results: str,
    worktree_path: str,
    base_commit: str,
    review_cycle: int = 0,
) -> str:
    """Build the prompt for the REVIEW stage (default: Antigravity/Gemini, configurable)."""
    design_excerpt = design_output[:15_000] if design_output else "(no design)"
    diff_excerpt = diff_text[:60_000] if diff_text else "(no diff)"
    checks_excerpt = check_results[:10_000] if check_results else "(no checks)"
    return (
        f"MODE C — REVIEW stage (cycle {review_cycle}).\n"
        f"TASK-ID: {task_id}\n"
        f"ROLE: Independent Reviewer (read-only)\n"
        f"BASE-COMMIT: {base_commit}\n"
        f"WORKTREE: {worktree_path}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        f"DESIGN:\n{design_excerpt}\n\n"
        f"CHECK RESULTS:\n{checks_excerpt}\n\n"
        f"DIFF:\n{diff_excerpt}\n\n"
        "Review the implementation against the user request and design.\n"
        "For each finding, provide: location, issue, evidence, and severity.\n"
        "Do not edit files. Do not commit.\n\n"
        "You MUST end your response with exactly one of these verdicts on its own line:\n"
        "VERDICT: PASS\n"
        "VERDICT: FIX_REQUIRED\n"
        "VERDICT: BLOCKED"
    )


def build_fix_prompt(
    *,
    task_id: str,
    user_request: str,
    review_findings: str,
    worktree_path: str,
    base_commit: str,
    review_cycle: int = 0,
) -> str:
    """Build the prompt for the FIX stage (default: Codex, configurable)."""
    findings_excerpt = review_findings[:30_000] if review_findings else "(no findings)"
    return (
        f"MODE C — FIX stage (cycle {review_cycle}).\n"
        f"TASK-ID: {task_id}\n"
        f"ROLE: Fix implementer (write access)\n"
        f"BASE-COMMIT: {base_commit}\n"
        f"WORKTREE: {worktree_path}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        f"REVIEW FINDINGS:\n{findings_excerpt}\n\n"
        "Address each FIX_REQUIRED finding from the review.\n"
        "Do not introduce unrelated changes.\n"
        "Run tests after fixing."
    )


def build_independent_work_prompt(
    *, task_id: str, user_request: str, agent: str, worktree_path: str, base_commit: str
) -> str:
    """Build the prompt for MODE A independent worker."""
    return (
        f"MODE A — INDEPENDENT_WORK.\n"
        f"TASK-ID: {task_id}\n"
        f"ROLE: Independent {agent} worker (write access)\n"
        f"BASE-COMMIT: {base_commit}\n"
        f"WORKTREE: {worktree_path}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        "Implement the user's request independently.\n"
        "Do NOT inspect sibling branches, worktrees, or other worker outputs.\n"
        "Run tests after implementation. Record changed files."
    )


def build_cross_review_prompt(
    *,
    task_id: str,
    user_request: str,
    reviewer: str,
    target: str,
    diff_text: str,
    changed_files: list[str],
    test_results: str,
    base_commit: str,
    target_branch: str,
    target_checkpoint: str,
) -> str:
    """Build the prompt for MODE A cross-review."""
    diff_excerpt = diff_text[:80_000] if diff_text else "(no diff)"
    tests_excerpt = test_results[:20_000] if test_results else "(no tests)"
    return (
        f"MODE A — CROSS_REVIEW.\n"
        f"TASK-ID: {task_id}\n"
        f"ROLE: {reviewer} reviewing {target}'s work (read-only)\n"
        f"BASE-COMMIT: {base_commit}\n"
        f"TARGET-BRANCH: {target_branch}\n"
        f"TARGET-CHECKPOINT: {target_checkpoint}\n"
        f"CHANGED-FILES: {', '.join(changed_files)}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        f"TEST RESULTS:\n{tests_excerpt}\n\n"
        "Report concrete findings with severity, file/location, evidence, "
        "and required change.\n"
        "Do not edit, commit, merge, or inspect the other worker's result.\n\n"
        f"DIFF:\n{diff_excerpt}"
    )


def build_response_prompt(
    *, task_id: str, worker: str, checkpoint: str, review_findings: str
) -> str:
    """Build the prompt for MODE A response round."""
    findings_excerpt = review_findings[:30_000] if review_findings else "(no findings)"
    return (
        f"MODE A — RESPONSE ROUND 1/1.\n"
        f"TASK-ID: {task_id}\n"
        f"ROLE: {worker} responding to review findings (read-only)\n"
        f"CHECKPOINT: {checkpoint}\n\n"
        "Do not edit files. For every finding use exactly one disposition:\n"
        "ACCEPT, REJECT, PARTIAL, or NEEDS_TEST.\n"
        "There is no second automatic round.\n\n"
        f"FINDINGS:\n{findings_excerpt}"
    )


def build_merge_prompt(
    *,
    task_id: str,
    selection: str,
    user_instruction: str,
    base_branch: str,
    base_head: str,
    selected_details: str,
    compare_text: str,
) -> str:
    """Build the prompt for MODE A Codex merge."""
    compare_excerpt = compare_text[:60_000] if compare_text else "(no compare)"
    return (
        f"MODE A — CODEX_MERGE after explicit user selection.\n"
        f"TASK-ID: {task_id}\n"
        f"USER-SELECTION: {selection}\n"
        f"USER-INSTRUCTION: {user_instruction or 'none'}\n"
        f"BASE-BRANCH: {base_branch}\n"
        f"BASE-CURRENT-HEAD: {base_head}\n\n"
        f"SELECTED RESULTS:\n{selected_details}\n\n"
        "Integrate exactly the selected result into the base working tree.\n"
        "You are the integration executor, not the decision maker.\n"
        "For SELECT_HYBRID, use both checkpoints and the user instruction.\n"
        "Do not commit, push, reset, or delete branches.\n"
        "Report changed files and checks.\n\n"
        f"COMPARE REPORT:\n{compare_excerpt}"
    )
