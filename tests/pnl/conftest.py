"""Fixtures for the Milestone 11 profit-and-loss suites.

The risk fixtures are reused rather than rebuilt: the daily-loss tests are
about the *risk engine's* reading of a realised figure, and testing that
against a hand-built campaign the engine never sees would be testing something
else. Everything else is constructed in ``tests/pnl/factories.py``, in process,
with no network, no broker and no model.
"""

from __future__ import annotations

from tests.risk.conftest import (  # noqa: F401 - re-exported as fixtures
    make_account,
    make_campaign,
    make_candidate,
    make_leg,
    make_profile,
    make_score,
    risk_limits,
)
