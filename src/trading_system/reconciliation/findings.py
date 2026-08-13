"""Building findings, with severity supplied from configuration.

Every comparison module builds its findings through :func:`make_finding`, and
none of them decides how alarming its own finding is. Severity comes from
``config/reconciliation.yaml`` through a lookup passed in by the caller, which
is what keeps a financial judgement — *is a position we believe in that the
broker does not hold an alarm or a note?* — out of a comparison function and in
a file an operator can read and change.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    ReconciliationFindingType,
    ReconciliationSeverity,
)
from trading_system.reconciliation.models import (
    ReconciliationFinding,
    finding_identifier,
)

__all__ = ["SeverityLookup", "delta_of", "make_finding"]

#: How a comparison module asks for a finding's configured severity.
SeverityLookup = Callable[[ReconciliationFindingType], ReconciliationSeverity]


def delta_of(expected: Decimal | None, observed: Decimal | None) -> str | None:
    """Observed minus expected, as an exact string. ``None`` if either is unknown.

    Never zero-fills a missing side: a delta computed against an unknown value
    would look like a measured difference.
    """
    if expected is None or observed is None:
        return None
    return str(observed - expected)


def make_finding(
    finding_type: ReconciliationFindingType,
    *,
    severity: SeverityLookup,
    identifier: str,
    summary: str,
    expected_value: str | None = None,
    observed_value: str | None = None,
    delta: str | None = None,
    expected_provenance: str | None = None,
    broker_provenance: str | None = None,
    observed_at: datetime | None = None,
    broker_timestamp: datetime | None = None,
    symbol: str | None = None,
    contract_id: int | None = None,
    execution_id: str | None = None,
    allocation_id: str | None = None,
    opportunity_id: str | None = None,
    reservation_id: str | None = None,
    broker_order_id: str | None = None,
    detail: str | None = None,
    recommended_action: str | None = None,
) -> ReconciliationFinding:
    """Assemble one finding. Its id is derived from what it says."""
    return ReconciliationFinding(
        finding_id=finding_identifier(
            finding_type=finding_type,
            identifier=identifier,
            expected=expected_value,
            observed=observed_value,
        ),
        finding_type=finding_type,
        severity=severity(finding_type),
        identifier=identifier,
        summary=summary,
        expected_value=expected_value,
        observed_value=observed_value,
        delta=delta,
        expected_provenance=expected_provenance,
        broker_provenance=broker_provenance,
        observed_at=observed_at,
        broker_timestamp=broker_timestamp,
        symbol=symbol,
        contract_id=contract_id,
        execution_id=execution_id,
        allocation_id=allocation_id,
        opportunity_id=opportunity_id,
        reservation_id=reservation_id,
        broker_order_id=broker_order_id,
        detail=detail,
        recommended_action=recommended_action,
    )
