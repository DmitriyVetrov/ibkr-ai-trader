"""Monitoring suites — not yet implemented.

Delivered in Milestone 10.

Trailing stop, time-to-expiration policy, thesis monitor, exit engine, the
recurring reconciliation loop and the scheduler. Milestone 9 delivered the
comparison those loops will run — ``tests/reconciliation`` — but not the loop
that runs it on a schedule, and no exit policy at all: a position is observed,
not managed.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Monitoring suites are delivered in Milestone 10")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
