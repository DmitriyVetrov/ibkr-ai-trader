"""Integration suites — the remaining multi-component flows.

Milestones 5-10 delivered research to strategy to contract to risk to
allocation to execution to fill to position to reconciliation, and then the exit
loop that closes a position again. Those live in ``test_research_to_*.py``,
``test_execution_to_position.py``, ``test_reconciliation_workflow.py`` and
``test_exit_to_execution_to_reconciliation.py``, all against the simulated
broker.

What is left is the **scheduled** lifecycle: a process that runs the monitoring
loop on a cadence, sends a notification when a position exits, and reports its
own health. Milestone 10 built the operation such a process will call and
stopped there, deliberately — a scheduler that ran an exit engine nobody had
tested would be the wrong order to build them in.

This placeholder keeps the gap visible in the test report instead of silently
absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="The scheduled loop, notifications and health checks are delivered later")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
