"""Reporting what reconciliation did to the campaign's committed capital.

Pure functions over a reservation and the conclusion drawn about it. The
decisions themselves live in :mod:`trading_system.reservations.lifecycle`; this
module only turns them into findings, so an operator reading a reconciliation
can see the money move without opening a second ledger.

Two of these findings are agreements — capital consumed by a fill, capital
released on proof — and two are not:

* ``RESERVATION_RETAINED_UNKNOWN`` — capital held because an execution is
  unresolved. Not an error, and not routine either: it is budget the campaign
  cannot use for a reason that will not resolve itself.
* ``RESERVATION_MISMATCH`` — the reservation and the position ledger disagree
  about whether capital is in a position. Either a fill was missed or a
  reservation was consumed for a position that does not exist.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trading_system.domain.enums import (
    ReconciliationFindingType,
    ReservationReasonCode,
    ReservationState,
)
from trading_system.positions.models import ExpectedPosition
from trading_system.reconciliation.findings import SeverityLookup, make_finding
from trading_system.reconciliation.models import ReconciliationFinding
from trading_system.reservations.lifecycle import ReservationOutcome
from trading_system.reservations.models import Reservation

__all__ = ["compare_reservations"]


def compare_reservations(
    updates: Sequence[tuple[Reservation, ReservationOutcome]],
    *,
    expected: Sequence[ExpectedPosition],
    severity: SeverityLookup,
    observed_at: datetime | None = None,
) -> list[ReconciliationFinding]:
    """Turn each reservation conclusion into a finding, and check it for sense."""
    held_by_opportunity = {
        opportunity
        for position in expected
        if position.quantity != 0
        for opportunity in position.opportunity_ids
    }
    findings: list[ReconciliationFinding] = []

    for reservation, outcome in sorted(updates, key=lambda pair: pair[0].reservation_id):
        if outcome.reason_code is ReservationReasonCode.CURRENCY_MISMATCH:
            findings.append(_currency(reservation, outcome, severity=severity, at=observed_at))
            continue

        mismatch = _mismatch(reservation, held_by_opportunity, severity=severity, at=observed_at)
        if mismatch is not None:
            findings.append(mismatch)
            continue

        if reservation.state is ReservationState.UNKNOWN:
            findings.append(_retained(reservation, outcome, severity=severity, at=observed_at))
        elif reservation.state is ReservationState.RELEASED:
            findings.append(_released(reservation, outcome, severity=severity, at=observed_at))
        elif reservation.state in (
            ReservationState.CONSUMED,
            ReservationState.PARTIALLY_CONSUMED,
        ):
            findings.append(_consumed(reservation, outcome, severity=severity, at=observed_at))
        else:
            findings.append(_match(reservation, severity=severity, at=observed_at))

    return findings


def _match(
    reservation: Reservation, *, severity: SeverityLookup, at: datetime | None
) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.RESERVATION_MATCH,
        severity=severity,
        identifier=reservation.reservation_id,
        summary=(
            f"{reservation.symbol}: {reservation.authorized_amount} "
            f"{reservation.currency} remains reserved and unspent"
        ),
        expected_value=str(reservation.authorized_amount),
        observed_value=str(reservation.committed_amount),
        observed_at=at,
        symbol=reservation.symbol,
        allocation_id=reservation.allocation_id,
        opportunity_id=reservation.opportunity_id,
        reservation_id=reservation.reservation_id,
        detail=(
            "committed, not invested: an authorisation this system has not executed keeps its "
            "capital, because not having executed is not evidence that nothing will be sent"
        ),
    )


def _consumed(
    reservation: Reservation,
    outcome: ReservationOutcome,
    *,
    severity: SeverityLookup,
    at: datetime | None,
) -> ReconciliationFinding:
    basis = (
        "actual fill economics"
        if reservation.consumed_from_actual_fills
        else "the authorisation's own unit cost"
    )
    return make_finding(
        ReconciliationFindingType.RESERVATION_CONSUMED,
        severity=severity,
        identifier=reservation.reservation_id,
        summary=(
            f"{reservation.symbol}: {reservation.consumed_amount} {reservation.currency} "
            f"consumed by confirmed fills ({reservation.state.value})"
        ),
        expected_value=str(reservation.authorized_amount),
        observed_value=str(reservation.consumed_amount),
        observed_at=at,
        symbol=reservation.symbol,
        allocation_id=reservation.allocation_id,
        opportunity_id=reservation.opportunity_id,
        reservation_id=reservation.reservation_id,
        execution_id=reservation.execution_id,
        detail=(
            f"consumed from {basis}. The authorised figure is kept unchanged alongside it: "
            f"Milestone 7 authorised {reservation.authorized_amount} and the market charged "
            f"{reservation.consumed_amount}. {outcome.detail}"
        ),
    )


def _released(
    reservation: Reservation,
    outcome: ReservationOutcome,
    *,
    severity: SeverityLookup,
    at: datetime | None,
) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.RESERVATION_RELEASED,
        severity=severity,
        identifier=reservation.reservation_id,
        summary=(
            f"{reservation.symbol}: {reservation.released_amount} {reservation.currency} "
            f"returned to the campaign ({outcome.reason_code.value})"
        ),
        expected_value=str(reservation.authorized_amount),
        observed_value=str(reservation.released_amount),
        observed_at=at,
        symbol=reservation.symbol,
        allocation_id=reservation.allocation_id,
        opportunity_id=reservation.opportunity_id,
        reservation_id=reservation.reservation_id,
        execution_id=reservation.execution_id,
        detail=(
            f"released on proof that the capital was not spent: {outcome.detail}. This is the "
            f"limitation Milestone 7 documented and could not resolve, closed by the milestone "
            f"that can observe what happened"
        ),
    )


def _retained(
    reservation: Reservation,
    outcome: ReservationOutcome,
    *,
    severity: SeverityLookup,
    at: datetime | None,
) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.RESERVATION_RETAINED_UNKNOWN,
        severity=severity,
        identifier=reservation.reservation_id,
        summary=(
            f"{reservation.symbol}: {reservation.committed_amount} {reservation.currency} stays "
            f"committed because execution {reservation.execution_id or '(unnamed)'} is UNKNOWN"
        ),
        expected_value=str(reservation.authorized_amount),
        observed_value=str(reservation.committed_amount),
        observed_at=at,
        symbol=reservation.symbol,
        allocation_id=reservation.allocation_id,
        opportunity_id=reservation.opportunity_id,
        reservation_id=reservation.reservation_id,
        execution_id=reservation.execution_id,
        detail=outcome.detail,
        recommended_action=(
            "ACTION REQUIRED: this capital is not available and must not be re-authorised. "
            "Resolve the execution against the broker; no amount of elapsed time settles it"
        ),
    )


def _currency(
    reservation: Reservation,
    outcome: ReservationOutcome,
    *,
    severity: SeverityLookup,
    at: datetime | None,
) -> ReconciliationFinding:
    return make_finding(
        ReconciliationFindingType.CURRENCY_MISMATCH,
        severity=severity,
        identifier=reservation.reservation_id,
        summary=(
            f"{reservation.symbol}: the execution settled in a currency this reservation "
            f"({reservation.currency}) does not hold"
        ),
        expected_value=reservation.currency,
        observed_value="a different currency",
        observed_at=at,
        symbol=reservation.symbol,
        allocation_id=reservation.allocation_id,
        opportunity_id=reservation.opportunity_id,
        reservation_id=reservation.reservation_id,
        execution_id=reservation.execution_id,
        detail=outcome.detail,
        recommended_action=(
            "ACTION REQUIRED: no exchange rate is invented here. Either denominate the campaign "
            "in the currency it trades, or add a deterministic FX source"
        ),
    )


def _mismatch(
    reservation: Reservation,
    held_by_opportunity: set[str],
    *,
    severity: SeverityLookup,
    at: datetime | None,
) -> ReconciliationFinding | None:
    """Where the money ledger and the position ledger disagree.

    Two directions, both worth catching:

    * capital consumed with no position to show for it — the fill it rests on
      is not in the position ledger;
    * a position held against a reservation that still says nothing was spent —
      the fill happened and the capital was never accounted for.
    """
    holds_position = reservation.opportunity_id in held_by_opportunity

    if reservation.consumed_amount > 0 and not holds_position:
        return make_finding(
            ReconciliationFindingType.RESERVATION_MISMATCH,
            severity=severity,
            identifier=reservation.reservation_id,
            summary=(
                f"{reservation.symbol}: {reservation.consumed_amount} {reservation.currency} is "
                f"recorded as consumed, but the position ledger holds nothing for this "
                f"opportunity"
            ),
            expected_value=f"consumed {reservation.consumed_amount}",
            observed_value="no expected position",
            observed_at=at,
            symbol=reservation.symbol,
            allocation_id=reservation.allocation_id,
            opportunity_id=reservation.opportunity_id,
            reservation_id=reservation.reservation_id,
            execution_id=reservation.execution_id,
            detail=(
                "capital was consumed by a fill that produced no position in the ledger. Either "
                "the position was closed and the reservation not yet settled, or a fill was "
                "recorded that never established a holding"
            ),
            recommended_action="ACTION REQUIRED: reconcile this opportunity's fills by hand",
        )

    if (
        holds_position
        and reservation.consumed_amount == 0
        and reservation.state is ReservationState.RESERVED
    ):
        return make_finding(
            ReconciliationFindingType.RESERVATION_MISMATCH,
            severity=severity,
            identifier=reservation.reservation_id,
            summary=(
                f"{reservation.symbol}: a position is held for this opportunity, but the "
                f"reservation still reports nothing consumed"
            ),
            expected_value="consumed 0",
            observed_value="position held",
            observed_at=at,
            symbol=reservation.symbol,
            allocation_id=reservation.allocation_id,
            opportunity_id=reservation.opportunity_id,
            reservation_id=reservation.reservation_id,
            detail=(
                "the position ledger and the capital ledger disagree about whether this trade "
                "happened. The capital is still counted as available for a new authorisation, "
                "which is the direction that risks committing it twice"
            ),
            recommended_action="ACTION REQUIRED: reconcile this opportunity's fills by hand",
        )

    return None
