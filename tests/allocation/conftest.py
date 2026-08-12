"""Deterministic fixtures for the allocation suite.

Reuses the risk suite's builders rather than duplicating them: the allocation
engine's input *is* the risk engine's input, and two divergent copies of a
candidate factory would eventually disagree about what a well-formed candidate
looks like — in exactly the direction that makes a test pass for the wrong
reason.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.risk.conftest import (  # noqa: F401 - fixtures are used by name
    make_account,
    make_campaign,
    make_candidate,
    make_leg,
    make_profile,
    make_reservation,
    make_score,
    risk_limits,
)
from trading_system.allocation.budget_allocator import AllocationEngine
from trading_system.risk.engine import RiskEngine
from trading_system.risk.models import RiskLimits

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


@pytest.fixture
def make_engine(risk_limits: RiskLimits) -> Callable[..., AllocationEngine]:  # noqa: F811
    """An allocation engine over the shipped limits, with overrides.

    ``overrides`` edits the *resolved* limits rather than the YAML, so a test
    that needs a 900-euro ceiling says so in one line instead of rewriting a
    configuration tree. Configuration loading itself is tested in
    ``tests/risk/test_limits.py``, where it belongs.
    """

    def _make(**overrides: Any) -> AllocationEngine:
        limits = risk_limits.model_copy(update=overrides) if overrides else risk_limits
        return AllocationEngine(limits, RiskEngine(limits))

    return _make


@pytest.fixture
def allocate(make_engine: Callable[..., AllocationEngine], make_account, make_campaign):  # noqa: F811
    """Run the engine over candidates against an empty campaign by default."""

    def _allocate(candidates, *, campaign=None, account=..., **overrides: Any):
        engine = make_engine(**overrides)
        return engine.allocate(
            candidates,
            campaign if campaign is not None else make_campaign(),
            as_of=NOW,
            account=make_account() if account is ... else account,
        )

    return _allocate


@pytest.fixture
def priced(make_candidate):  # noqa: F811
    """A candidate at an exact unit cost, with a distinct opportunity id."""

    def _priced(cost: str, *, symbol: str = "NVDA", score: float | None = None, **overrides: Any):
        fields: dict[str, Any] = {
            "symbol": symbol,
            "opportunity_id": f"opportunity-{symbol.lower()}-{cost.replace('.', '')}",
            "price_overrides": {"unit_cost": Decimal(cost)},
        }
        fields.update(overrides)
        candidate = make_candidate(**fields)
        if score is None:
            return candidate
        return candidate.model_copy(
            update={"score": candidate.score.model_copy(update={"total": score})}
        )

    return _priced
