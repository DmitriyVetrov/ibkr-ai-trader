"""Broker adapter suites — not yet implemented.

Delivered in Milestone 2.

IBKR adapter, positions, orders, reconciliation and the simulator. Read-only tests must
assert orders_submitted == 0.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Broker adapter suites are delivered in Milestone 2")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
