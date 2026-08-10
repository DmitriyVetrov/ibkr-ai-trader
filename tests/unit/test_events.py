"""Domain events carry enough context to reconstruct a decision after the fact."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system.domain import events
from trading_system.domain.enums import (
    ExitReason,
    MarketHypothesis,
    PositionState,
    ReconciliationStatus,
    RiskOutcome,
    StrategyAction,
    StrategyType,
    ThesisStatus,
    TradingMode,
)

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


@pytest.mark.unit
def test_every_event_declares_a_distinct_type() -> None:
    """The event_type discriminator must be unique for replay to be unambiguous."""
    types = []
    for name in events.__all__:
        cls = getattr(events, name)
        if cls is events.DomainEvent:
            continue
        field = cls.model_fields["event_type"]
        types.append(field.default)

    assert len(types) == len(set(types)), "duplicate event_type discriminator"
    assert all(t.isupper() for t in types)


@pytest.mark.unit
def test_research_completed_event() -> None:
    event = events.ResearchCompleted(
        event_id="e1",
        occurred_at=NOW,
        report_id="research-001",
        ticker="NVDA",
        hypothesis=MarketHypothesis.B,
        confidence=0.7,
    )
    assert event.event_type == "RESEARCH_COMPLETED"
    assert event.occurred_at.tzinfo is UTC


@pytest.mark.unit
def test_no_trade_is_recorded_as_an_event() -> None:
    """NO_TRADE is an outcome worth persisting, not an absence of one."""
    event = events.NoTradeDecided(
        event_id="e1",
        occurred_at=NOW,
        ticker="NVDA",
        stage="strategy_selection",
        reason="No configured strategy matches hypothesis E.",
    )
    assert event.event_type == "NO_TRADE_DECIDED"


@pytest.mark.unit
def test_strategy_selected_allows_a_no_trade_outcome() -> None:
    event = events.StrategySelected(
        event_id="e1",
        occurred_at=NOW,
        decision_id="s1",
        ticker="NVDA",
        action=StrategyAction.NO_TRADE,
    )
    assert event.strategy_type is None


@pytest.mark.unit
def test_risk_evaluated_requires_a_reason_code() -> None:
    with pytest.raises(ValidationError):
        events.RiskEvaluated(
            event_id="e1",
            occurred_at=NOW,
            decision_id="r1",
            purchase_card_id="c1",
            outcome=RiskOutcome.REJECTED,
            reason_codes=[],
        )


@pytest.mark.unit
def test_order_submitted_records_the_trading_mode() -> None:
    """Which account an order went to must be reconstructable from the log alone."""
    event = events.OrderSubmitted(
        event_id="e1",
        occurred_at=NOW,
        intent_id="i1",
        broker="SIMULATOR",
        trading_mode=TradingMode.PAPER,
    )
    assert event.trading_mode is TradingMode.PAPER


@pytest.mark.unit
def test_thesis_update_records_both_old_and_new_status() -> None:
    """The original thesis is immutable; updates append rather than overwrite."""
    event = events.ThesisUpdated(
        event_id="e1",
        occurred_at=NOW,
        position_id="p1",
        previous_status=ThesisStatus.VALID,
        new_status=ThesisStatus.INVALIDATED,
        evidence=["Guidance withdrawn"],
    )
    assert event.previous_status is ThesisStatus.VALID
    assert event.new_status is ThesisStatus.INVALIDATED


@pytest.mark.unit
def test_state_change_event_records_both_ends() -> None:
    event = events.PositionStateChanged(
        event_id="e1",
        occurred_at=NOW,
        position_id="p1",
        from_state=PositionState.OPEN,
        to_state=PositionState.MONITORING,
    )
    assert event.from_state is PositionState.OPEN


@pytest.mark.unit
def test_reconciliation_mismatch_carries_discrepancies() -> None:
    event = events.ReconciliationChecked(
        event_id="e1",
        occurred_at=NOW,
        status=ReconciliationStatus.MISMATCH,
        discrepancies=["internal quantity 4, broker reports 3"],
    )
    assert event.status is ReconciliationStatus.MISMATCH
    assert event.discrepancies


@pytest.mark.unit
def test_position_closed_carries_pnl_and_reason() -> None:
    event = events.PositionClosed(
        event_id="e1",
        occurred_at=NOW,
        position_id="p1",
        realized_pnl_eur=Decimal("180.00"),
        exit_reason=ExitReason.TRAILING_STOP,
    )
    assert isinstance(event.realized_pnl_eur, Decimal)


@pytest.mark.unit
def test_event_money_rejects_binary_float() -> None:
    with pytest.raises(ValidationError, match="binary floating point"):
        events.PositionClosed(
            event_id="e1",
            occurred_at=NOW,
            position_id="p1",
            realized_pnl_eur=180.0,
            exit_reason=ExitReason.TRAILING_STOP,
        )


@pytest.mark.unit
def test_events_are_immutable() -> None:
    event = events.ContractSelected(
        event_id="e1",
        occurred_at=NOW,
        underlying="NVDA",
        strategy_type=StrategyType.LONG_CALL,
        dte=21,
        leg_count=1,
    )
    with pytest.raises(ValidationError):
        event.dte = 30  # type: ignore[misc]
