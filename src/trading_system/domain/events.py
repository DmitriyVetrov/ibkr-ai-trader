"""Domain events emitted as a candidate moves through the workflow.

Events are an append-only audit trail, not system state. Current state is
reconstructed from persisted artifacts and — for anything the broker knows
about — from the broker itself.

Milestone 1 defines the vocabulary and the payloads. Dispatching, persistence
and subscribers arrive with the scheduler in Milestone 10.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from trading_system.domain.enums import (
    ExitReason,
    MarketHypothesis,
    PositionState,
    ReconciliationStatus,
    RiskOutcome,
    RiskReasonCode,
    StrategyAction,
    StrategyType,
    ThesisStatus,
    TradingMode,
)
from trading_system.domain.models import (
    Identifier,
    ImmutableModel,
    Money,
    Ticker,
    UtcDatetime,
)

__all__ = [
    "BudgetAllocated",
    "ContractSelected",
    "DomainEvent",
    "ExitTriggered",
    "NoTradeDecided",
    "OrderFilled",
    "OrderPartiallyFilled",
    "OrderSubmitted",
    "PositionClosed",
    "PositionOpened",
    "PositionStateChanged",
    "ReconciliationChecked",
    "ResearchCompleted",
    "RiskEvaluated",
    "StrategySelected",
    "ThesisUpdated",
    "UniverseSelected",
]


class DomainEvent(ImmutableModel):
    """Base class for all domain events.

    ``occurred_at`` is the time the fact became true, which is not necessarily
    the time it was recorded; both matter for reconstructing a trade.
    """

    event_id: Identifier
    occurred_at: UtcDatetime
    correlation_id: Identifier | None = None


# ---------------------------------------------------------------------------
# Discovery and research
# ---------------------------------------------------------------------------
class UniverseSelected(DomainEvent):
    event_type: Literal["UNIVERSE_SELECTED"] = "UNIVERSE_SELECTED"
    universe_id: Identifier
    candidate_count: int = Field(ge=0)


class ResearchCompleted(DomainEvent):
    event_type: Literal["RESEARCH_COMPLETED"] = "RESEARCH_COMPLETED"
    report_id: Identifier
    ticker: Ticker
    hypothesis: MarketHypothesis
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Strategy and contract
# ---------------------------------------------------------------------------
class StrategySelected(DomainEvent):
    event_type: Literal["STRATEGY_SELECTED"] = "STRATEGY_SELECTED"
    decision_id: Identifier
    ticker: Ticker
    action: StrategyAction
    strategy_type: StrategyType | None = None


class ContractSelected(DomainEvent):
    event_type: Literal["CONTRACT_SELECTED"] = "CONTRACT_SELECTED"
    underlying: Ticker
    strategy_type: StrategyType
    dte: int = Field(ge=0)
    leg_count: int = Field(ge=1)


# ---------------------------------------------------------------------------
# Allocation and risk
# ---------------------------------------------------------------------------
class BudgetAllocated(DomainEvent):
    event_type: Literal["BUDGET_ALLOCATED"] = "BUDGET_ALLOCATED"
    allocation_id: Identifier
    allocated_eur: Money
    reserve_eur: Money


class RiskEvaluated(DomainEvent):
    event_type: Literal["RISK_EVALUATED"] = "RISK_EVALUATED"
    decision_id: Identifier
    purchase_card_id: Identifier
    outcome: RiskOutcome
    reason_codes: list[RiskReasonCode] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
class OrderSubmitted(DomainEvent):
    event_type: Literal["ORDER_SUBMITTED"] = "ORDER_SUBMITTED"
    intent_id: Identifier
    broker: Identifier
    broker_order_id: str | None = None
    trading_mode: TradingMode


class OrderPartiallyFilled(DomainEvent):
    event_type: Literal["ORDER_PARTIALLY_FILLED"] = "ORDER_PARTIALLY_FILLED"
    intent_id: Identifier
    filled_quantity: int = Field(ge=0)
    requested_quantity: int = Field(ge=1)


class OrderFilled(DomainEvent):
    event_type: Literal["ORDER_FILLED"] = "ORDER_FILLED"
    intent_id: Identifier
    filled_quantity: int = Field(ge=1)
    average_fill_price: Money


# ---------------------------------------------------------------------------
# Position lifecycle
# ---------------------------------------------------------------------------
class PositionOpened(DomainEvent):
    event_type: Literal["POSITION_OPENED"] = "POSITION_OPENED"
    position_id: Identifier
    underlying: Ticker
    strategy_type: StrategyType
    quantity: int = Field(ge=1)


class PositionStateChanged(DomainEvent):
    event_type: Literal["POSITION_STATE_CHANGED"] = "POSITION_STATE_CHANGED"
    position_id: Identifier
    from_state: PositionState
    to_state: PositionState
    reason: str | None = None


class ThesisUpdated(DomainEvent):
    """Records new evidence about a thesis.

    The original thesis is never rewritten; this event appends to its history
    (specification section 18).
    """

    event_type: Literal["THESIS_UPDATED"] = "THESIS_UPDATED"
    position_id: Identifier
    previous_status: ThesisStatus
    new_status: ThesisStatus
    evidence: list[str] = Field(default_factory=list)


class ExitTriggered(DomainEvent):
    event_type: Literal["EXIT_TRIGGERED"] = "EXIT_TRIGGERED"
    position_id: Identifier
    reason: ExitReason


class PositionClosed(DomainEvent):
    event_type: Literal["POSITION_CLOSED"] = "POSITION_CLOSED"
    position_id: Identifier
    realized_pnl_eur: Money
    exit_reason: ExitReason


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
class ReconciliationChecked(DomainEvent):
    """Outcome of comparing internal state with broker state.

    A ``MISMATCH`` blocks new executions until it is resolved or explicitly
    classified as safe (specification section 20).
    """

    event_type: Literal["RECONCILIATION_CHECKED"] = "RECONCILIATION_CHECKED"
    status: ReconciliationStatus
    discrepancies: list[str] = Field(default_factory=list)


class NoTradeDecided(DomainEvent):
    """``NO_TRADE`` is a first-class outcome and is recorded, not discarded."""

    event_type: Literal["NO_TRADE_DECIDED"] = "NO_TRADE_DECIDED"
    ticker: Ticker | None = None
    stage: Identifier
    reason: str
