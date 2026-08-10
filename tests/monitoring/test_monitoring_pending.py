"""Monitoring suites — not yet implemented.

Delivered in Milestones 9-10.

Trailing stop, time-to-expiration policy, thesis monitor, reconciliation loop and
scheduler.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Monitoring suites are delivered in Milestones 9-10")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
