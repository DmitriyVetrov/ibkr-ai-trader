"""AI agent suites — not yet implemented.

Delivered in Milestone 4 onwards.

Each agent gets a dedicated suite validating input/output schema, enum membership,
refusal and no-trade behaviour, malformed input, missing data and source attribution.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="AI agent suites are delivered in Milestone 4 onwards")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
