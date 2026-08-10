"""Integration suites — not yet implemented.

Delivered in Milestones 5-9.

Multi-component flows: research to strategy, strategy to allocation, allocation to risk,
risk to execution, and the full position lifecycle. Simulated broker by default.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Integration suites are delivered in Milestones 5-9")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
