"""Resolving an ambiguous submission by observing the broker.

Milestone 8's central safety invariant is ``UNKNOWN != FAILED``: a submission
whose outcome was never learned may be a live order right now. It is resolved
by *looking*, never by retrying, and never by waiting long enough that it
starts to feel resolved.

This module is the looking. It is pure — the broker's answers are handed to it
— and every branch is about one question: **is this evidence strong enough to
establish what happened?**

.. code-block:: text

    broker reports the order OPEN            -> SUBMITTED       reservation held
    broker reports it PARTIALLY FILLED       -> PARTIALLY_FILLED reservation part-consumed
    broker reports it FILLED                 -> FILLED           reservation consumed
    broker reports it CANCELLED, no fill     -> CANCELLED        reservation released
    a broker FILL exists for the order       -> FILLED / PARTIAL
    absent, and the order list is complete   -> CANCELLED        reservation released
    absent, and completeness is not claimed  -> stays UNKNOWN    reservation held
    the broker could not be read             -> stays UNKNOWN    reservation held

The last three lines are the point. Absence from a list of *open* orders is
what a filled order, a cancelled order and an order that never existed all look
like, so absence alone resolves nothing. IBKR's execution list is
session-scoped, so absence from that resolves nothing either unless the
configuration explicitly claims the list is a complete history — which it ships
not claiming.

Nothing here is optimistic. An unresolvable ``UNKNOWN`` stays ``UNKNOWN``, its
capital stays locked, and the run reports it as the critical finding it is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    ExecutionEventType,
    ExecutionReasonCode,
    ExecutionState,
    OrderSide,
    OrderStatus,
    ReconciliationFindingType,
)
from trading_system.domain.models import BrokerExecution, BrokerOrder, Fill
from trading_system.execution.fill_tracker import (
    event_from_broker_order,
    event_identifier,
    terminal_event_type,
)
from trading_system.execution.models import ExecutionEvent, ExecutionRecord
from trading_system.reconciliation.findings import SeverityLookup, make_finding
from trading_system.reconciliation.models import ReconciliationFinding

__all__ = ["UnknownResolution", "resolve_unknown", "unknown_findings"]


@dataclass(frozen=True, slots=True)
class UnknownResolution:
    """What broker evidence established about one ambiguous submission."""

    record: ExecutionRecord
    resolved: bool
    state: ExecutionState
    detail: str
    event: ExecutionEvent | None = None
    evidence: str = ""

    @property
    def execution_id(self) -> str:
        return self.record.execution_id

    @property
    def finding_type(self) -> ReconciliationFindingType:
        return (
            ReconciliationFindingType.UNKNOWN_EXECUTION_RESOLVED
            if self.resolved
            else ReconciliationFindingType.UNKNOWN_EXECUTION_UNRESOLVED
        )


def resolve_unknown(
    record: ExecutionRecord,
    *,
    orders: Sequence[BrokerOrder],
    fills: Sequence[BrokerExecution],
    orders_readable: bool,
    fills_readable: bool,
    fills_are_complete_history: bool,
    observed_at: datetime,
    sequence: int,
    source: str,
) -> UnknownResolution:
    """Settle one ``UNKNOWN`` execution from broker evidence, or leave it unsettled."""
    if record.state is not ExecutionState.UNKNOWN:
        raise ValueError(
            f"execution {record.execution_id} is {record.state.value}, not UNKNOWN; this module "
            f"resolves ambiguity and has nothing to say about a settled submission"
        )

    if not orders_readable:
        return _unresolved(
            record,
            "broker order state could not be read, so nothing was established. An unread list "
            "is not an empty one, and the capital stays committed",
        )

    matched = _match_order(record, orders)
    if matched is not None:
        event = event_from_broker_order(
            record,
            matched,
            sequence=sequence,
            observed_at=observed_at,
            source=source,
        )
        return UnknownResolution(
            record=record,
            resolved=True,
            state=event.state,
            detail=(
                f"the broker reports order {matched.broker_order_id} as {matched.status.value} "
                f"with {matched.filled_quantity} filled; the execution resolves to "
                f"{event.state.value}"
            ),
            event=event,
            evidence=f"open order {matched.broker_order_id}",
        )

    if record.broker_order_id is None:
        return _unresolved(
            record,
            "this execution has no broker order id, so there is nothing to look up by name. "
            "The submission may still have reached the broker under an id we never received",
        )

    if fills_readable:
        matching = _fills_for(record, fills)
        if matching:
            event = _event_from_fills(
                record, matching, sequence=sequence, observed_at=observed_at, source=source
            )
            return UnknownResolution(
                record=record,
                resolved=True,
                state=event.state,
                detail=(
                    f"the broker reports {len(matching)} execution report(s) for order "
                    f"{record.broker_order_id}; the execution resolves to {event.state.value}"
                ),
                event=event,
                evidence=f"{len(matching)} broker fill(s)",
            )

        if fills_are_complete_history:
            event = _cancelled_event(
                record, sequence=sequence, observed_at=observed_at, source=source
            )
            return UnknownResolution(
                record=record,
                resolved=True,
                state=ExecutionState.CANCELLED,
                detail=(
                    "the order is absent from the open orders and the broker's execution list "
                    "is configured as a complete history containing no fill for it, so absence "
                    "is established"
                ),
                event=event,
                evidence="complete execution history with no matching fill",
            )

    return _unresolved(
        record,
        "the order is absent from the open orders and no fill was found for it. Absence from "
        "the open list is what a filled, a cancelled and a never-sent order all look like, and "
        "the broker's execution list is session-scoped, so neither establishes absence. The "
        "execution stays UNKNOWN and its capital stays committed",
    )


def unknown_findings(
    resolutions: Sequence[UnknownResolution], *, severity: SeverityLookup
) -> list[ReconciliationFinding]:
    """One finding per ambiguous submission, resolved or not."""
    findings: list[ReconciliationFinding] = []
    for resolution in resolutions:
        record = resolution.record
        if resolution.resolved:
            findings.append(
                make_finding(
                    ReconciliationFindingType.UNKNOWN_EXECUTION_RESOLVED,
                    severity=severity,
                    identifier=record.execution_id,
                    summary=(
                        f"execution {record.execution_id} was UNKNOWN and broker evidence "
                        f"resolves it to {resolution.state.value}"
                    ),
                    expected_value="UNKNOWN",
                    observed_value=resolution.state.value,
                    expected_provenance=f"execution {record.execution_id}",
                    broker_provenance=resolution.evidence,
                    symbol=record.underlying,
                    execution_id=record.execution_id,
                    allocation_id=record.allocation_id,
                    broker_order_id=record.broker_order_id,
                    detail=resolution.detail,
                )
            )
            continue

        findings.append(
            make_finding(
                ReconciliationFindingType.UNKNOWN_EXECUTION_UNRESOLVED,
                severity=severity,
                identifier=record.execution_id,
                summary=(
                    f"execution {record.execution_id} is UNKNOWN and broker evidence does not "
                    f"settle it"
                ),
                expected_value="UNKNOWN",
                observed_value="not established",
                expected_provenance=f"execution {record.execution_id}",
                symbol=record.underlying,
                execution_id=record.execution_id,
                allocation_id=record.allocation_id,
                broker_order_id=record.broker_order_id,
                detail=resolution.detail,
                recommended_action=(
                    "ACTION REQUIRED: an order may be live at the broker. Its campaign capital "
                    "stays committed and must not be re-authorised. Do NOT resubmit; establish "
                    "the order's real state at the broker"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _unresolved(record: ExecutionRecord, detail: str) -> UnknownResolution:
    return UnknownResolution(
        record=record,
        resolved=False,
        state=ExecutionState.UNKNOWN,
        detail=detail,
    )


def _match_order(record: ExecutionRecord, orders: Sequence[BrokerOrder]) -> BrokerOrder | None:
    """Match by broker order id only.

    Deliberately not a fuzzy match on symbol, side and quantity. Two orders for
    the same contract on the same day are ordinary, and resolving an ambiguous
    submission against the wrong one would record a fill that belongs to a
    different trade.
    """
    if not record.broker_order_id:
        return None
    return next(
        (order for order in orders if order.broker_order_id == record.broker_order_id), None
    )


def _fills_for(record: ExecutionRecord, fills: Sequence[BrokerExecution]) -> list[BrokerExecution]:
    if not record.broker_order_id:
        return []
    return sorted(
        (fill for fill in fills if fill.broker_order_id == record.broker_order_id),
        key=lambda fill: (fill.executed_at, fill.execution_id),
    )


def _event_from_fills(
    record: ExecutionRecord,
    fills: Sequence[BrokerExecution],
    *,
    sequence: int,
    observed_at: datetime,
    source: str,
) -> ExecutionEvent:
    """Build the resolving event from the broker's own execution reports.

    Quantity is summed from the reports and capped at what was submitted — a
    broker cannot fill more than was sent, so a larger total is a tracking
    fault rather than a market event, and capping keeps the record
    constructible while the contradiction stays visible in the fills.
    """
    total = sum((fill.quantity for fill in fills), Decimal("0"))
    filled = min(int(total), record.quantity) if record.quantity else int(total)
    value = sum((fill.price * fill.quantity for fill in fills), Decimal("0"))
    average = value / total if total > 0 else None
    state = (
        ExecutionState.FILLED
        if record.quantity and filled >= record.quantity
        else ExecutionState.PARTIALLY_FILLED
    )
    status = OrderStatus.FILLED if state is ExecutionState.FILLED else OrderStatus.PARTIALLY_FILLED
    return ExecutionEvent(
        event_id=event_identifier(
            execution_id=record.execution_id,
            sequence=sequence,
            event_type=f"resolve-{state.value}",
        ),
        execution_id=record.execution_id,
        sequence=sequence,
        event_type=terminal_event_type(state),
        state=state,
        occurred_at=fills[-1].executed_at,
        observed_at=observed_at,
        source=source,
        reason_code=ExecutionReasonCode.OK,
        detail=(
            f"resolved from {len(fills)} broker execution report(s) for order "
            f"{record.broker_order_id}"
        ),
        broker_order_id=record.broker_order_id,
        broker_status=status,
        filled_quantity=filled,
        remaining_quantity=max(record.quantity - filled, 0) if record.quantity else None,
        average_fill_price=average if average and average > 0 else None,
        last_fill_price=fills[-1].price,
        fills=[
            Fill(
                fill_id=fill.execution_id,
                leg_index=0,
                quantity=int(fill.quantity),
                price=fill.price,
                commission=fill.commission if fill.commission is not None else Decimal("0"),
                filled_at=fill.executed_at,
            )
            for fill in fills
            if fill.quantity >= 1 and fill.side in (OrderSide.BUY, OrderSide.SELL)
        ],
        broker_timestamp=fills[-1].as_of,
    )


def _cancelled_event(
    record: ExecutionRecord,
    *,
    sequence: int,
    observed_at: datetime,
    source: str,
) -> ExecutionEvent:
    """The order is gone and no fill exists for it, on evidence strong enough to say so."""
    return ExecutionEvent(
        event_id=event_identifier(
            execution_id=record.execution_id,
            sequence=sequence,
            event_type="resolve-CANCELLED",
        ),
        execution_id=record.execution_id,
        sequence=sequence,
        event_type=ExecutionEventType.EXECUTION_CANCELLED,
        state=ExecutionState.CANCELLED,
        occurred_at=observed_at,
        observed_at=observed_at,
        source=source,
        reason_code=ExecutionReasonCode.UNKNOWN_BROKER_STATE,
        detail=(
            "the broker reports neither an open order nor any fill for this submission, and the "
            "execution list is configured as a complete history, so absence is established"
        ),
        broker_order_id=record.broker_order_id,
        broker_status=OrderStatus.CANCELLED,
        filled_quantity=0,
    )
