"""Data provider suites — not yet implemented.

Delivered in Milestone 3.

Per-provider tests for valid responses, missing fields, stale data, malformed data,
timeouts, rate limits, duplicates and timestamp normalisation.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Data provider suites are delivered in Milestone 3")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
