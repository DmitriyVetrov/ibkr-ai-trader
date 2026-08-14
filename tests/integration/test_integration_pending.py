"""Integration suites — the remaining multi-component flows.

Milestones 5-10 delivered research to strategy to contract to risk to
allocation to execution to fill to position to reconciliation, and then the
exit loop that closes a position again. Milestone 11 closed the last of the
loop: a scheduler that runs the monitoring cycle on a cadence, realised profit
and loss from confirmed fills, settlement that returns capital to the campaign,
and operational health and alerts over all of it. Those live in
``test_operations_lifecycle.py`` and in ``tests/operations``.

What is left is **live operation**: a long-running process against a real
gateway, over days rather than a fixture, with a real notification channel at
the other end. That is Milestone 12's, and it is not something a test suite can
stand in for — which is why the paper-execution and paper-exit tests are opt-in
and gated behind two environment variables rather than simulated here.

This placeholder keeps the gap visible in the test report instead of silently
absent.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.skip(reason="Live multi-day operation is delivered after Milestone 11")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")


def test_the_operations_lifecycle_is_covered(repo_root) -> None:
    """The chain Milestone 11 closed, asserted to have a test of its own.

    Named rather than assumed: this file is what a reader checks to find out
    what is *not* covered, so it must not quietly outlive the gap it describes.
    """
    covered = repo_root / "tests" / "integration" / "test_operations_lifecycle.py"

    assert covered.is_file()
    source = covered.read_text(encoding="utf-8")
    for claim in (
        "realised profit and loss",
        "capital returns to the campaign",
        "second_run",
        "UNKNOWN",
    ):
        assert claim in source, claim
