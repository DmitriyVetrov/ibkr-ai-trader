"""When committed capital may return to the campaign, and on what evidence.

Pure functions over captured state. Nothing here opens a connection, reads a
clock or writes a file — which is what makes a settlement decision replayable
from the stored artifacts long after the fact.

The whole module answers one question per closed position: **is there proof the
position is gone and proof of what it made?**

.. code-block:: text

    broker confirms none of the structure   proof the position ended
    every execution resolved                proof nothing is still working
    realised result computed                proof of what came back
    reconciliation agrees                   proof the records are not disputed
        -> SETTLE (fully, or the matched fraction)

    broker still reports it                 -> BLOCK  CLOSURE_NOT_CONFIRMED
    an execution is UNKNOWN                 -> BLOCK  EXECUTION_UNKNOWN
    no usable realised result               -> BLOCK  PNL_UNAVAILABLE
    a critical reconciliation finding       -> BLOCK  RECONCILIATION_MISMATCH

``EXECUTION_UNKNOWN`` is the line this module exists to hold, and it is the
same line :mod:`trading_system.reservations.lifecycle` holds for release. An
execution whose outcome was never learned may be a live order at the broker
right now. No amount of elapsed time turns that into a failure, no
configuration permits settling it — ``settlement.release_on_unknown: true``
fails to load — and resolution is by *observing* the broker.

**A settlement returns capital, not proceeds.** What comes back to the campaign
is what the reservation consumed; the realised result is recorded next to it
and, by default, is *not* added to the spendable envelope. A system that grew
its own budget on a winning trade would be compounding without anybody deciding
to, and ``settlement.return_realized_pnl_to_campaign`` is where that decision
would be made explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from trading_system.domain.enums import (
    SETTLEABLE_RESERVATION_STATES,
    PnLStatus,
    ReservationEventType,
    ReservationReasonCode,
    ReservationState,
    SettlementBlockReason,
    SettlementStatus,
)
from trading_system.infrastructure.settings import PnLSettlementConfig
from trading_system.pnl.models import (
    RealizedPnL,
    ReservationSettlement,
    settlement_identifier,
)
from trading_system.reservations.models import (
    Reservation,
    ReservationEvent,
    reservation_event_identifier,
)

__all__ = [
    "SettlementInputs",
    "SettlementOutcome",
    "build_settlement",
    "settle",
    "settlement_event",
]


@dataclass(frozen=True, slots=True)
class SettlementInputs:
    """Everything one settlement decision rests on. All captured, all evidence."""

    reservation: Reservation
    position_id: str
    #: Whether the broker itself reports it holds none of the structure. Never
    #: inferred from a submitted order, a reported fill or a decision to exit.
    closure_confirmed: bool
    #: Whether the broker read that established the above actually succeeded.
    #: "We could not look" is not "there is nothing there".
    broker_read_usable: bool
    #: True when any execution against this position is unresolved.
    execution_unknown: bool
    #: The realised result, where one could be computed.
    realized: RealizedPnL | None
    #: Critical reconciliation findings touching this position, by type. Empty
    #: is the ordinary case; a non-empty list stops capital moving.
    reconciliation_findings: Sequence[str] = ()
    reconciliation_id: str | None = None
    policy: PnLSettlementConfig | None = None


@dataclass(frozen=True, slots=True)
class SettlementOutcome:
    """What one evaluation concluded, as a delta against the reservation.

    A delta rather than a total, exactly as a
    :class:`~trading_system.reservations.lifecycle.ReservationOutcome` is.
    Applying it twice over an already-settled reservation moves nothing, which
    is what makes the settlement job idempotent *economically* — a duplicate
    record is untidy, a double release is money.
    """

    status: SettlementStatus
    settled_delta: Decimal = Decimal("0")
    block_reason: SettlementBlockReason | None = None
    reason_code: ReservationReasonCode = ReservationReasonCode.POSITION_CLOSED_CONFIRMED
    event_type: ReservationEventType = ReservationEventType.RESERVATION_SETTLED
    state: ReservationState = ReservationState.SETTLED
    realized_pnl: Decimal | None = None
    matched_quantity: Decimal = Decimal("0")
    detail: str = ""

    @property
    def moved(self) -> bool:
        """Whether this outcome actually returns capital to the campaign."""
        return self.settled_delta > 0


def settle(inputs: SettlementInputs) -> SettlementOutcome:
    """Decide whether this reservation's capital may return, and how much.

    Every branch that refuses names the evidence that was missing. "Blocked"
    on its own is not something an operator can act on, and the question they
    are actually asking — *why has my available capital not gone back up?* —
    is answered by the reason code.
    """
    reservation = inputs.reservation
    policy = inputs.policy

    if reservation.state is ReservationState.SETTLED:
        return SettlementOutcome(
            status=SettlementStatus.ALREADY_SETTLED,
            state=ReservationState.SETTLED,
            detail=(
                "this reservation already settled. Running settlement again derives the same "
                "identity, the ledger recognises the replayed event, and nothing moves"
            ),
        )

    if reservation.state is ReservationState.UNKNOWN or inputs.execution_unknown:
        return _block(
            SettlementBlockReason.EXECUTION_UNKNOWN,
            reservation,
            "an execution against this position is UNKNOWN: an order may be working at the "
            "broker right now. Returning its capital could let the campaign fund the same "
            "trade twice. No elapsed time settles this — only observing the broker does",
        )

    if reservation.state not in SETTLEABLE_RESERVATION_STATES:
        return SettlementOutcome(
            status=SettlementStatus.NOT_APPLICABLE,
            state=reservation.state,
            detail=(
                f"the reservation is {reservation.state.value}, which has no consumed capital "
                f"to settle. Capital that was never spent comes back through a release, not "
                f"through a settlement"
            ),
        )

    consumed = reservation.consumed_amount - reservation.settled_amount
    if consumed <= 0:
        return SettlementOutcome(
            status=SettlementStatus.NOT_APPLICABLE,
            state=reservation.state,
            detail="every euro this reservation consumed has already been settled",
        )

    if not inputs.broker_read_usable:
        return _block(
            SettlementBlockReason.CLOSURE_NOT_CONFIRMED,
            reservation,
            "the broker could not be read, so whether this position still exists is unknown. "
            "'We could not look' is not 'there is nothing there', and capital does not move "
            "on an absence of data",
        )

    if not inputs.closure_confirmed and (policy is None or policy.require_broker_confirmed_closure):
        return _block(
            SettlementBlockReason.CLOSURE_NOT_CONFIRMED,
            reservation,
            "the broker still reports this structure. Only broker reality closes a position — "
            "not a submitted exit, not a reported fill, and not a decision to exit",
        )

    if inputs.reconciliation_findings and (policy is None or policy.require_clean_reconciliation):
        return _block(
            SettlementBlockReason.RECONCILIATION_MISMATCH,
            reservation,
            f"reconciliation reports {', '.join(sorted(set(inputs.reconciliation_findings)))} "
            f"against this position. Capital must not move while our records and the account "
            f"disagree about what is held",
        )

    realized = inputs.realized
    if realized is None or realized.status is PnLStatus.NOT_AVAILABLE:
        codes = ", ".join(code.value for code in realized.reason_codes) if realized else "no result"
        return _block(
            SettlementBlockReason.PNL_UNAVAILABLE,
            reservation,
            f"realised profit and loss could not be computed ({codes}), so what this position "
            f"returned is not a known quantity. Settling anyway would credit the campaign with "
            f"a figure nobody has",
        )

    if realized.currency and realized.currency != reservation.currency:
        return _block(
            SettlementBlockReason.CURRENCY_MISMATCH,
            reservation,
            f"the result settled in {realized.currency} and the reservation is denominated in "
            f"{reservation.currency}. No deterministic FX rate source exists, and converting "
            f"at an invented one would misstate the campaign by an amount nobody recorded",
        )

    # --- how much comes back ----------------------------------------------
    #
    # A structure that closed four of its ten units returns four tenths of the
    # capital, and the fraction comes from the *matched units*, which are what
    # the confirmed fills on both sides actually support.
    fraction, matched = _closed_fraction(reservation, realized)
    if fraction <= 0:
        return _block(
            SettlementBlockReason.CLOSURE_NOT_CONFIRMED,
            reservation,
            "no matched units support a closure, so there is nothing this settlement could "
            "honestly return",
        )

    if fraction >= 1:
        settled = consumed
        status = SettlementStatus.SETTLED
        state = ReservationState.SETTLED
        event_type = ReservationEventType.RESERVATION_SETTLED
        reason = ReservationReasonCode.POSITION_CLOSED_CONFIRMED
    else:
        # Rounded DOWN, always. Returning less than the exact share leaves the
        # remainder committed, which is the conservative error; returning more
        # would hand the campaign capital that is still in a live position.
        settled = _round_down(reservation.consumed_amount * fraction) - reservation.settled_amount
        if settled <= 0:
            return SettlementOutcome(
                status=SettlementStatus.NOT_APPLICABLE,
                state=reservation.state,
                detail=(
                    "the closed fraction is smaller than one unit of currency, so nothing "
                    "returns yet"
                ),
            )
        status = SettlementStatus.PARTIALLY_SETTLED
        state = reservation.state
        event_type = ReservationEventType.RESERVATION_PARTIALLY_SETTLED
        reason = ReservationReasonCode.POSITION_PARTIALLY_CLOSED

    return SettlementOutcome(
        status=status,
        settled_delta=settled,
        reason_code=reason,
        event_type=event_type,
        state=state,
        realized_pnl=realized.best_available_pnl,
        matched_quantity=matched,
        detail=(
            f"the broker confirms {'the structure' if fraction >= 1 else 'part of the structure'} "
            f"is gone and the realised result is {realized.status.value}. {settled} returns to "
            f"the campaign; the trade's result is recorded separately and is not added to the "
            f"spendable envelope"
        ),
    )


def settlement_event(
    reservation: Reservation,
    outcome: SettlementOutcome,
    settlement: ReservationSettlement,
    *,
    sequence: int,
    occurred_at: datetime,
    observed_at: datetime,
    source: str = "pnl",
) -> ReservationEvent:
    """Turn a settlement conclusion into an appendable ledger observation.

    The event id derives from the reservation, the sequence and the event type,
    exactly as every other reservation event's does — so a replayed settlement
    is recognised by the store and appends nothing.
    """
    return ReservationEvent(
        event_id=reservation_event_identifier(
            reservation_id=reservation.reservation_id,
            sequence=sequence,
            event_type=outcome.event_type.value,
        ),
        reservation_id=reservation.reservation_id,
        sequence=sequence,
        event_type=outcome.event_type,
        state=outcome.state,
        occurred_at=occurred_at,
        observed_at=observed_at,
        source=source,
        settled_delta=outcome.settled_delta,
        realized_pnl=outcome.realized_pnl,
        settlement_id=settlement.settlement_id,
        pnl_id=settlement.pnl_id,
        position_id=settlement.position_id,
        reason_code=outcome.reason_code,
        detail=outcome.detail or None,
        reconciliation_id=settlement.reconciliation_id,
    )


def build_settlement(
    reservation: Reservation,
    outcome: SettlementOutcome,
    *,
    position_id: str,
    pnl_id: str | None,
    settled_at: datetime,
    reconciliation_id: str | None = None,
) -> ReservationSettlement:
    """The immutable record of what this evaluation concluded, either way."""
    committed_before = reservation.committed_amount
    return ReservationSettlement(
        settlement_id=settlement_identifier(
            reservation_id=reservation.reservation_id,
            position_id=position_id,
            settled_amount=str(outcome.settled_delta),
            status=outcome.status.value,
        ),
        reservation_id=reservation.reservation_id,
        position_id=position_id,
        campaign_id=reservation.campaign_id,
        allocation_id=reservation.allocation_id,
        status=outcome.status,
        block_reason=outcome.block_reason,
        currency=reservation.currency,
        committed_before=committed_before,
        settled_amount=outcome.settled_delta,
        committed_after=committed_before - outcome.settled_delta,
        pnl_id=pnl_id,
        realized_pnl=outcome.realized_pnl,
        matched_quantity=outcome.matched_quantity,
        authorized_quantity=reservation.authorized_quantity,
        settled_at=settled_at,
        reconciliation_id=reconciliation_id,
        detail=outcome.detail or None,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _block(
    reason: SettlementBlockReason, reservation: Reservation, detail: str
) -> SettlementOutcome:
    """Move nothing, and name the missing evidence."""
    return SettlementOutcome(
        status=SettlementStatus.BLOCKED,
        settled_delta=Decimal("0"),
        block_reason=reason,
        reason_code=ReservationReasonCode.SETTLEMENT_BLOCKED,
        event_type=ReservationEventType.RESERVATION_SETTLEMENT_BLOCKED,
        state=reservation.state,
        detail=detail,
    )


def _closed_fraction(reservation: Reservation, realized: RealizedPnL) -> tuple[Decimal, Decimal]:
    """How much of the authorisation the confirmed closure actually covers.

    Taken from the realised result's *matched* units — the units both sides'
    confirmed fills support — against what the reservation consumed units for.
    Anything else would let a structure that half-closed return all of its
    capital.
    """
    matched = realized.matched_quantity
    basis = Decimal(reservation.consumed_quantity or reservation.authorized_quantity)
    if basis <= 0 or matched <= 0:
        return Decimal("0"), matched
    return min(matched / basis, Decimal("1")), matched


def _round_down(value: Decimal) -> Decimal:
    """Two decimal places, always downwards. The conservative direction."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
