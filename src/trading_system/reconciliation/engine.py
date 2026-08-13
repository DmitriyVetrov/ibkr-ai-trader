"""The deterministic reconciliation engine.

A pure function of its arguments. No broker, no repository, no clock, no model
— the instant is supplied and every input is already-captured state, which is
what makes a stored reconciliation reproducible long after the account has
moved on.

.. code-block:: text

    broker snapshot + open orders + fills      what the broker says
    expected positions + executions + reservations   what we believe
          |
    compare, contract by contract, order by order
          |
    ReconciliationResult

Three properties hold whatever the inputs:

* **It reports; it never repairs.** No branch here adjusts the internal ledger
  to agree, and there is no order path anywhere in the package's import graph.
  ``orders_submitted`` and ``corrective_orders`` are validated to be zero on
  the result.
* **It never reconciles against absent data.** A failed broker read produces
  ``BROKER_DATA_UNAVAILABLE`` and no comparison at all. Comparing an internal
  ledger against an unreadable broker would report every position as missing,
  with total confidence and no basis.
* **``MATCH`` means everything relevant agreed *and* was actually compared.**
  Agreeing with an empty set is not agreement, and the result model refuses to
  record it as one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from trading_system import __version__ as application_version
from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    BrokerReadStatus,
    ReconciliationFindingType,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    TradingMode,
)
from trading_system.domain.models import BrokerOrder, SystemVersions
from trading_system.execution.models import ExecutionRecord
from trading_system.infrastructure.settings import ReconciliationConfig
from trading_system.positions.models import (
    BrokerPositionSnapshot,
    ExpectedPosition,
    ObservedFill,
    StrategyPosition,
)
from trading_system.reconciliation.fills import compare_fills
from trading_system.reconciliation.findings import SeverityLookup, make_finding
from trading_system.reconciliation.models import (
    RECONCILIATION_SCHEMA_VERSION,
    ReconciliationCounts,
    ReconciliationFinding,
    ReconciliationResult,
    reconciliation_identifier,
)
from trading_system.reconciliation.orders import compare_orders
from trading_system.reconciliation.positions import compare_positions, compare_structures
from trading_system.reconciliation.reservations import compare_reservations
from trading_system.reconciliation.unknown import UnknownResolution, unknown_findings
from trading_system.reservations.lifecycle import ReservationOutcome
from trading_system.reservations.models import Reservation

__all__ = ["ReconciliationEngine", "ReconciliationInputs"]


@dataclass(frozen=True, slots=True)
class ReconciliationInputs:
    """Everything one comparison needs, already captured.

    Both sides arrive as data rather than as sources. That is what keeps the
    engine reproducible — and it is also what makes "reconciliation cannot
    trade" structural rather than a matter of care, since there is nothing here
    to trade *with*.
    """

    campaign_id: str
    broker: str
    account_reference: str
    trading_mode: TradingMode
    as_of: datetime
    observed_at: datetime

    # --- broker reality ----------------------------------------------------
    snapshot: BrokerPositionSnapshot
    orders: tuple[BrokerOrder, ...] = ()
    broker_fills: tuple[ObservedFill, ...] = ()
    account_read: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    orders_read: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    fills_read: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    read_detail: str | None = None

    # --- what we believe ---------------------------------------------------
    expected: tuple[ExpectedPosition, ...] = ()
    structures: tuple[StrategyPosition, ...] = ()
    executions: tuple[ExecutionRecord, ...] = ()
    reservations: tuple[tuple[Reservation, ReservationOutcome], ...] = ()
    unknown_resolutions: tuple[UnknownResolution, ...] = ()

    account_snapshot_id: str | None = None
    internal_failures: tuple[str, ...] = ()
    #: Stamped onto the stored record so a past comparison stays replayable
    #: against the policy that was actually in force when it ran.
    config_version: str = "unknown"


class ReconciliationEngine:
    """Compares captured broker state against captured internal state."""

    def __init__(self, config: ReconciliationConfig) -> None:
        self._config = config

    @property
    def config(self) -> ReconciliationConfig:
        return self._config

    def severity_of(self, finding: ReconciliationFindingType) -> ReconciliationSeverity:
        """Configured severity. Never decided here — see ``config/reconciliation.yaml``."""
        return self._config.severity_of(finding)

    def reconcile(self, inputs: ReconciliationInputs) -> ReconciliationResult:
        """Compare, and record what was found."""
        severity: SeverityLookup = self.severity_of
        findings: list[ReconciliationFinding] = []

        for failure in inputs.internal_failures:
            findings.append(
                make_finding(
                    ReconciliationFindingType.INTERNAL_DATA_UNAVAILABLE,
                    severity=severity,
                    identifier="internal",
                    summary="an internal ledger could not be read; nothing was compared from it",
                    detail=failure,
                    observed_at=inputs.observed_at,
                    recommended_action=(
                        "ACTION REQUIRED: repair the internal store before relying on this "
                        "comparison"
                    ),
                )
            )

        if self._config.require_broker_account and not inputs.account_read.usable:
            findings.append(
                make_finding(
                    ReconciliationFindingType.BROKER_DATA_UNAVAILABLE,
                    severity=severity,
                    identifier=f"account@{inputs.broker}",
                    summary=(f"the broker account could not be read ({inputs.account_read.value})"),
                    detail=inputs.read_detail
                    or "no account state was available for this reconciliation",
                    observed_at=inputs.observed_at,
                    recommended_action=(
                        "ACTION REQUIRED: restore broker connectivity and reconcile again"
                    ),
                )
            )

        position_findings = compare_positions(
            expected=inputs.expected,
            snapshot=inputs.snapshot,
            severity=severity,
            observed_at=inputs.observed_at,
        )
        findings.extend(position_findings)
        findings.extend(compare_structures(inputs.structures, severity=severity))

        findings.extend(
            compare_orders(
                executions=inputs.executions,
                orders=inputs.orders,
                orders_status=(
                    inputs.orders_read
                    if self._config.require_broker_orders
                    or inputs.orders_read is not BrokerReadStatus.NOT_REQUESTED
                    else BrokerReadStatus.EMPTY
                ),
                severity=severity,
                broker=inputs.broker,
                observed_at=inputs.observed_at,
                detail=inputs.read_detail,
            )
        )
        findings.extend(unknown_findings(inputs.unknown_resolutions, severity=severity))
        findings.extend(
            compare_fills(
                broker_fills=inputs.broker_fills,
                executions=inputs.executions,
                fills_status=inputs.fills_read,
                severity=severity,
                broker=inputs.broker,
                required=self._config.require_broker_fills,
                complete_history=self._config.treat_broker_fills_as_complete_history,
                observed_at=inputs.observed_at,
                detail=inputs.read_detail,
            )
        )
        findings.extend(
            compare_reservations(
                inputs.reservations,
                expected=inputs.expected,
                severity=severity,
                observed_at=inputs.observed_at,
            )
        )

        counts = _counts(findings, inputs)
        status = _status(findings, inputs)
        digest = _content_digest(findings, inputs)

        return ReconciliationResult(
            reconciliation_id=reconciliation_identifier(
                campaign_id=inputs.campaign_id,
                broker=inputs.broker,
                account_reference=inputs.account_reference,
                as_of=inputs.as_of,
                content_digest=digest,
            ),
            campaign_id=inputs.campaign_id,
            as_of=inputs.as_of,
            observed_at=inputs.observed_at,
            generated_at=inputs.observed_at,
            status=status,
            broker=inputs.broker,
            account_reference=inputs.account_reference,
            trading_mode=inputs.trading_mode,
            account_read=inputs.account_read,
            positions_read=inputs.snapshot.read_status,
            orders_read=inputs.orders_read,
            fills_read=inputs.fills_read,
            position_snapshot_id=inputs.snapshot.snapshot_id,
            account_snapshot_id=inputs.account_snapshot_id,
            execution_ids=sorted(record.execution_id for record in inputs.executions),
            reservation_ids=sorted(
                reservation.reservation_id for reservation, _ in inputs.reservations
            ),
            expected_position_count=len([p for p in inputs.expected if p.quantity != 0]),
            broker_position_count=len(inputs.snapshot.positions),
            findings=findings,
            counts=counts,
            reservations_released=sorted(
                reservation.reservation_id
                for reservation, _ in inputs.reservations
                if reservation.released_amount > 0
            ),
            reservations_consumed=sorted(
                reservation.reservation_id
                for reservation, _ in inputs.reservations
                if reservation.consumed_amount > 0
            ),
            reservations_retained_unknown=sorted(
                reservation.reservation_id
                for reservation, _ in inputs.reservations
                if reservation.locked_by_uncertainty
            ),
            executions_resolved=sorted(
                resolution.execution_id
                for resolution in inputs.unknown_resolutions
                if resolution.resolved
            ),
            orders_submitted=0,
            corrective_orders=0,
            content_hash=digest,
            versions=SystemVersions(
                application_version=application_version,
                config_version=inputs.config_version,
                strategy_spec_version=RECONCILIATION_SCHEMA_VERSION,
            ),
            status_detail=_detail(status, findings),
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _counts(
    findings: Sequence[ReconciliationFinding], inputs: ReconciliationInputs
) -> ReconciliationCounts:
    disagreements = [finding for finding in findings if not finding.agreement]
    # Position findings are the ones identified by a contract key, which is the
    # only identifier shaped `cid:` or `sym:`. Counting them this way keeps the
    # count derived from the findings rather than from a second tally that
    # could disagree with them.
    contracts = {
        finding.identifier
        for finding in findings
        if finding.identifier.startswith(("cid:", "sym:"))
    }
    return ReconciliationCounts(
        positions_compared=len(contracts),
        positions_matched=len(
            [
                finding
                for finding in findings
                if finding.finding_type is ReconciliationFindingType.POSITION_MATCH
            ]
        ),
        orders_compared=len(inputs.orders),
        fills_compared=len(inputs.broker_fills),
        reservations_compared=len(inputs.reservations),
        executions_considered=len(inputs.executions),
        mismatches=len(disagreements),
        critical=len(
            [finding for finding in findings if finding.severity is ReconciliationSeverity.CRITICAL]
        ),
        warnings=len(
            [finding for finding in findings if finding.severity is ReconciliationSeverity.WARNING]
        ),
        unresolved_unknown=len(
            [
                finding
                for finding in findings
                if finding.finding_type is ReconciliationFindingType.UNKNOWN_EXECUTION_UNRESOLVED
            ]
        ),
        orphan_positions=len(
            [
                finding
                for finding in findings
                if finding.finding_type is ReconciliationFindingType.ORPHAN_BROKER_POSITION
            ]
        ),
    )


def _status(
    findings: Sequence[ReconciliationFinding], inputs: ReconciliationInputs
) -> ReconciliationRunStatus:
    """Derive the run's status from what it actually managed to compare."""
    if any(
        finding.finding_type is ReconciliationFindingType.BROKER_DATA_UNAVAILABLE
        for finding in findings
    ):
        return ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE
    if inputs.internal_failures:
        return ReconciliationRunStatus.INTERNAL_DATA_UNAVAILABLE
    if any(not finding.agreement for finding in findings):
        return ReconciliationRunStatus.MISMATCH
    if not inputs.snapshot.usable:
        # Belt and braces: a snapshot that produced no findings at all must
        # still never be reported as agreement.
        return ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE
    return ReconciliationRunStatus.MATCH


def _content_digest(findings: Sequence[ReconciliationFinding], inputs: ReconciliationInputs) -> str:
    """What this reconciliation *found*, with every observation clock excluded.

    The same reasoning as the data layer's payload hash: the question is "is
    this the same comparison?", not "when did we run it". Two runs over
    unchanged broker and internal state produce the same digest, so the second
    is recognisable as a re-observation rather than as new trouble.
    """
    return stable_hash(
        [
            "RECONCILIATION_CONTENT",
            inputs.snapshot.content_hash,
            sorted(finding.finding_id for finding in findings),
            sorted(f"{record.execution_id}:{record.state.value}" for record in inputs.executions),
            sorted(
                f"{reservation.reservation_id}:{reservation.state.value}:"
                f"{reservation.consumed_amount}:{reservation.released_amount}"
                for reservation, _ in inputs.reservations
            ),
        ]
    )


def _detail(
    status: ReconciliationRunStatus, findings: Sequence[ReconciliationFinding]
) -> str | None:
    if status is ReconciliationRunStatus.MATCH:
        return "internal records and broker reality agree. No corrective order was placed."
    critical = [finding for finding in findings if finding.blocking]
    if critical:
        return (
            f"{len(critical)} critical finding(s). ACTION REQUIRED — nothing was corrected "
            f"automatically and no order was placed."
        )
    disagreements = [finding for finding in findings if not finding.agreement]
    if disagreements:
        return (
            f"{len(disagreements)} discrepancy(ies) reported. Nothing was corrected "
            f"automatically and no order was placed."
        )
    return None
