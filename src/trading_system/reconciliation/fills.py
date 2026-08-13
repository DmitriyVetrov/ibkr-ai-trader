"""Comparing recorded fills against the broker's own execution reports.

Pure functions. The asymmetry here is deliberate and is the module's whole
subtlety: **IBKR's execution list is session-scoped.** It reports what traded in
the current session, not the account's history. So:

* a fill the broker reports that no execution of ours accounts for is a real
  discrepancy — ``ORPHAN_BROKER_FILL``;
* a fill *we* recorded that the broker's list does not contain is usually just
  a fill from an earlier session, and calling it a mismatch would produce a
  false alarm every morning.

The second case becomes a genuine ``FILL_MISMATCH`` only when the
configuration explicitly claims the broker's list is a complete history
(``treat_broker_fills_as_complete_history``), which it ships not claiming. That
key exists so the assumption is visible and switchable rather than baked into a
comparison nobody reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trading_system.domain.enums import BrokerReadStatus, ReconciliationFindingType
from trading_system.execution.models import ExecutionRecord
from trading_system.positions.models import ObservedFill
from trading_system.reconciliation.findings import SeverityLookup, make_finding
from trading_system.reconciliation.models import ReconciliationFinding

__all__ = ["compare_fills", "fills_unavailable_finding"]


def fills_unavailable_finding(
    *, broker: str, status: BrokerReadStatus, detail: str, severity: SeverityLookup
) -> ReconciliationFinding:
    """Say that the broker's execution reports could not be read."""
    return make_finding(
        ReconciliationFindingType.BROKER_DATA_UNAVAILABLE,
        severity=severity,
        identifier=f"fills@{broker}",
        summary=f"broker execution reports could not be read ({status.value})",
        detail=(
            f"{detail}. No fill was compared. In particular, this cannot be read as 'the "
            f"account has not traded'"
        ),
        recommended_action="ACTION REQUIRED: restore broker connectivity and reconcile again",
    )


def compare_fills(
    *,
    broker_fills: Sequence[ObservedFill],
    executions: Sequence[ExecutionRecord],
    fills_status: BrokerReadStatus,
    severity: SeverityLookup,
    broker: str,
    required: bool = False,
    complete_history: bool = False,
    observed_at: datetime | None = None,
    detail: str | None = None,
) -> list[ReconciliationFinding]:
    """Compare the broker's execution reports against our execution ledger."""
    if not fills_status.usable:
        if required:
            return [
                fills_unavailable_finding(
                    broker=broker,
                    status=fills_status,
                    detail=detail or "no detail supplied",
                    severity=severity,
                )
            ]
        return []

    findings: list[ReconciliationFinding] = []
    by_order: dict[str, list[ObservedFill]] = {}
    for fill in broker_fills:
        by_order.setdefault(fill.broker_order_id or "", []).append(fill)

    for fill in sorted(broker_fills, key=lambda f: (f.executed_at, f.fill_id)):
        if fill.execution_id:
            findings.append(_matched(fill, severity=severity))
            continue
        findings.append(_orphan(fill, severity=severity, broker=broker))

    if complete_history:
        for record in sorted(executions, key=lambda r: r.execution_id):
            if record.filled_quantity <= 0 or not record.broker_order_id:
                continue
            if by_order.get(record.broker_order_id):
                continue
            findings.append(
                _claimed_but_absent(record, severity=severity, broker=broker, at=observed_at)
            )

    return findings


def _matched(fill: ObservedFill, *, severity: SeverityLookup) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.FILL_MATCH,
        severity=severity,
        identifier=fill.fill_id,
        summary=(
            f"{fill.underlying}: broker fill {fill.broker_execution_id or fill.fill_id} "
            f"({fill.side.value} {fill.quantity} @ {fill.price}) is accounted for by execution "
            f"{fill.execution_id}"
        ),
        expected_value=f"{fill.side.value} {fill.quantity} @ {fill.price}",
        observed_value=f"{fill.side.value} {fill.quantity} @ {fill.price}",
        expected_provenance=f"execution {fill.execution_id}",
        broker_provenance=fill.broker_source,
        observed_at=fill.observed_at,
        broker_timestamp=fill.broker_timestamp,
        symbol=fill.underlying,
        contract_id=fill.contract_id,
        execution_id=fill.execution_id,
        broker_order_id=fill.broker_order_id,
    )


def _orphan(fill: ObservedFill, *, severity: SeverityLookup, broker: str) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.ORPHAN_BROKER_FILL,
        severity=severity,
        identifier=fill.fill_id,
        summary=(
            f"{fill.underlying}: the broker reports a fill ({fill.side.value} {fill.quantity} @ "
            f"{fill.price}) that no internal execution accounts for"
        ),
        observed_value=f"{fill.side.value} {fill.quantity} @ {fill.price}",
        broker_provenance=fill.broker_source or broker,
        observed_at=fill.observed_at,
        broker_timestamp=fill.broker_timestamp,
        symbol=fill.underlying,
        contract_id=fill.contract_id,
        broker_order_id=fill.broker_order_id,
        detail=(
            "the fill is real — the broker reported it — and it is recorded with acquisition "
            "provenance UNKNOWN. No execution, allocation or strategy is invented to explain it"
        ),
        recommended_action=(
            "ACTION REQUIRED: identify this trade manually. It may be a manual trade in the "
            "same account, or an execution this system failed to record"
        ),
    )


def _claimed_but_absent(
    record: ExecutionRecord,
    *,
    severity: SeverityLookup,
    broker: str,
    at: datetime | None,
) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.FILL_MISMATCH,
        severity=severity,
        identifier=record.execution_id,
        summary=(
            f"execution {record.execution_id} records {record.filled_quantity} filled unit(s), "
            f"but the broker's execution history contains no fill for order "
            f"{record.broker_order_id}"
        ),
        expected_value=f"{record.filled_quantity} filled",
        observed_value="no broker fill",
        expected_provenance=f"execution {record.execution_id}",
        broker_provenance=broker,
        observed_at=at,
        symbol=record.underlying,
        execution_id=record.execution_id,
        allocation_id=record.allocation_id,
        broker_order_id=record.broker_order_id,
        detail=(
            "reported because the broker's execution list is configured as a complete history. "
            "Without that claim this would be an ordinary session boundary rather than a "
            "discrepancy"
        ),
        recommended_action=(
            "ACTION REQUIRED: a recorded fill the broker does not know about must not be "
            "treated as a position"
        ),
    )
