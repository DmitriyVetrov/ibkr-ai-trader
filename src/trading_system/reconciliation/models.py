"""Canonical reconciliation artifacts.

Reconciliation compares what this system believes against what the broker
reports. It produces **findings**, and a finding is a statement of fact with
both sides attached — never a verdict about what to do next.

:class:`ReconciliationFinding`
    One specific disagreement, or one specific agreement. Carries the contract
    identity, the expected value, the observed value, the difference, both
    provenances and both clocks. "Positions differ" is not an actionable
    statement; "expected +2 NVDA 2026-09-18 180 CALL, broker holds +1,
    difference -1, expected from execution-abc, broker observed 14:31:02" is.
:class:`ReconciliationResult`
    The immutable record of one comparison: what was read, what was compared,
    what disagreed, and what it did to the campaign's committed capital.
:class:`ReconciliationEvent`
    One appended step in a reconciliation's own history.

Two fields are structurally zero and validated as such:
``orders_submitted`` and ``corrective_orders``. Reconciliation is read-only
with respect to the broker, and a record that could claim otherwise would make
the claim unfalsifiable. They are read off the broker's own counter rather than
asserted, so the zero is evidence.

``severity`` is copied from configuration onto every finding rather than
computed here. How alarming a missing position is depends on how the account is
operated, and a severity policy invented in a module would be a financial
judgement nobody could see or change.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    AGREEMENT_FINDINGS,
    BrokerReadStatus,
    DiscrepancyType,
    ReconciliationEventType,
    ReconciliationFindingType,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    ReconciliationStatus,
    TradingMode,
)
from trading_system.domain.models import (
    Identifier,
    ImmutableModel,
    ReconciliationDiscrepancy,
    ReconciliationReport,
    SystemVersions,
    UtcDatetime,
)

__all__ = [
    "RECONCILIATION_SCHEMA_VERSION",
    "ReconciliationCounts",
    "ReconciliationEvent",
    "ReconciliationFinding",
    "ReconciliationResult",
    "finding_identifier",
    "reconciliation_event_identifier",
    "reconciliation_identifier",
]

#: Bumped when a stored reconciliation artifact changes shape.
RECONCILIATION_SCHEMA_VERSION = "1.0.0"

#: How each finding projects onto the Milestone 1 discrepancy vocabulary.
#:
#: The Milestone 9 vocabulary is deliberately finer. This mapping is what lets
#: the narrow Milestone 1 boundary keep working unchanged — the same technique
#: research, strategy and execution use to project onto their M1 shapes.
_M1_DISCREPANCY: dict[ReconciliationFindingType, DiscrepancyType] = {
    ReconciliationFindingType.EXPECTED_POSITION_MISSING: DiscrepancyType.POSITION_MISMATCH,
    ReconciliationFindingType.ORPHAN_BROKER_POSITION: DiscrepancyType.POSITION_MISMATCH,
    ReconciliationFindingType.POSITION_QUANTITY_MISMATCH: DiscrepancyType.POSITION_MISMATCH,
    ReconciliationFindingType.POSITION_CONTRACT_MISMATCH: DiscrepancyType.POSITION_MISMATCH,
    ReconciliationFindingType.PARTIAL_STRUCTURE: DiscrepancyType.POSITION_MISMATCH,
    ReconciliationFindingType.ORDER_STATE_MISMATCH: DiscrepancyType.ORDER_MISMATCH,
    ReconciliationFindingType.ORPHAN_BROKER_ORDER: DiscrepancyType.ORDER_MISMATCH,
    ReconciliationFindingType.EXPECTED_ORDER_MISSING: DiscrepancyType.ORDER_MISMATCH,
    ReconciliationFindingType.FAILED_EXECUTION_HAS_BROKER_ORDER: DiscrepancyType.ORDER_MISMATCH,
    ReconciliationFindingType.UNKNOWN_EXECUTION_UNRESOLVED: DiscrepancyType.ORDER_MISMATCH,
    ReconciliationFindingType.FILL_MISMATCH: DiscrepancyType.EXECUTION_MISMATCH,
    ReconciliationFindingType.ORPHAN_BROKER_FILL: DiscrepancyType.EXECUTION_MISMATCH,
    ReconciliationFindingType.RESERVATION_MISMATCH: DiscrepancyType.ACCOUNT_MISMATCH,
    ReconciliationFindingType.RESERVATION_RETAINED_UNKNOWN: DiscrepancyType.ACCOUNT_MISMATCH,
    ReconciliationFindingType.CURRENCY_MISMATCH: DiscrepancyType.ACCOUNT_MISMATCH,
    ReconciliationFindingType.BROKER_DATA_UNAVAILABLE: DiscrepancyType.UNKNOWN,
    ReconciliationFindingType.INTERNAL_DATA_UNAVAILABLE: DiscrepancyType.UNKNOWN,
}


def finding_identifier(
    *,
    finding_type: ReconciliationFindingType,
    identifier: str,
    expected: str | None,
    observed: str | None,
    schema_version: str = RECONCILIATION_SCHEMA_VERSION,
) -> str:
    """Derive a finding's identity from what it says.

    Content-derived and clock-free, so the same disagreement observed twice
    produces the same finding id. That is what makes a repeated reconciliation
    recognisable as a repeat rather than as new trouble.
    """
    digest = stable_hash(
        [
            "RECONCILIATION_FINDING",
            schema_version,
            finding_type.value,
            identifier,
            expected or "",
            observed or "",
        ]
    )
    return f"finding-{digest[:20]}"


def reconciliation_identifier(
    *,
    campaign_id: str,
    broker: str,
    account_reference: str,
    as_of: datetime,
    content_digest: str,
    schema_version: str = RECONCILIATION_SCHEMA_VERSION,
) -> str:
    """Derive a reconciliation's id from what it compared and what it found.

    The content digest excludes every observation clock, exactly as the data
    layer's snapshot identity does: reconciling unchanged state again is a
    *re-observation* of one comparison, not a second comparison that happens to
    agree. The instant stays in the id so two genuinely separate observations
    of the same state remain distinguishable.
    """
    digest = stable_hash(
        [
            "RECONCILIATION",
            schema_version,
            campaign_id,
            broker,
            account_reference,
            as_of.isoformat(),
            content_digest,
        ]
    )
    return f"reconciliation-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}"


def reconciliation_event_identifier(
    *,
    reconciliation_id: str,
    sequence: int,
    event_type: str,
    schema_version: str = RECONCILIATION_SCHEMA_VERSION,
) -> str:
    """Derive one event's identity, so a replayed step does not duplicate."""
    digest = stable_hash(
        ["RECONCILIATION_EVENT", schema_version, reconciliation_id, sequence, event_type]
    )
    return f"recevt-{digest[:20]}"


class ReconciliationFinding(ImmutableModel):
    """One thing reconciliation established, with both sides attached.

    Deliberately verbose. A finding is read by a person deciding whether to
    intervene in a trading account, and every field here is one they would
    otherwise have to reconstruct by hand from three ledgers.
    """

    finding_id: Identifier
    finding_type: ReconciliationFindingType
    severity: ReconciliationSeverity
    #: What the finding is about: a contract key, an order id, an execution id
    #: or a reservation id, depending on the type.
    identifier: Identifier
    summary: str = Field(min_length=1)
    schema_version: Identifier = RECONCILIATION_SCHEMA_VERSION

    # --- both sides, verbatim ---------------------------------------------
    expected_value: str | None = None
    observed_value: str | None = None
    #: Observed minus expected, where both are numbers. Recorded rather than
    #: left to the reader: "expected 10, broker 7" and "difference -3" are the
    #: same fact, and the second is the one people act on.
    delta: str | None = None
    expected_provenance: str | None = None
    broker_provenance: str | None = None
    observed_at: UtcDatetime | None = None
    broker_timestamp: UtcDatetime | None = None

    # --- what it concerns --------------------------------------------------
    symbol: str | None = None
    contract_id: int | None = None
    execution_id: Identifier | None = None
    allocation_id: Identifier | None = None
    opportunity_id: Identifier | None = None
    reservation_id: Identifier | None = None
    broker_order_id: str | None = None

    detail: str | None = None
    #: What a person should do. Never an instruction to trade — this milestone
    #: reports, and deciding what to do about a discrepancy is a later one's
    #: job, or a human's.
    recommended_action: str | None = None

    @model_validator(mode="after")
    def _a_disagreement_shows_both_sides(self) -> ReconciliationFinding:
        """A mismatch that names only one side is not actionable.

        The whole value of a finding is that a person can see what we believed
        and what the broker said without opening three ledgers, so a
        quantity-style mismatch is required to carry both.
        """
        two_sided = {
            ReconciliationFindingType.POSITION_QUANTITY_MISMATCH,
            ReconciliationFindingType.ORDER_STATE_MISMATCH,
            ReconciliationFindingType.FILL_MISMATCH,
        }
        if self.finding_type in two_sided and (
            self.expected_value is None or self.observed_value is None
        ):
            raise ValueError(
                f"{self.finding_type.value} must record both the expected and the observed "
                f"value; a mismatch with one side missing cannot be acted on"
            )
        return self

    @property
    def agreement(self) -> bool:
        """Whether this finding records agreement rather than a discrepancy."""
        return self.finding_type in AGREEMENT_FINDINGS

    @property
    def blocking(self) -> bool:
        return self.severity is ReconciliationSeverity.CRITICAL

    def to_discrepancy(self) -> ReconciliationDiscrepancy:
        """Project onto the Milestone 1 boundary.

        The full finding is the Milestone 9 artifact; this is the narrow
        contract the rest of the system was built against. An agreement has no
        Milestone 1 shape at all — that vocabulary only describes disagreement
        — so projecting one raises rather than inventing a discrepancy that
        does not exist.
        """
        kind = _M1_DISCREPANCY.get(self.finding_type)
        if kind is None:
            raise ValueError(
                f"{self.finding_type.value} records agreement and has no Milestone 1 "
                f"discrepancy; a report of agreement is not a discrepancy of any kind"
            )
        return ReconciliationDiscrepancy(
            discrepancy_type=kind,
            identifier=self.identifier,
            description=self.summary,
            internal_value=self.expected_value,
            broker_value=self.observed_value,
        )


class ReconciliationCounts(ImmutableModel):
    """How much was compared, and how much of it agreed."""

    positions_compared: int = Field(default=0, ge=0)
    positions_matched: int = Field(default=0, ge=0)
    orders_compared: int = Field(default=0, ge=0)
    fills_compared: int = Field(default=0, ge=0)
    reservations_compared: int = Field(default=0, ge=0)
    executions_considered: int = Field(default=0, ge=0)

    mismatches: int = Field(default=0, ge=0)
    critical: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    unresolved_unknown: int = Field(default=0, ge=0)
    orphan_positions: int = Field(default=0, ge=0)


class ReconciliationResult(ImmutableModel):
    """The immutable record of one comparison against broker reality."""

    reconciliation_id: Identifier
    campaign_id: Identifier
    as_of: UtcDatetime
    observed_at: UtcDatetime
    generated_at: UtcDatetime
    status: ReconciliationRunStatus
    schema_version: Identifier = RECONCILIATION_SCHEMA_VERSION

    broker: Identifier
    account_reference: Identifier
    trading_mode: TradingMode

    # --- what was actually read -------------------------------------------
    account_read: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    positions_read: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    orders_read: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    fills_read: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED

    # --- what it compared, by id ------------------------------------------
    position_snapshot_id: Identifier | None = None
    account_snapshot_id: Identifier | None = None
    execution_ids: list[str] = Field(default_factory=list)
    reservation_ids: list[str] = Field(default_factory=list)
    expected_position_count: int = Field(default=0, ge=0)
    broker_position_count: int = Field(default=0, ge=0)

    findings: list[ReconciliationFinding] = Field(default_factory=list)
    counts: ReconciliationCounts = Field(default_factory=ReconciliationCounts)
    #: Reservations this reconciliation moved, by id. Naming them keeps the
    #: economic consequence of a comparison auditable from the comparison.
    reservations_released: list[str] = Field(default_factory=list)
    reservations_consumed: list[str] = Field(default_factory=list)
    reservations_retained_unknown: list[str] = Field(default_factory=list)
    #: Executions whose ambiguous state broker evidence settled.
    executions_resolved: list[str] = Field(default_factory=list)

    #: Structurally zero. Read off the broker's own counter, never asserted.
    orders_submitted: int = Field(default=0, ge=0)
    #: Structurally zero. Reconciliation reports; it does not repair.
    corrective_orders: int = Field(default=0, ge=0)

    content_hash: Identifier
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _reconciliation_never_trades(self) -> ReconciliationResult:
        if self.orders_submitted:
            raise ValueError(
                f"a reconciliation reported {self.orders_submitted} submitted order(s). "
                f"Comparing records against broker reality must never place one"
            )
        if self.corrective_orders:
            raise ValueError(
                f"a reconciliation reported {self.corrective_orders} corrective order(s). "
                f"Automatic corrective trading is out of scope: a discrepancy is reported, and "
                f"deciding what to do about it is a separate judgement"
            )
        return self

    @model_validator(mode="after")
    def _the_status_matches_the_findings(self) -> ReconciliationResult:
        """A status is a claim about the findings and must agree with them.

        ``MATCH`` is the one that matters. Agreeing with nothing is not
        agreement, so a run that could not read the broker can never be a
        match however empty its finding list is.
        """
        disagreements = [finding for finding in self.findings if not finding.agreement]
        if self.status is ReconciliationRunStatus.MATCH:
            if disagreements:
                raise ValueError(
                    f"MATCH cannot carry {len(disagreements)} disagreement(s): "
                    f"{', '.join(f.finding_type.value for f in disagreements[:3])}"
                )
            if not self.positions_read.usable:
                raise ValueError(
                    f"MATCH requires that broker positions were actually read, but the read "
                    f"was {self.positions_read.value}. Agreeing with an absence of data is not "
                    f"agreement"
                )
        if self.status is ReconciliationRunStatus.MISMATCH and not disagreements:
            raise ValueError("MISMATCH must name at least one disagreement")
        if self.status is ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE and not any(
            finding.finding_type is ReconciliationFindingType.BROKER_DATA_UNAVAILABLE
            for finding in self.findings
        ):
            raise ValueError(
                "BROKER_DATA_UNAVAILABLE must carry a finding saying which read failed and why"
            )
        return self

    # --- derived views -----------------------------------------------------
    @property
    def disagreements(self) -> list[ReconciliationFinding]:
        return [finding for finding in self.findings if not finding.agreement]

    @property
    def critical_findings(self) -> list[ReconciliationFinding]:
        return [finding for finding in self.findings if finding.blocking]

    @property
    def matched(self) -> bool:
        return self.status is ReconciliationRunStatus.MATCH

    @property
    def blocks_new_executions(self) -> bool:
        """Fail safe: anything other than a clean match stops new trading.

        An unreachable broker blocks too. Unknown broker state is not a safe
        state to open a position from — the same rule the Milestone 1 report
        applies, restated here so a caller need not project first.
        """
        return not self.matched

    def by_type(self, finding_type: ReconciliationFindingType) -> list[ReconciliationFinding]:
        return [finding for finding in self.findings if finding.finding_type is finding_type]

    def to_reconciliation_report(self) -> ReconciliationReport:
        """Project onto the Milestone 1 workflow boundary.

        Raises for a status the Milestone 1 vocabulary cannot express.
        ``INTERNAL_DATA_UNAVAILABLE`` is the important one: that vocabulary has
        no member for "our own ledger could not be read", and mapping it onto
        ``BROKER_UNAVAILABLE`` would blame the broker for our fault — while
        mapping it onto ``MATCHED`` would claim an agreement nobody checked.
        """
        status = {
            ReconciliationRunStatus.MATCH: ReconciliationStatus.MATCHED,
            ReconciliationRunStatus.MISMATCH: ReconciliationStatus.MISMATCH,
            ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE: (
                ReconciliationStatus.BROKER_UNAVAILABLE
            ),
        }.get(self.status)
        if status is None:
            raise ValueError(
                f"reconciliation status {self.status.value} has no Milestone 1 equivalent. "
                f"A failure to read our own records is not a broker failure and is not a match"
            )
        return ReconciliationReport(
            as_of=self.as_of,
            broker=self.broker,
            status=status,
            discrepancies=[finding.to_discrepancy() for finding in self.disagreements],
            positions_compared=self.counts.positions_compared,
            orders_compared=self.counts.orders_compared,
            executions_compared=self.counts.fills_compared,
        )


class ReconciliationEvent(ImmutableModel):
    """One appended step in a reconciliation's own history.

    Append-only, like every other history in this system. It answers "what did
    this run actually do, and in what order" — which is the question a
    reservation that moved unexpectedly turns into.
    """

    event_id: Identifier
    reconciliation_id: Identifier
    sequence: int = Field(ge=0)
    event_type: ReconciliationEventType
    occurred_at: UtcDatetime
    observed_at: UtcDatetime
    source: Identifier
    schema_version: Identifier = RECONCILIATION_SCHEMA_VERSION

    detail: str | None = None
    finding_id: Identifier | None = None
    reservation_id: Identifier | None = None
    execution_id: Identifier | None = None
    amount: Decimal | None = None
