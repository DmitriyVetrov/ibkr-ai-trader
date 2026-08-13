"""Comparing execution records against the orders the broker actually has.

Pure functions over the Milestone 8 execution ledger and the Milestone 2 broker
order model. No broker connection, no repository, no clock.

The cases, and what each one means:

.. code-block:: text

    internal SUBMITTED  + broker OPEN      ORDER_MATCH
    internal SUBMITTED  + broker ABSENT    EXPECTED_ORDER_MISSING (filled? cancelled?)
    internal terminal   + broker OPEN      ORDER_STATE_MISMATCH
    internal UNKNOWN    + broker anything  resolved in unknown.py
    internal FAILED     + broker ANY ORDER FAILED_EXECUTION_HAS_BROKER_ORDER
    broker OPEN, no internal record        ORPHAN_BROKER_ORDER

The fifth is the serious one and it is a Milestone 8 invariant made checkable.
``FAILED`` means the attempt *provably* never left the process — that is the
entire distinction between ``FAILED`` and ``UNKNOWN``. An order at the broker
for a ``FAILED`` execution means one of those two records is wrong about
something that moved money, and this module refuses to guess which: it reports
the contradiction and never relabels the execution.

Absence from the *open* order list is not evidence that nothing was sent. A
filled order, a cancelled order and an order that never existed all look
identical from there, which is why the absent case is a question rather than a
conclusion.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trading_system.domain.enums import (
    LIVE_EXECUTION_STATES,
    BrokerReadStatus,
    ExecutionState,
    ReconciliationFindingType,
)
from trading_system.domain.models import BrokerOrder
from trading_system.execution.models import ExecutionRecord
from trading_system.reconciliation.findings import SeverityLookup, make_finding
from trading_system.reconciliation.models import ReconciliationFinding

__all__ = ["compare_orders", "orders_unavailable_finding"]

#: Execution states in which we believe an order is working at the broker.
#:
#: ``UNKNOWN`` is deliberately absent: it is not a belief that an order is
#: working, it is the absence of a belief either way, and it is resolved in
#: :mod:`.unknown` rather than reported as a mismatch here.
_WORKING = frozenset(
    {
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.CANCEL_PENDING,
        ExecutionState.SUBMISSION_PENDING,
    }
)


def orders_unavailable_finding(
    *, broker: str, status: BrokerReadStatus, detail: str, severity: SeverityLookup
) -> ReconciliationFinding:
    """Say that open orders could not be read, and compare none."""
    return make_finding(
        ReconciliationFindingType.BROKER_DATA_UNAVAILABLE,
        severity=severity,
        identifier=f"orders@{broker}",
        summary=f"broker open orders could not be read ({status.value}); no order was compared",
        detail=(
            f"{detail}. This is not 'the broker has no open orders': an unread list establishes "
            f"nothing, and in particular it cannot resolve an UNKNOWN submission"
        ),
        recommended_action=(
            "ACTION REQUIRED: restore broker connectivity before sending any further order"
        ),
    )


def compare_orders(
    *,
    executions: Sequence[ExecutionRecord],
    orders: Sequence[BrokerOrder],
    orders_status: BrokerReadStatus,
    severity: SeverityLookup,
    broker: str,
    observed_at: datetime | None = None,
    detail: str | None = None,
) -> list[ReconciliationFinding]:
    """Compare every execution record against the broker's open orders."""
    if not orders_status.usable:
        return [
            orders_unavailable_finding(
                broker=broker,
                status=orders_status,
                detail=detail or "no detail supplied",
                severity=severity,
            )
        ]

    by_id = {order.broker_order_id: order for order in orders}
    findings: list[ReconciliationFinding] = []
    claimed: set[str] = set()

    for record in sorted(executions, key=lambda r: r.execution_id):
        order = by_id.get(record.broker_order_id or "")
        if order is not None:
            claimed.add(order.broker_order_id)

        if record.state is ExecutionState.FAILED:
            if order is not None:
                findings.append(_failed_with_order(record, order, severity=severity))
            continue

        if record.state is ExecutionState.UNKNOWN:
            # Not a mismatch. An ambiguous submission is a question, and it is
            # answered in unknown.py from the same broker evidence.
            continue

        if record.state in _WORKING:
            findings.append(
                _working(record, order, severity=severity, broker=broker, observed_at=observed_at)
            )
            continue

        if order is not None and record.state not in LIVE_EXECUTION_STATES:
            findings.append(_terminal_but_open(record, order, severity=severity))

    for order_id in sorted(set(by_id) - claimed):
        findings.append(_orphan(by_id[order_id], severity=severity))

    return findings


def _working(
    record: ExecutionRecord,
    order: BrokerOrder | None,
    *,
    severity: SeverityLookup,
    broker: str,
    observed_at: datetime | None,
) -> ReconciliationFinding:
    if order is None:
        return make_finding(
            ReconciliationFindingType.EXPECTED_ORDER_MISSING,
            severity=severity,
            identifier=record.execution_id,
            summary=(
                f"execution {record.execution_id} is {record.state.value} but the broker reports "
                f"no matching open order"
            ),
            expected_value=record.state.value,
            observed_value="absent from open orders",
            broker_provenance=broker,
            observed_at=observed_at,
            symbol=record.underlying,
            execution_id=record.execution_id,
            allocation_id=record.allocation_id,
            broker_order_id=record.broker_order_id,
            detail=(
                "absence from the open-order list is what a filled order, a cancelled order and "
                "an order that never existed all look like. Nothing is concluded from it here"
            ),
            recommended_action=(
                "ACTION REQUIRED: check the broker's executions for this order before assuming "
                "either outcome"
            ),
        )

    if order.status is not record.broker_status or int(order.filled_quantity) != (
        record.filled_quantity
    ):
        return make_finding(
            ReconciliationFindingType.ORDER_STATE_MISMATCH,
            severity=severity,
            identifier=order.broker_order_id,
            summary=(
                f"order {order.broker_order_id}: internal state {record.state.value} "
                f"({record.filled_quantity} filled), broker reports {order.status.value} "
                f"({order.filled_quantity} filled)"
            ),
            expected_value=f"{record.state.value}/{record.filled_quantity}",
            observed_value=f"{order.status.value}/{order.filled_quantity}",
            expected_provenance=f"execution {record.execution_id}",
            broker_provenance=order.source,
            observed_at=observed_at,
            broker_timestamp=order.updated_at,
            symbol=record.underlying,
            execution_id=record.execution_id,
            allocation_id=record.allocation_id,
            broker_order_id=order.broker_order_id,
            detail=(
                "the broker is authoritative about its own order. The execution record is not "
                "edited here; resolving it is 'execution explain --resolve', which records the "
                "broker's answer as an appended observation"
            ),
            recommended_action="ACTION REQUIRED: resolve this execution against the broker",
        )

    return make_finding(
        ReconciliationFindingType.ORDER_MATCH,
        severity=severity,
        identifier=order.broker_order_id,
        summary=(
            f"order {order.broker_order_id}: internal and broker agree on "
            f"{order.status.value} ({order.filled_quantity} filled)"
        ),
        expected_value=f"{record.state.value}/{record.filled_quantity}",
        observed_value=f"{order.status.value}/{order.filled_quantity}",
        expected_provenance=f"execution {record.execution_id}",
        broker_provenance=order.source,
        observed_at=observed_at,
        broker_timestamp=order.updated_at,
        symbol=record.underlying,
        execution_id=record.execution_id,
        broker_order_id=order.broker_order_id,
    )


def _failed_with_order(
    record: ExecutionRecord, order: BrokerOrder, *, severity: SeverityLookup
) -> ReconciliationFinding:
    """A FAILED execution with an order at the broker. A serious contradiction."""
    return make_finding(
        ReconciliationFindingType.FAILED_EXECUTION_HAS_BROKER_ORDER,
        severity=severity,
        identifier=record.execution_id,
        summary=(
            f"execution {record.execution_id} is recorded FAILED, but the broker has order "
            f"{order.broker_order_id} for it"
        ),
        expected_value="FAILED (nothing was sent)",
        observed_value=f"{order.status.value} at the broker",
        expected_provenance=f"execution {record.execution_id}",
        broker_provenance=order.source,
        broker_timestamp=order.updated_at,
        symbol=record.underlying,
        execution_id=record.execution_id,
        allocation_id=record.allocation_id,
        broker_order_id=order.broker_order_id,
        detail=(
            "FAILED means the attempt provably never left this process — that is the whole "
            "distinction between FAILED and UNKNOWN. An order at the broker means one of the "
            "two records is wrong about something that moved money. The execution is NOT "
            "relabelled: silently changing FAILED to SUBMITTED would erase the evidence that "
            "the two ever disagreed"
        ),
        recommended_action=(
            "ACTION REQUIRED: treat this as a consistency violation. Establish at the broker "
            "what this order is and whether it filled, before any further submission"
        ),
    )


def _terminal_but_open(
    record: ExecutionRecord, order: BrokerOrder, *, severity: SeverityLookup
) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.ORDER_STATE_MISMATCH,
        severity=severity,
        identifier=order.broker_order_id,
        summary=(
            f"execution {record.execution_id} is terminal ({record.state.value}) but the broker "
            f"still reports order {order.broker_order_id} as {order.status.value}"
        ),
        expected_value=record.state.value,
        observed_value=order.status.value,
        expected_provenance=f"execution {record.execution_id}",
        broker_provenance=order.source,
        broker_timestamp=order.updated_at,
        symbol=record.underlying,
        execution_id=record.execution_id,
        allocation_id=record.allocation_id,
        broker_order_id=order.broker_order_id,
        detail="the broker is authoritative: an order it still reports may still fill",
        recommended_action="ACTION REQUIRED: confirm the order's real state at the broker",
    )


def _orphan(order: BrokerOrder, *, severity: SeverityLookup) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.ORPHAN_BROKER_ORDER,
        severity=severity,
        identifier=order.broker_order_id,
        summary=(
            f"the broker has order {order.broker_order_id} ({order.symbol} "
            f"{order.side.value} {order.quantity}) that no internal execution names"
        ),
        observed_value=order.status.value,
        broker_provenance=order.source,
        broker_timestamp=order.updated_at,
        symbol=order.symbol,
        contract_id=order.contract_id,
        broker_order_id=order.broker_order_id,
        detail=(
            "this order is real and may fill. It is reported and nothing more: it is not "
            "cancelled automatically, and this milestone has no path that could cancel it"
        ),
        recommended_action=(
            "ACTION REQUIRED: decide manually what this order is. Nothing here cancels it"
        ),
    )
