"""Comparing what we believe we hold against what the broker reports.

Pure functions. No broker, no repository, no clock — the instant is supplied,
which is what makes a stored reconciliation reproducible.

**The broker wins, and losing is not the same as being wrong about what to do.**
Every function here reports; none of them adjusts the internal ledger to make a
disagreement disappear, and none of them proposes a trade. The four outcomes:

.. code-block:: text

    expected +2, broker +2      POSITION_MATCH
    expected +2, broker +1      POSITION_QUANTITY_MISMATCH   delta -1
    expected +2, broker none    EXPECTED_POSITION_MISSING
    expected none, broker +4    ORPHAN_BROKER_POSITION

The third is the dangerous one: every risk figure downstream is computed from a
holding that does not exist. The fourth is the ordinary state of a real account
that traded before this system existed, and it is reported, never adopted,
never sold and never assigned to a campaign.

A fifth case exists and is worth its own finding: the same instrument present
at the broker under a *different* contract id. Reported as
``POSITION_CONTRACT_MISMATCH`` rather than as an unrelated missing/orphan pair,
because the pair reads as two problems when it is one — usually a corporate
action that adjusted the contract underneath us.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    ReconciliationFindingType,
    StructureStatus,
)
from trading_system.positions.models import (
    BrokerPositionSnapshot,
    ExpectedPosition,
    ObservedPosition,
    StrategyPosition,
)
from trading_system.reconciliation.findings import (
    SeverityLookup,
    delta_of,
    make_finding,
)
from trading_system.reconciliation.models import ReconciliationFinding

__all__ = [
    "compare_positions",
    "compare_structures",
    "position_unavailable_finding",
]


def position_unavailable_finding(
    snapshot: BrokerPositionSnapshot, *, severity: SeverityLookup
) -> ReconciliationFinding:
    """Say that broker positions could not be read — and compare nothing.

    The single most important refusal in this package. Reconciling an
    unreadable broker against the internal ledger would report every position
    the system believes in as missing, every real holding as gone, and would do
    it with total confidence.
    """
    return make_finding(
        ReconciliationFindingType.BROKER_DATA_UNAVAILABLE,
        severity=severity,
        identifier=f"positions@{snapshot.broker}",
        summary=(
            f"broker position state could not be read ({snapshot.read_status.value}); no "
            f"position comparison was performed"
        ),
        observed_at=snapshot.observed_at,
        detail=(
            f"{snapshot.detail or 'no detail supplied'}. This is NOT an empty account: nothing "
            f"here says the broker holds no positions, only that we were unable to look. No "
            f"position was compared, because comparing against an absence of data would report "
            f"every holding as missing"
        ),
        recommended_action=(
            "ACTION REQUIRED: restore broker connectivity and reconcile again before opening "
            "any new position"
        ),
    )


def compare_positions(
    *,
    expected: Sequence[ExpectedPosition],
    snapshot: BrokerPositionSnapshot,
    severity: SeverityLookup,
    observed_at: datetime | None = None,
) -> list[ReconciliationFinding]:
    """Compare the internal ledger against one broker snapshot, contract by contract.

    Deterministic: keys are visited in sorted order, so the same two ledgers
    always produce the same findings in the same order.
    """
    if not snapshot.usable:
        return [position_unavailable_finding(snapshot, severity=severity)]

    # A closed position — expected zero, and the broker reports nothing — is not
    # a holding on either side. It is excluded here rather than compared, so a
    # contract that was opened and closed does not read as a missing position
    # for the rest of the campaign's life.
    ours = {position.key: position for position in expected if position.quantity != 0}
    theirs = {position.key: position for position in snapshot.positions}

    if not theirs and not ours:
        return [
            make_finding(
                ReconciliationFindingType.BROKER_RETURNED_EMPTY,
                severity=severity,
                identifier=f"positions@{snapshot.broker}",
                summary="the broker holds no positions, and the system expects none",
                observed_at=snapshot.observed_at,
                detail=(
                    "the broker answered and reported an empty portfolio. That is a valid "
                    "answer about the account, and it agrees with the internal ledger"
                ),
            )
        ]

    findings: list[ReconciliationFinding] = []

    for key in sorted(set(ours) | set(theirs)):
        mine = ours.get(key)
        broker = theirs.get(key)
        if mine is not None and broker is not None:
            findings.append(_compare_one(mine, broker, severity=severity))
        elif mine is not None:
            findings.append(
                _missing(mine, snapshot, severity=severity, candidates=list(theirs.values()))
            )
        elif broker is not None:
            findings.append(_orphan(broker, severity=severity))

    return findings


def _compare_one(
    expected: ExpectedPosition,
    observed: ObservedPosition,
    *,
    severity: SeverityLookup,
) -> ReconciliationFinding:
    if expected.quantity == observed.quantity:
        return make_finding(
            ReconciliationFindingType.POSITION_MATCH,
            severity=severity,
            identifier=expected.key,
            summary=f"{observed.describe()}: expected and broker both report {observed.quantity}",
            expected_value=str(expected.quantity),
            observed_value=str(observed.quantity),
            delta="0",
            expected_provenance=_provenance(expected),
            broker_provenance=observed.broker_source,
            observed_at=observed.observed_at,
            broker_timestamp=observed.broker_timestamp,
            symbol=observed.underlying,
            contract_id=observed.contract_id,
        )

    return make_finding(
        ReconciliationFindingType.POSITION_QUANTITY_MISMATCH,
        severity=severity,
        identifier=expected.key,
        summary=(
            f"{observed.describe()}: expected {expected.quantity}, broker reports "
            f"{observed.quantity}"
        ),
        expected_value=str(expected.quantity),
        observed_value=str(observed.quantity),
        delta=delta_of(expected.quantity, observed.quantity),
        expected_provenance=_provenance(expected),
        broker_provenance=observed.broker_source,
        observed_at=observed.observed_at,
        broker_timestamp=observed.broker_timestamp,
        symbol=observed.underlying,
        contract_id=observed.contract_id,
        detail=(
            "the broker is authoritative for what is held. The internal ledger is not adjusted "
            "to agree: one of the two is wrong about a real trade, and quietly picking a winner "
            "destroys the evidence of which"
        ),
        recommended_action=(
            "ACTION REQUIRED: establish which fills the broker actually reported for this "
            "contract before any further order is sent for it"
        ),
    )


def _missing(
    expected: ExpectedPosition,
    snapshot: BrokerPositionSnapshot,
    *,
    severity: SeverityLookup,
    candidates: Sequence[ObservedPosition],
) -> ReconciliationFinding:
    """We believe we hold this and the broker does not report it."""
    twin = _same_instrument_different_contract(expected, candidates)
    if twin is not None:
        return make_finding(
            ReconciliationFindingType.POSITION_CONTRACT_MISMATCH,
            severity=severity,
            identifier=expected.key,
            summary=(
                f"{expected.symbol} {expected.expiration} {expected.strike} "
                f"{expected.right.value if expected.right else ''}: expected under contract "
                f"{expected.contract_id}, broker holds it under {twin.contract_id}"
            ).strip(),
            expected_value=f"{expected.contract_id}:{expected.quantity}",
            observed_value=f"{twin.contract_id}:{twin.quantity}",
            expected_provenance=_provenance(expected),
            broker_provenance=twin.broker_source,
            observed_at=twin.observed_at,
            broker_timestamp=twin.broker_timestamp,
            symbol=expected.symbol,
            contract_id=twin.contract_id,
            detail=(
                "the same human-readable contract terms appear under a different broker "
                "contract id. This is what a corporate action that adjusted the contract looks "
                "like; it is reported as one finding rather than as an unrelated missing "
                "position and an unrelated orphan"
            ),
            recommended_action=(
                "ACTION REQUIRED: confirm whether a corporate action adjusted this contract"
            ),
        )

    return make_finding(
        ReconciliationFindingType.EXPECTED_POSITION_MISSING,
        severity=severity,
        identifier=expected.key,
        summary=(
            f"{expected.symbol}: the system expects {expected.quantity} contract(s) that the "
            f"broker does not report"
        ),
        expected_value=str(expected.quantity),
        observed_value="0",
        delta=delta_of(expected.quantity, Decimal("0")),
        expected_provenance=_provenance(expected),
        broker_provenance=snapshot.broker,
        observed_at=snapshot.observed_at,
        symbol=expected.symbol,
        contract_id=expected.contract_id,
        detail=(
            "the broker is authoritative: as far as the account is concerned this position does "
            "not exist. Every downstream risk and exposure figure computed from it is wrong "
            "until this is resolved"
        ),
        recommended_action=(
            "ACTION REQUIRED: no replacement order is placed and none should be placed "
            "automatically. Establish what happened to the fills this position rests on"
        ),
    )


def _orphan(observed: ObservedPosition, *, severity: SeverityLookup) -> ReconciliationFinding:
    """The broker holds something no execution of ours accounts for."""
    return make_finding(
        ReconciliationFindingType.ORPHAN_BROKER_POSITION,
        severity=severity,
        identifier=observed.key,
        summary=(
            f"{observed.describe()}: the broker holds {observed.quantity} contract(s) that no "
            f"internal execution accounts for"
        ),
        expected_value=None,
        observed_value=str(observed.quantity),
        broker_provenance=observed.broker_source,
        observed_at=observed.observed_at,
        broker_timestamp=observed.broker_timestamp,
        symbol=observed.underlying,
        contract_id=observed.contract_id,
        detail=(
            "this is real: the broker holds it. Its acquisition provenance is UNKNOWN and stays "
            "UNKNOWN — no allocation, execution, strategy or research thesis is invented for it. "
            "A pre-existing holding in an account that traded before this system is the ordinary "
            "cause, and the first reconciliation of such an account is expected to report several"
        ),
        recommended_action=(
            "ACTION REQUIRED: decide manually whether this holding should be adopted. Nothing "
            "here sells it, hedges it or assigns it to a campaign"
        ),
    )


def _same_instrument_different_contract(
    expected: ExpectedPosition, candidates: Sequence[ObservedPosition]
) -> ObservedPosition | None:
    """A broker holding with the same terms but a different contract id."""
    if expected.expiration is None or expected.strike is None or expected.right is None:
        return None
    for candidate in candidates:
        if (
            candidate.underlying == expected.underlying
            and candidate.expiration == expected.expiration
            and candidate.strike == expected.strike
            and candidate.right is expected.right
            and candidate.contract_id != expected.contract_id
        ):
            return candidate
    return None


def compare_structures(
    structures: Sequence[StrategyPosition], *, severity: SeverityLookup
) -> list[ReconciliationFinding]:
    """Report multi-leg structures the broker holds only in part.

    A straddle with its call held and its put missing is neither a straddle nor
    an absence of one. It is a naked long call against limits nobody checked
    for one, and nothing in this milestone hedges, completes or closes it — it
    is reported so a person can.
    """
    findings: list[ReconciliationFinding] = []
    for structure in structures:
        if structure.status is not StructureStatus.PARTIAL:
            continue
        held = [leg for leg in structure.legs if leg.present]
        absent = [leg for leg in structure.legs if leg.absent]
        findings.append(
            make_finding(
                ReconciliationFindingType.PARTIAL_STRUCTURE,
                severity=severity,
                identifier=structure.strategy_position_id,
                summary=(
                    f"{structure.underlying} {structure.strategy.value}: the broker holds "
                    f"{len(held)} of {len(structure.legs)} legs"
                ),
                expected_value=f"{len(structure.legs)} legs",
                observed_value=f"{len(held)} legs",
                observed_at=structure.as_of,
                symbol=structure.underlying,
                execution_id=structure.execution_id,
                allocation_id=structure.allocation_id,
                opportunity_id=structure.opportunity_id,
                detail=(
                    "the authorised structure is only partly present. Missing legs: "
                    + ", ".join(
                        f"{leg.right.value if leg.right else '?'} {leg.strike}" for leg in absent
                    )
                    + ". The risk of what is actually held is not the risk that was authorised"
                ),
                recommended_action=(
                    "ACTION REQUIRED: this structure is not the position that was authorised. "
                    "Nothing here completes or closes it"
                ),
            )
        )
    return findings


def _provenance(expected: ExpectedPosition) -> str:
    """Where an expected position came from, in one readable string."""
    if expected.execution_ids:
        return f"executions {', '.join(expected.execution_ids)}"
    return expected.provenance.value
