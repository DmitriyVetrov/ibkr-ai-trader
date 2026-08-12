"""Turn what the broker said into what we record.

Pure functions over broker-sourced domain models. Nothing here talks to a
broker, and nothing here *decides* anything — it translates an observation into
an :class:`~trading_system.execution.models.ExecutionEvent`, which the record
then folds in.

Two rules, and both are ways a system claims a position it does not have:

* **A fill comes from a fill report, never from anything else.** Not from the
  submitted quantity, not from an acknowledgement, not from the fact that a
  limit order was priced to fill. If the broker reported no quantity, the
  quantity recorded is the quantity already known — never the quantity hoped
  for.
* **Numbers outrank labels.** Where a broker's status says ``FILLED`` and its
  own counts say three of ten, the counts win and the state is
  ``PARTIALLY_FILLED``. The disagreement is recorded rather than resolved
  silently, because one of the two is a bug and hiding it loses the evidence.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    ExecutionEventType,
    ExecutionReasonCode,
    ExecutionState,
    OrderStatus,
)
from trading_system.domain.models import BrokerOrder, ExecutionResult
from trading_system.execution.models import (
    EXECUTION_SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionRecord,
)

__all__ = [
    "event_from_broker_order",
    "event_from_execution_result",
    "event_identifier",
    "state_for",
    "terminal_event_type",
]


def event_identifier(*, execution_id: str, sequence: int, event_type: str) -> str:
    """Derive one event's identity, so a replayed observation does not duplicate."""
    digest = stable_hash(
        ["EXECUTION_EVENT", EXECUTION_SCHEMA_VERSION, execution_id, sequence, event_type]
    )
    return f"execevt-{digest[:20]}"


def state_for(
    status: OrderStatus | None,
    *,
    filled_quantity: int,
    submitted_quantity: int,
    has_broker_order_id: bool,
) -> ExecutionState:
    """Map a broker's answer onto an execution state.

    The mapping is deliberately conservative in both directions. An order the
    broker acknowledged without naming is ``UNKNOWN`` rather than ``SUBMITTED``:
    an order we cannot identify is one we cannot cancel or reconcile, and
    recording it as tracked would be a claim we cannot honour. An unrecognised
    status is ``UNKNOWN`` rather than anything reassuring.
    """
    if status is None:
        return ExecutionState.UNKNOWN

    if status in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED):
        # Counts outrank the label: a broker that reports a fill while still
        # calling the order "submitted" has filled it.
        if filled_quantity <= 0:
            return ExecutionState.SUBMITTED if has_broker_order_id else ExecutionState.UNKNOWN
        if submitted_quantity and filled_quantity >= submitted_quantity:
            return ExecutionState.FILLED
        return ExecutionState.PARTIALLY_FILLED

    if status is OrderStatus.FILLED:
        if submitted_quantity and filled_quantity < submitted_quantity:
            # The broker contradicted itself. Believe the counts: reporting a
            # partial as FILLED would claim a whole structure that is not there.
            return (
                ExecutionState.PARTIALLY_FILLED
                if filled_quantity > 0
                else (ExecutionState.SUBMITTED if has_broker_order_id else ExecutionState.UNKNOWN)
            )
        return ExecutionState.FILLED

    if status is OrderStatus.PARTIALLY_FILLED:
        if submitted_quantity and filled_quantity >= submitted_quantity:
            return ExecutionState.FILLED
        return ExecutionState.PARTIALLY_FILLED if filled_quantity > 0 else ExecutionState.SUBMITTED

    if status is OrderStatus.CANCELLED:
        # A cancelled order that partly filled leaves a real, smaller position.
        # The state is CANCELLED and the filled quantity is preserved; the two
        # together are the whole truth and neither alone is.
        return ExecutionState.CANCELLED

    if status is OrderStatus.REJECTED:
        return ExecutionState.REJECTED

    return ExecutionState.UNKNOWN


def terminal_event_type(state: ExecutionState) -> ExecutionEventType:
    """The event type that corresponds to arriving in ``state``."""
    return {
        ExecutionState.SUBMISSION_PENDING: ExecutionEventType.EXECUTION_SUBMISSION_PENDING,
        ExecutionState.SUBMITTED: ExecutionEventType.EXECUTION_SUBMITTED,
        ExecutionState.PARTIALLY_FILLED: ExecutionEventType.EXECUTION_PARTIAL_FILL,
        ExecutionState.FILLED: ExecutionEventType.EXECUTION_FILLED,
        ExecutionState.CANCEL_PENDING: ExecutionEventType.EXECUTION_CANCEL_REQUESTED,
        ExecutionState.CANCELLED: ExecutionEventType.EXECUTION_CANCELLED,
        ExecutionState.REJECTED: ExecutionEventType.EXECUTION_REJECTED,
        ExecutionState.EXPIRED: ExecutionEventType.EXECUTION_EXPIRED,
        ExecutionState.FAILED: ExecutionEventType.EXECUTION_FAILED,
        ExecutionState.UNKNOWN: ExecutionEventType.EXECUTION_STATE_UNKNOWN,
        ExecutionState.CREATED: ExecutionEventType.EXECUTION_CREATED,
        ExecutionState.VALIDATED: ExecutionEventType.EXECUTION_VALIDATED,
    }[state]


def event_from_execution_result(
    record: ExecutionRecord,
    result: ExecutionResult,
    *,
    sequence: int,
    observed_at: datetime,
    source: str,
) -> ExecutionEvent:
    """Build the event for what the broker returned from a submission."""
    filled = min(result.filled_quantity, record.quantity) if record.quantity else 0
    state = state_for(
        result.status,
        filled_quantity=filled,
        submitted_quantity=record.quantity,
        has_broker_order_id=bool(result.broker_order_id),
    )
    detail = _contradiction(result.status, filled, record.quantity)
    return ExecutionEvent(
        event_id=event_identifier(
            execution_id=record.execution_id, sequence=sequence, event_type=state.value
        ),
        execution_id=record.execution_id,
        sequence=sequence,
        event_type=terminal_event_type(state),
        state=state,
        occurred_at=result.submitted_at or result.last_update_at,
        observed_at=observed_at,
        source=source,
        reason_code=_reason_for(state),
        detail=detail or result.message,
        broker_order_id=result.broker_order_id,
        broker_status=result.status,
        filled_quantity=filled,
        remaining_quantity=max(record.quantity - filled, 0) if record.quantity else None,
        average_fill_price=result.average_fill_price,
        last_fill_price=result.fills[-1].price if result.fills else None,
        fills=list(result.fills),
        broker_message=result.message,
        broker_timestamp=result.last_update_at,
    )


def event_from_broker_order(
    record: ExecutionRecord,
    order: BrokerOrder,
    *,
    sequence: int,
    observed_at: datetime,
    source: str,
) -> ExecutionEvent:
    """Build the event for an order state observed at the broker.

    Used when resolving an uncertain submission or confirming a cancellation:
    the broker is asked what it has, and whatever it says becomes the record.
    """
    filled = int(order.filled_quantity)
    if record.quantity:
        filled = min(filled, record.quantity)
    state = state_for(
        order.status,
        filled_quantity=filled,
        submitted_quantity=record.quantity,
        has_broker_order_id=bool(order.broker_order_id),
    )
    remaining = int(order.remaining_quantity) if order.remaining_quantity is not None else None
    event_type = (
        ExecutionEventType.EXECUTION_OBSERVED
        if state is record.state
        else terminal_event_type(state)
    )
    return ExecutionEvent(
        event_id=event_identifier(
            execution_id=record.execution_id, sequence=sequence, event_type=f"observe-{state.value}"
        ),
        execution_id=record.execution_id,
        sequence=sequence,
        event_type=event_type,
        state=state,
        occurred_at=order.updated_at or order.as_of,
        observed_at=observed_at,
        source=source,
        reason_code=_reason_for(state),
        detail=_contradiction(order.status, filled, record.quantity),
        broker_order_id=order.broker_order_id,
        broker_status=order.status,
        filled_quantity=filled,
        remaining_quantity=remaining,
        average_fill_price=_positive(order.average_fill_price),
        broker_message=None,
        broker_timestamp=order.updated_at,
    )


def _reason_for(state: ExecutionState) -> ExecutionReasonCode | None:
    if state is ExecutionState.REJECTED:
        return ExecutionReasonCode.BROKER_REJECTED
    if state is ExecutionState.UNKNOWN:
        return ExecutionReasonCode.UNKNOWN_BROKER_STATE
    if state in (ExecutionState.SUBMITTED, ExecutionState.PARTIALLY_FILLED, ExecutionState.FILLED):
        return ExecutionReasonCode.OK
    return None


def _contradiction(status: OrderStatus | None, filled: int, submitted: int) -> str | None:
    """Record a broker that disagreed with itself, rather than smoothing it over."""
    if status is OrderStatus.FILLED and submitted and filled < submitted:
        return (
            f"broker reported status FILLED with {filled} of {submitted} units filled. The "
            f"counts are believed; reporting this as FILLED would claim a position that does "
            f"not exist"
        )
    return None


def _positive(value: Decimal | None) -> Decimal | None:
    """A non-positive average fill price is absence of information, not a price."""
    if value is None or value <= 0:
        return None
    return value
