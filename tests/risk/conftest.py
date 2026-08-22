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
    DailyPnLStatus,
    Direction,
    ExpectedMagnitude,
    FxRateOrigin,
    LegAction,
    MarketHypothesis,
    MaxLossBasis,
    OptionRight,
    PriceSource,
    StrategyType,
    TradingMode,
)
from trading_system.fx.models import FxRate, FxRateTable
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

#: The exchange rate every test in this suite converts at.
#:
#: Injected explicitly, exactly as the brief requires: deterministic tests get a
#: **stated** rate rather than a convenient one, and it is deliberately not 1.0.
#: A test rate of parity would let every cross-currency defect in the system
#: pass the whole suite and fail only against a real account.
#:
#: 1.10 is chosen so the arithmetic stays legible by hand - the shipped EUR
#: 5,000 campaign becomes USD 5,500, its EUR 1,500 per-trade ceiling becomes
#: USD 1,650 - which matters because these are the numbers every boundary test
#: in the suite sits one cent either side of.
TEST_EUR_USD = Decimal("1.10")


def eur_usd_rates(as_of: datetime = NOW, rate: Decimal = TEST_EUR_USD) -> FxRateTable:
    """A EUR/USD rate table for a test to hand to a snapshot or a resolution.

    Marked :attr:`FxRateOrigin.CONFIGURED`, which is the origin reserved for a
    rate that came from a fixture or an explicit override rather than from a
    broker. Nothing in production constructs one, and the origin travels onto
    every artifact built from it, so a stored record can never leave a reader
    wondering whether a rate was real.
    """
    return FxRateTable(
        rates=(
            FxRate(
                base_currency="EUR",
                quote_currency="USD",
                rate=rate,
                as_of=as_of,
                origin=FxRateOrigin.CONFIGURED,
                source="TEST_FIXTURE",
            ),
        )
    )


@pytest.fixture
def risk_limits(system_config: SystemConfig) -> RiskLimits:
    """The shipped limits, resolved across every layer and converted once.

    The shipped campaign declares its capital in EUR and trades in USD, so
    these come back in USD at :data:`TEST_EUR_USD`. That is the configuration
    the system actually ships, and testing the limits in a currency they are
    never compared in would test a path nothing runs.
    """
    return resolve_limits(system_config, fx_rates=eur_usd_rates(), as_of=NOW)


@pytest.fixture
def unconvertible_limits(system_config: SystemConfig) -> RiskLimits:
    """The same limits with no rate available at all.

    What an operator gets before capturing an account, and what every candidate
    is measured against when the broker quotes no rate: the declared figures,
    labelled EUR, and marked not convertible.
    """
    return resolve_limits(system_config, fx_rates=None, as_of=NOW)


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
            "min_option_price": Decimal("0.30"),
            "max_option_price": Decimal("25.00"),
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
            # The instrument's own currency, which is what this campaign
            # trades. An option on a US listing is quoted in dollars whatever
            # currency the account holding it is based in.
            "currency": "USD",
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
            "currency": "USD",
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
    """An empty campaign: EUR 5,000 declared, USD 5,500 to spend.

    The figures here are in the currency the campaign *trades*, because that is
    what a reservation costs and what every limit is compared against. The
    declared original travels alongside so the record never loses the currency
    the operator actually holds.

    A realised figure implies ``daily_pnl_status=TRACKED`` unless a test says
    otherwise. The snapshot refuses a figure alongside any other status — that
    combination is what "we could not measure today" quietly passing a loss
    limit would look like — so a fixture that let a caller build one would be
    handing tests a shape the system cannot produce.
    """

    def _make(**overrides: Any) -> CampaignSnapshot:
        fields: dict[str, Any] = {
            "campaign_id": "campaign-001",
            "as_of": NOW,
            "currency": "USD",
            "budget": Decimal("5500.00"),
            "reserve": Decimal("1100.00"),
            "declared_budget": Decimal("5000"),
            "declared_currency": "EUR",
            "open_positions": [],
            "realized_pnl_today": None,
        }
        fields.update(overrides)
        if fields.get("realized_pnl_today") is not None:
            fields.setdefault("daily_pnl_status", DailyPnLStatus.TRACKED)
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

    Based in EUR and carrying a EUR/USD rate, which is the shape a real
    European account trading US options has. A test that wants the no-rate case
    passes ``fx_rates=FxRateTable()``.
    """

    def _make(**overrides: Any) -> AccountSnapshot:
        fields: dict[str, Any] = {
            "snapshot_id": "account-20260810T143000Z-abc",
            "as_of": NOW,
            "captured_at": NOW,
            "broker": "SIMULATOR",
            "account_id": "DU0000000",
            # The account's BASE currency. It is not the currency the campaign
            # trades, and the gap between the two is what the FX layer closes.
            "currency": "EUR",
            "trading_mode": TradingMode.PAPER,
            "cash": Decimal("100000.00"),
            "net_liquidation": Decimal("100000.00"),
            "buying_power": Decimal("400000.00"),
            "available_funds": Decimal("98000.00"),
            "cash_by_currency": {"EUR": Decimal("100000.00"), "USD": Decimal("0.00")},
            "fx_rates": eur_usd_rates(),
            "positions": [],
            "read_only": True,
            "orders_submitted": 0,
            "simulated": True,
        }
        fields.update(overrides)
        return AccountSnapshot(**fields)

    return _make
