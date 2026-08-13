"""Integration suites — the remaining multi-component flows.

Milestones 5-9 delivered research to strategy to contract to risk to allocation
to execution to fill to position to reconciliation; those live in
``test_research_to_*.py``, ``test_execution_to_position.py`` and
``test_reconciliation_workflow.py``, all against the simulated broker.

What is left for Milestone 10 is the *scheduled* lifecycle: the monitoring loop
that runs reconciliation repeatedly, the thesis monitor, and the exit engine
that closes a position. Nothing in this system closes one today.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="The scheduled position lifecycle is delivered in Milestone 10")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
