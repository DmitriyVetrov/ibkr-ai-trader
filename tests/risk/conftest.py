"""Deterministic fixtures for the risk suite.

Everything here is built by hand at a fixed instant. No test in this directory
reads a clock, opens a socket or constructs a broker, and the account state
each test reasons about is a stored snapshot rather than anything a gateway
said — which is the property the milestone exists to guarantee, so the tests
have to hold it too.

The builders are factories rather than plain fixtures because most tests want
*one thing changed*: a missing price, a stale quote, a different currency. A
factory keeps the change visible in the test that makes it instead of hiding it
in a fixture three files away.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_system.domain.enums import (
    ConfidenceLevel,
    Direction,
    ExpectedMagnitude,
    LegAction,
    MarketHypothesis,
    MaxLossBasis,
    OptionRight,
    PriceSource,
    StrategyType,
    TradingMode,
)
from trading_system.infrastructure.settings import SystemConfig
from trading_system.risk.limits import resolve_limits
from trading_system.risk.models import (
    AccountSnapshot,
    AllocationCandidate,
    CampaignPosition,
    CampaignSnapshot,
    CandidateLeg,
    CandidatePrice,
    OpportunityScore,
    RiskLimits,
    StrategyRiskProfile,
)

#: The instant every risk test is anchored at. The whole chain — quote,
#: candidate, campaign, account — shares it, so a "fresh" quote really is
#: fresh and a staleness test has to move a timestamp rather than wait.
NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
EXPIRATION = date(2026, 8, 31)


@pytest.fixture
def risk_limits(system_config: SystemConfig) -> RiskLimits:
    """The shipped limits, resolved across every layer."""
    return resolve_limits(system_config)


@pytest.fixture
def make_profile() -> Callable[..., StrategyRiskProfile]:
    def _make(**overrides: Any) -> StrategyRiskProfile:
        fields: dict[str, Any] = {
            "strategy": StrategyType.LONG_CALL,
            "strategy_version": "1.0.0",
            "max_loss_basis": MaxLossBasis.NET_DEBIT_PAID,
            "directional_view": Direction.BULLISH,
            "single_position": True,
            "leg_count": 1,
            "dte_min": 14,
            "dte_max": 30,
            "min_option_price_eur": Decimal("0.30"),
            "max_option_price_eur": Decimal("25.00"),
            "max_bid_ask_spread_pct": 10.0,
        }
        fields.update(overrides)
        return StrategyRiskProfile(**fields)

    return _make


@pytest.fixture
def make_leg() -> Callable[..., CandidateLeg]:
    def _make(**overrides: Any) -> CandidateLeg:
        fields: dict[str, Any] = {
            "leg_index": 0,
            "action": LegAction.BUY,
            "right": OptionRight.CALL,
            "ratio": 1,
            "underlying": "NVDA",
            "expiration": EXPIRATION,
            "strike": Decimal("180.00"),
            "multiplier": 100,
            "contract_id": 771234567,
            "trading_class": "NVDA",
            "exchange": "SMART",
            "currency": "EUR",
            "bid": Decimal("5.95"),
            "ask": Decimal("6.05"),
            "quote_as_of": NOW,
            "quote_snapshot_id": "snap-option-quotes-nvda",
        }
        fields.update(overrides)
        return CandidateLeg(**fields)

    return _make


@pytest.fixture
def make_score() -> Callable[..., OpportunityScore]:
    def _make(total: float = 80.0, **overrides: Any) -> OpportunityScore:
        fields: dict[str, Any] = {
            "total": total,
            "research_confidence": 70.0,
            "strategy_confidence": 70.0,
            "expected_magnitude": 70.0,
            "spread_quality": 83.3,
            "data_quality": 100.0,
            "weights": {
                "research_confidence": 0.30,
                "strategy_confidence": 0.20,
                "expected_magnitude": 0.15,
                "spread_quality": 0.20,
                "data_quality": 0.15,
            },
        }
        fields.update(overrides)
        return OpportunityScore(**fields)

    return _make


@pytest.fixture
def make_candidate(
    make_profile: Callable[..., StrategyRiskProfile],
    make_leg: Callable[..., CandidateLeg],
    make_score: Callable[..., OpportunityScore],
) -> Callable[..., AllocationCandidate]:
    """A well-formed purchase candidate: one long call at 605.00 per unit."""

    def _make(**overrides: Any) -> AllocationCandidate:
        price_overrides = overrides.pop("price_overrides", {})
        price_fields: dict[str, Any] = {
            "available": True,
            "source": PriceSource.ASK_DEBIT,
            "currency": "EUR",
            "unit_cost": Decimal("605.00"),
            "max_leg_spread_pct": 1.67,
            "quote_as_of": NOW,
        }
        price_fields.update(price_overrides)

        # Legs follow the symbol unless a test states its own. A candidate
        # whose legs named a different underlying is not merely unrealistic —
        # the model refuses it — so the default has to track the override.
        symbol = overrides.get("symbol", "NVDA")

        fields: dict[str, Any] = {
            "opportunity_id": "opportunity-nvda-0001",
            "symbol": symbol,
            "as_of": NOW,
            "strategy": StrategyType.LONG_CALL,
            "risk_profile": make_profile(),
            "legs": [make_leg(underlying=symbol)],
            "expiration": EXPIRATION,
            "dte": 21,
            "price": CandidatePrice(**price_fields),
            "score": make_score(),
            "hypothesis": MarketHypothesis.B,
            "research_confidence": ConfidenceLevel.MEDIUM,
            "strategy_confidence": ConfidenceLevel.MEDIUM,
            "expected_magnitude": ExpectedMagnitude.MODERATE,
            "horizon_days": 21,
            "research_usable": True,
            "contract_selection_id": "contract-NVDA-0001",
            "contract_run_id": "contract-run-0001",
            "strategy_decision_id": "strategy-NVDA-0001",
            "strategy_run_id": "strategy-run-0001",
            "research_report_id": "research-NVDA-0001",
            "research_run_id": "research-run-0001",
            "input_snapshot_ids": ["snap-chain-nvda", "snap-option-quotes-nvda"],
        }
        fields.update(overrides)
        return AllocationCandidate(**fields)

    return _make


@pytest.fixture
def make_campaign() -> Callable[..., CampaignSnapshot]:
    """An empty EUR 5,000 campaign holding a 20% reserve."""

    def _make(**overrides: Any) -> CampaignSnapshot:
        fields: dict[str, Any] = {
            "campaign_id": "campaign-001",
            "as_of": NOW,
            "currency": "EUR",
            "budget": Decimal("5000"),
            "reserve": Decimal("1000.00"),
            "open_positions": [],
            "realized_pnl_today": None,
        }
        fields.update(overrides)
        return CampaignSnapshot(**fields)

    return _make


@pytest.fixture
def make_reservation() -> Callable[..., CampaignPosition]:
    def _make(**overrides: Any) -> CampaignPosition:
        fields: dict[str, Any] = {
            "opportunity_id": "opportunity-held-0001",
            "allocation_id": "allocation-held-0001",
            "symbol": "AAPL",
            "strategy": StrategyType.LONG_CALL,
            "direction": Direction.BULLISH,
            "quantity": 1,
            "capital_committed": Decimal("500.00"),
            "max_loss": Decimal("500.00"),
            "authorized_at": NOW - timedelta(days=1),
        }
        fields.update(overrides)
        return CampaignPosition(**fields)

    return _make


@pytest.fixture
def make_account() -> Callable[..., AccountSnapshot]:
    """A paper account holding far more than the campaign may spend.

    Deliberately so: the campaign budget must bind long before the account
    does, which is the whole point of having a campaign envelope.
    """

    def _make(**overrides: Any) -> AccountSnapshot:
        fields: dict[str, Any] = {
            "snapshot_id": "account-20260810T143000Z-abc",
            "as_of": NOW,
            "captured_at": NOW,
            "broker": "SIMULATOR",
            "account_id": "DU0000000",
            "currency": "EUR",
            "trading_mode": TradingMode.PAPER,
            "cash": Decimal("100000.00"),
            "net_liquidation": Decimal("100000.00"),
            "buying_power": Decimal("400000.00"),
            "available_funds": Decimal("98000.00"),
            "positions": [],
            "read_only": True,
            "orders_submitted": 0,
            "simulated": True,
        }
        fields.update(overrides)
        return AccountSnapshot(**fields)

    return _make
