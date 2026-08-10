"""Campaign allocation suites — not yet implemented.

Delivered in Milestone 7.

Budget scenarios: one strong candidate, ten candidates, budget exhausted, min/max
allocation, concentration limits, reserve cash, ties and deterministic repeatability.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Campaign allocation suites are delivered in Milestone 7")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
