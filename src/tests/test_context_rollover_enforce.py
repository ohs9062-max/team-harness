"""Boundary decisions for worker context enforcement."""

from team_harness.worker_context import rollover_decision


def test_below_budget_continues():
    assert rollover_decision(99, 100, 2, 1, 0, True) == "continue"


def test_at_budget_continues():
    assert rollover_decision(100, 100, 2, 1, 0, True) == "continue"


def test_unknown_tokens_continue():
    assert rollover_decision(None, 100, 2, 1, 0, True) == "continue"


def test_missing_worklog_continues():
    assert rollover_decision(101, 100, None, 1, 0, True) == "continue"


def test_unchanged_worklog_continues():
    assert rollover_decision(101, 100, 1, 1, 0, True) == "continue"


def test_over_budget_at_boundary_rolls_over():
    assert rollover_decision(101, 100, 2, 1, 0, True) == "rollover"


def test_without_progress_requires_split():
    assert rollover_decision(101, 100, 2, 1, 1, False) == "split_needed"


def test_three_rollovers_require_split():
    assert rollover_decision(101, 100, 2, 1, 3, True) == "split_needed"
