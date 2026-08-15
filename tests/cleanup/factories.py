"""Deterministic builders for the orphan-cleanup suites.

One rule holds throughout: **build the real model, never a partial one.**
``model_construct`` skips validation, so a test built on one can pass while the
artifact it describes could not exist — which is precisely the failure mode this
package's validators are there to catch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from tests.positions.factories import MASKED, versions
from trading_system.domain.enums import (
    BrokerReadStatus,
    ReconciliationFindingType,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    TradingMode,
)
from trading_system.reconciliation.findings import make_finding
from trading_system.reconciliation.models import (
    ReconciliationCounts,
    ReconciliationFinding,
    ReconciliationResult,
)

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


def orphan_finding(
    *,
    key: str,
    contract_id: int | None,
    quantity: str = "1",
    symbol: str = "SMH",
) -> ReconciliationFinding:
    """One ``ORPHAN_BROKER_POSITION``, exactly as the engine emits it."""
    return make_finding(
        ReconciliationFindingType.ORPHAN_BROKER_POSITION,
        severity=lambda _type: ReconciliationSeverity.WARNING,
        identifier=key,
        summary=f"{key}: the broker holds {quantity} contract(s) nothing accounts for",
        observed_value=quantity,
        contract_id=contract_id,
        symbol=symbol,
    )


def reconciliation_result(
    *,
    reconciliation_id: str = "reconciliation-test",
    status: ReconciliationRunStatus = ReconciliationRunStatus.MISMATCH,
    observed_at: datetime = NOW,
    as_of: datetime = NOW,
    findings: Sequence[ReconciliationFinding] | None = None,
    account_reference: str = MASKED,
    position_snapshot_id: str | None = "positions-test",
) -> ReconciliationResult:
    """A complete, valid reconciliation result.

    A ``MISMATCH`` with no findings cannot exist — the model refuses one — so a
    default orphan finding stands in when a caller supplies none and asks for a
    mismatch. That keeps every result here a thing the engine could actually
    have produced.
    """
    if findings is None:
        findings = _findings_for(status)
    positions_read = (
        BrokerReadStatus.UNAVAILABLE
        if status is ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE
        else BrokerReadStatus.OK
    )
    return ReconciliationResult(
        reconciliation_id=reconciliation_id,
        campaign_id="campaign-001",
        as_of=as_of,
        observed_at=observed_at,
        generated_at=observed_at,
        status=status,
        broker="SIMULATOR",
        account_reference=account_reference,
        trading_mode=TradingMode.PAPER,
        position_snapshot_id=position_snapshot_id,
        findings=list(findings),
        positions_read=positions_read,
        counts=ReconciliationCounts(
            mismatches=sum(1 for finding in findings if not finding.agreement)
        ),
        content_hash="0" * 32,
        versions=versions(),
    )


def _findings_for(status: ReconciliationRunStatus) -> list[ReconciliationFinding]:
    """The findings a status requires, because the model refuses one without.

    Every status here is a *claim about the findings*: a ``MISMATCH`` with none
    is not a mismatch, and a ``BROKER_DATA_UNAVAILABLE`` that does not say which
    read failed is not an explanation. Building the real thing keeps these
    tests honest about what the engine can actually produce.
    """
    if status is ReconciliationRunStatus.MISMATCH:
        return [orphan_finding(key="cid:100001", contract_id=100001)]
    if status is ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE:
        return [
            make_finding(
                ReconciliationFindingType.BROKER_DATA_UNAVAILABLE,
                severity=lambda _type: ReconciliationSeverity.CRITICAL,
                identifier="positions@SIMULATOR",
                summary="broker positions could not be read; nothing was compared",
                detail="the gateway refused the connection",
            )
        ]
    return []
