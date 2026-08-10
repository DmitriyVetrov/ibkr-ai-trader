"""Execution engine suites — not yet implemented.

Delivered in Milestone 8.

Order construction, submission, partial fills, cancellation and controlled replacement,
against the simulator.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Execution engine suites are delivered in Milestone 8")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
