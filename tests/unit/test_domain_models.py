"""Domain models: construction, and the invariants that protect real money."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system.domain.enums import (
    Direction,
    ExitAction,
    ExitReason,
    ExpectedMagnitude,
    LegAction,
    MarketHypothesis,
    OptionRight,
    OrderType,
    RiskOutcome,
    RiskReasonCode,
    StrategyAction,
    StrategyType,
    TradingMode,
)
from trading_system.domain.models import (
    AllocationDecision,
    AllocationEntry,
    ContractSelection,
    ExitDecision,
    OptionLeg,
    OrderIntent,
    PurchaseCard,
    ResearchReport,
    RiskDecision,
    StrategyDecision,
    SystemVersions,
    UniverseCandidate,
    UniverseSelection,
)

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_every_boundary_artifact_can_be_instantiated(
    universe_selection: UniverseSelection,
    research_report: ResearchReport,
    strategy_decision: StrategyDecision,
    purchase_card: PurchaseCard,
    allocation_decision: AllocationDecision,
    risk_decision: RiskDecision,
    order_intent: OrderIntent,
    execution_result: object,
    position_snapshot: object,
    exit_decision: ExitDecision,
    trade_snapshot: object,
) -> None:
    for artifact in (
        universe_selection,
        research_report,
        strategy_decision,
        purchase_card,
        allocation_decision,
        risk_decision,
        order_intent,
        execution_result,
        position_snapshot,
        exit_decision,
        trade_snapshot,
    ):
        assert artifact is not None


@pytest.mark.unit
def test_trade_artifacts_are_immutable(purchase_card: PurchaseCard) -> None:
    """The purchase card is the authoritative reason for a trade; it is written once."""
    with pytest.raises(ValidationError):
        purchase_card.quantity = 99  # type: ignore[misc]


@pytest.mark.unit
def test_unknown_fields_are_rejected(versions: SystemVersions) -> None:
    """An agent returning an unexpected field is a contract violation."""
    with pytest.raises(ValidationError):
        UniverseCandidate(  # type: ignore[call-arg]
            ticker="NVDA", rank=1, selection_score=90.0, made_up_field="x"
        )


# ---------------------------------------------------------------------------
# Money must be decimal-safe
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_monetary_field_rejects_binary_float() -> None:
    with pytest.raises(ValidationError, match="binary floating point"):
        OptionLeg(
            underlying="NVDA",
            right=OptionRight.CALL,
            strike=180.25,
            expiration=date(2026, 8, 31),
            action=LegAction.BUY,
        )


@pytest.mark.unit
@pytest.mark.parametrize("value", [Decimal("180.25"), "180.25", 180])
def test_monetary_field_accepts_exact_representations(value: object) -> None:
    leg = OptionLeg(
        underlying="NVDA",
        right=OptionRight.CALL,
        strike=value,
        expiration=date(2026, 8, 31),
        action=LegAction.BUY,
    )
    assert isinstance(leg.strike, Decimal)


@pytest.mark.unit
def test_decimal_arithmetic_stays_exact(allocation_decision: AllocationDecision) -> None:
    total = allocation_decision.allocated + allocation_decision.reserve
    assert total == allocation_decision.total_budget
    assert total == Decimal("5000")


# ---------------------------------------------------------------------------
# Time must be aware and UTC
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_naive_datetime_is_rejected(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        UniverseSelection(
            universe_id="u1",
            as_of=datetime(2026, 8, 10, 14, 30),
            candidates=[],
            versions=versions,
        )


@pytest.mark.unit
def test_aware_datetime_is_normalised_to_utc(versions: SystemVersions) -> None:
    madrid = timezone(timedelta(hours=2))
    selection = UniverseSelection(
        universe_id="u1",
        as_of=datetime(2026, 8, 10, 16, 30, tzinfo=madrid),
        candidates=[],
        versions=versions,
    )
    assert selection.as_of.tzinfo == UTC
    assert selection.as_of == NOW


# ---------------------------------------------------------------------------
# Cross-field invariants
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_universe_ranks_must_be_contiguous(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="contiguous"):
        UniverseSelection(
            universe_id="u1",
            as_of=NOW,
            candidates=[
                UniverseCandidate(ticker="NVDA", rank=1, selection_score=90.0),
                UniverseCandidate(ticker="AAPL", rank=3, selection_score=80.0),
            ],
            versions=versions,
        )


@pytest.mark.unit
def test_universe_rejects_duplicate_tickers(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="duplicate ticker"):
        UniverseSelection(
            universe_id="u1",
            as_of=NOW,
            candidates=[
                UniverseCandidate(ticker="NVDA", rank=1, selection_score=90.0),
                UniverseCandidate(ticker="NVDA", rank=2, selection_score=80.0),
            ],
            versions=versions,
        )


@pytest.mark.unit
def test_hypothesis_e_requires_an_explanation(
    versions: SystemVersions,
) -> None:
    with pytest.raises(ValidationError, match="explanation"):
        ResearchReport(
            report_id="r1",
            ticker="NVDA",
            as_of=NOW,
            hypothesis=MarketHypothesis.E,
            direction=Direction.UNCERTAIN,
            expected_magnitude=ExpectedMagnitude.MODERATE,
            confidence=0.5,
            expected_horizon_days=21,
            invalidation_conditions=["something"],
            versions=versions,
        )


@pytest.mark.unit
def test_research_requires_invalidation_conditions(versions: SystemVersions) -> None:
    """A thesis that cannot be invalidated cannot be monitored."""
    with pytest.raises(ValidationError):
        ResearchReport(
            report_id="r1",
            ticker="NVDA",
            as_of=NOW,
            hypothesis=MarketHypothesis.B,
            direction=Direction.BULLISH,
            expected_magnitude=ExpectedMagnitude.MODERATE,
            confidence=0.5,
            expected_horizon_days=21,
            invalidation_conditions=[],
            versions=versions,
        )


@pytest.mark.unit
@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_is_bounded(versions: SystemVersions, confidence: float) -> None:
    with pytest.raises(ValidationError):
        ResearchReport(
            report_id="r1",
            ticker="NVDA",
            as_of=NOW,
            hypothesis=MarketHypothesis.B,
            direction=Direction.BULLISH,
            expected_magnitude=ExpectedMagnitude.MODERATE,
            confidence=confidence,
            expected_horizon_days=21,
            invalidation_conditions=["x"],
            versions=versions,
        )


@pytest.mark.unit
def test_buy_requires_a_strategy(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="requires a strategy_type"):
        StrategyDecision(
            decision_id="s1",
            ticker="NVDA",
            research_report_id="r1",
            as_of=NOW,
            action=StrategyAction.BUY,
            strategy_type=None,
            rationale="because",
            versions=versions,
        )


@pytest.mark.unit
def test_no_trade_must_not_carry_a_strategy(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="must not carry"):
        StrategyDecision(
            decision_id="s1",
            ticker="NVDA",
            research_report_id="r1",
            as_of=NOW,
            action=StrategyAction.NO_TRADE,
            strategy_type=StrategyType.LONG_CALL,
            rationale="because",
            versions=versions,
        )


@pytest.mark.unit
def test_no_trade_is_a_valid_outcome(versions: SystemVersions) -> None:
    decision = StrategyDecision(
        decision_id="s1",
        ticker="NVDA",
        research_report_id="r1",
        as_of=NOW,
        action=StrategyAction.NO_TRADE,
        rationale="Evidence is too thin to justify premium.",
        versions=versions,
    )
    assert decision.action is StrategyAction.NO_TRADE


@pytest.mark.unit
def test_allocation_books_must_balance(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="must equal total_budget"):
        AllocationDecision(
            allocation_id="a1",
            campaign_id="c1",
            as_of=NOW,
            currency="USD",
            total_budget=Decimal("5000"),
            allocated=Decimal("1000"),
            reserve=Decimal("1000"),
            entries=[
                AllocationEntry(
                    opportunity_id="o1",
                    ticker="NVDA",
                    rank=1,
                    opportunity_score=90.0,
                    allocated=Decimal("1000"),
                )
            ],
            versions=versions,
        )


@pytest.mark.unit
def test_allocation_entries_must_sum_to_allocated(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="do not sum"):
        AllocationDecision(
            allocation_id="a1",
            campaign_id="c1",
            as_of=NOW,
            currency="USD",
            total_budget=Decimal("5000"),
            allocated=Decimal("1000"),
            reserve=Decimal("4000"),
            entries=[
                AllocationEntry(
                    opportunity_id="o1",
                    ticker="NVDA",
                    rank=1,
                    opportunity_score=90.0,
                    allocated=Decimal("750"),
                )
            ],
            versions=versions,
        )


@pytest.mark.unit
def test_zero_allocation_is_valid(versions: SystemVersions) -> None:
    """A ranked opportunity need not be funded."""
    decision = AllocationDecision(
        allocation_id="a1",
        campaign_id="c1",
        as_of=NOW,
        currency="USD",
        total_budget=Decimal("5000"),
        allocated=Decimal("0"),
        reserve=Decimal("5000"),
        entries=[
            AllocationEntry(
                opportunity_id="o1",
                ticker="NVDA",
                rank=1,
                opportunity_score=72.0,
                allocated=Decimal("0"),
            )
        ],
        versions=versions,
    )
    assert decision.allocated == Decimal("0")


@pytest.mark.unit
def test_approved_risk_decision_cannot_carry_a_rejection_reason(
    versions: SystemVersions,
) -> None:
    with pytest.raises(ValidationError, match="exactly one reason code"):
        RiskDecision(
            decision_id="r1",
            purchase_card_id="c1",
            as_of=NOW,
            outcome=RiskOutcome.APPROVED,
            reason_codes=[RiskReasonCode.DAILY_LOSS_LIMIT_REACHED],
            trading_mode=TradingMode.PAPER,
            versions=versions,
        )


@pytest.mark.unit
def test_rejected_risk_decision_cannot_claim_ok(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="must not carry the OK"):
        RiskDecision(
            decision_id="r1",
            purchase_card_id="c1",
            as_of=NOW,
            outcome=RiskOutcome.REJECTED,
            reason_codes=[RiskReasonCode.OK, RiskReasonCode.SPREAD_TOO_WIDE],
            trading_mode=TradingMode.PAPER,
            versions=versions,
        )


@pytest.mark.unit
def test_rejection_requires_at_least_one_code(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError):
        RiskDecision(
            decision_id="r1",
            purchase_card_id="c1",
            as_of=NOW,
            outcome=RiskOutcome.REJECTED,
            reason_codes=[],
            trading_mode=TradingMode.PAPER,
            versions=versions,
        )


@pytest.mark.unit
def test_limit_order_requires_a_price(
    versions: SystemVersions, contract_selection: ContractSelection
) -> None:
    with pytest.raises(ValidationError, match="requires a limit_price"):
        OrderIntent(
            intent_id="i1",
            purchase_card_id="c1",
            risk_decision_id="r1",
            created_at=NOW,
            underlying="NVDA",
            strategy_type=StrategyType.LONG_CALL,
            legs=list(contract_selection.legs),
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=None,
            trading_mode=TradingMode.PAPER,
            versions=versions,
        )


@pytest.mark.unit
def test_market_order_must_not_carry_a_price(
    versions: SystemVersions, contract_selection: ContractSelection
) -> None:
    with pytest.raises(ValidationError, match="must not carry a limit_price"):
        OrderIntent(
            intent_id="i1",
            purchase_card_id="c1",
            risk_decision_id="r1",
            created_at=NOW,
            underlying="NVDA",
            strategy_type=StrategyType.LONG_CALL,
            legs=list(contract_selection.legs),
            quantity=1,
            order_type=OrderType.MARKET,
            limit_price=Decimal("6.00"),
            trading_mode=TradingMode.PAPER,
            versions=versions,
        )


@pytest.mark.unit
def test_contract_legs_must_share_the_underlying() -> None:
    with pytest.raises(ValidationError, match="all legs must reference"):
        ContractSelection(
            underlying="NVDA",
            strategy_type=StrategyType.LONG_STRADDLE,
            as_of=NOW,
            legs=[
                OptionLeg(
                    underlying="NVDA",
                    right=OptionRight.CALL,
                    strike=Decimal("180"),
                    expiration=date(2026, 8, 31),
                    action=LegAction.BUY,
                ),
                OptionLeg(
                    underlying="AAPL",
                    right=OptionRight.PUT,
                    strike=Decimal("180"),
                    expiration=date(2026, 8, 31),
                    action=LegAction.BUY,
                ),
            ],
            dte=21,
        )


@pytest.mark.unit
def test_sell_requires_an_exit_reason(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="requires an exit reason"):
        ExitDecision(
            decision_id="e1",
            position_id="p1",
            as_of=NOW,
            decision=ExitAction.SELL,
            reason=None,
            versions=versions,
        )


@pytest.mark.unit
def test_hold_must_not_carry_an_exit_reason(versions: SystemVersions) -> None:
    with pytest.raises(ValidationError, match="must not carry an exit reason"):
        ExitDecision(
            decision_id="e1",
            position_id="p1",
            as_of=NOW,
            decision=ExitAction.HOLD,
            reason=ExitReason.TRAILING_STOP,
            versions=versions,
        )


@pytest.mark.unit
def test_exit_closes_the_whole_strategy_by_default(versions: SystemVersions) -> None:
    decision = ExitDecision(
        decision_id="e1",
        position_id="p1",
        as_of=NOW,
        decision=ExitAction.SELL,
        reason=ExitReason.THESIS_INVALIDATION,
        versions=versions,
    )
    assert decision.close_whole_strategy is True
