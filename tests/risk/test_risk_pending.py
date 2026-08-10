"""Risk engine suites — not yet implemented.

Delivered in Milestone 7.

Every limit in config/risk.yaml, plus the critical invariant that no risk rejection can
be overridden by an AI agent.

This placeholder keeps the suite discoverable and makes the gap visible in the
test report instead of silently absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Risk engine suites are delivered in Milestone 7")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")
