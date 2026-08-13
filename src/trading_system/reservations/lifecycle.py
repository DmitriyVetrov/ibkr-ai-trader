"""When committed capital may move, and on what evidence.

Pure functions over an execution record and a policy. Nothing here opens a
connection, reads a clock or writes a file — which is what makes a reservation
outcome reproducible from the stored artifacts long after the fact.

The whole module answers one question per reservation: **is there proof the
capital was not spent?**

.. code-block:: text

    FAILED before submission   proof: the attempt never left the process
    REJECTED by the broker     proof: the broker refused it
    CANCELLED, no fill         proof: the broker cancelled it unfilled
    EXPIRED, no fill           proof: it stopped working unfilled
        -> RELEASE

    FILLED                     the capital is in a position
    PARTIALLY_FILLED           part of it is; the rest may still fill
        -> CONSUME (and release only what a terminal outcome frees)

    SUBMITTED / PENDING        the order is working
    UNKNOWN                    the order may be working
        -> HOLD

``UNKNOWN`` is the line this module exists to hold. An execution whose outcome
was never learned may be a live order at the broker right now; releasing its
capital and letting the campaign authorise it again is exactly how one
intention becomes two positions. There is no policy switch that permits it —
``release_on_unknown: true`` fails to load — and where *any* attempt against an
authorisation is unresolved, no release happens at all.

**Units.** ``ExecutionRecord.executed_capital`` is in the broker's *quoted*
terms (price times units); money needs the multiplier as well. This module
computes money explicitly in :func:`executed_capital`, because a reservation
consuming 12.10 where 1,210.00 was meant would leave the campaign believing it
had almost all its budget left.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    ExecutionState,
    ReservationEventType,
    ReservationReasonCode,
    ReservationState,
)
from trading_system.infrastructure.settings import ReservationPolicyConfig
from trading_system.reservations.models import (
    Reservation,
    ReservationEvent,
    reservation_event_identifier,
)

__all__ = [
    "EXECUTION_PRECEDENCE",
    "ReservationOutcome",
    "authorised_capital_for",
    "dominant_execution",
    "executed_capital",
    "resolve_reservation",
]

#: Which attempt describes an authorisation's fate when several exist.
#:
#: Ordered by how much they establish. A fill is the strongest statement and a
#: refusal the weakest; ``UNKNOWN`` sits above every working state because it
#: is the one that must not be overridden by a tidier-looking sibling.
EXECUTION_PRECEDENCE: dict[ExecutionState, int] = {
    ExecutionState.FILLED: 90,
    ExecutionState.PARTIALLY_FILLED: 80,
    ExecutionState.UNKNOWN: 70,
    ExecutionState.CANCEL_PENDING: 62,
    ExecutionState.SUBMITTED: 60,
    ExecutionState.SUBMISSION_PENDING: 55,
    ExecutionState.CANCELLED: 40,
    ExecutionState.EXPIRED: 35,
    ExecutionState.REJECTED: 20,
    ExecutionState.FAILED: 10,
    ExecutionState.VALIDATED: 5,
    ExecutionState.CREATED: 1,
}


@dataclass(frozen=True, slots=True)
class ReservationOutcome:
    """What one evaluation concluded about a reservation's capital.

    Deltas rather than totals, so applying an outcome twice over an already
    updated reservation is a no-op rather than a double consumption. That is
    what makes reconciliation idempotent at the economic level, which matters
    more than idempotency at the record level: a duplicate record is untidy, a
    double release is money.
    """

    state: ReservationState
    reason_code: ReservationReasonCode
    event_type: ReservationEventType
    consumed_delta: Decimal = Decimal("0")
    released_delta: Decimal = Decimal("0")
    over_delta: Decimal = Decimal("0")
    consumed_quantity_delta: int = 0
    consumed_from_actual_fills: bool = False
    execution_id: str | None = None
    broker_order_id: str | None = None
    detail: str = ""

    @property
    def changed(self) -> bool:
        """Whether anything about the money or the state actually moved."""
        return bool(
            self.consumed_delta
            or self.released_delta
            or self.over_delta
            or self.consumed_quantity_delta
        )

    def to_event(
        self,
        reservation: Reservation,
        *,
        sequence: int,
        occurred_at: datetime,
        observed_at: datetime,
        source: str,
        reconciliation_id: str | None = None,
        fill_ids: Sequence[str] = (),
    ) -> ReservationEvent:
        """Turn the conclusion into an appendable observation."""
        return ReservationEvent(
            event_id=reservation_event_identifier(
                reservation_id=reservation.reservation_id,
                sequence=sequence,
                event_type=self.event_type.value,
            ),
            reservation_id=reservation.reservation_id,
            sequence=sequence,
            event_type=self.event_type,
            state=self.state,
            occurred_at=occurred_at,
            observed_at=observed_at,
            source=source,
            consumed_delta=self.consumed_delta,
            released_delta=self.released_delta,
            over_delta=self.over_delta,
            consumed_quantity_delta=self.consumed_quantity_delta,
            consumed_from_actual_fills=self.consumed_from_actual_fills,
            reason_code=self.reason_code,
            detail=self.detail or None,
            execution_id=self.execution_id,
            broker_order_id=self.broker_order_id,
            reconciliation_id=reconciliation_id,
            fill_ids=list(fill_ids),
        )


def executed_capital(record: object) -> Decimal | None:
    """Money one execution actually spent, multiplier included.

    ``None`` when the broker reported no average fill price, nothing filled, or
    no multiplier is on the record. Never an estimate: a reservation that
    consumed a guessed figure would misstate the campaign by an amount nobody
    could reconstruct.

    Deliberately *not* :attr:`ExecutionRecord.executed_capital`, which is in
    quoted terms. The conversion happens exactly once, here, against a
    multiplier the Milestone 8 validator already proved every leg shares.
    """
    price = getattr(record, "average_fill_price", None)
    filled = int(getattr(record, "filled_quantity", 0) or 0)
    multiplier = int(getattr(record, "multiplier", 0) or 0)
    if price is None or filled <= 0 or multiplier <= 0:
        return None
    return Decimal(price) * Decimal(filled) * Decimal(multiplier)


def authorised_capital_for(reservation: Reservation, *, filled: int) -> Decimal:
    """The authorisation's own arithmetic, prorated to what filled.

    The fallback when the broker's fill economics are unavailable. Exact rather
    than estimated: it is Milestone 7's unit cost times the units that actually
    traded, and a record built from it says so in
    ``consumed_from_actual_fills=False`` so the two are never confused.
    """
    if reservation.authorized_quantity <= 0:
        return Decimal("0")
    unit = reservation.authorized_amount / Decimal(reservation.authorized_quantity)
    return unit * Decimal(min(filled, reservation.authorized_quantity))


def dominant_execution[RecordT](records: Sequence[RecordT]) -> RecordT | None:
    """The attempt that describes this authorisation's fate.

    Ties break on the execution id so two attempts recorded in the same state
    resolve deterministically rather than on whichever the filesystem returned
    first.
    """
    if not records:
        return None
    return max(
        records,
        key=lambda record: (
            EXECUTION_PRECEDENCE.get(getattr(record, "state", ExecutionState.CREATED), 0),
            str(getattr(record, "execution_id", "")),
        ),
    )


def resolve_reservation(
    reservation: Reservation,
    records: Sequence[object],
    *,
    policy: ReservationPolicyConfig,
) -> ReservationOutcome:
    """Decide what should be true of this reservation's capital, and by how much.

    Returns deltas against the reservation as it stands, so an outcome applied
    to an already-current reservation moves nothing.
    """
    if not records:
        return _hold(
            reservation,
            ReservationReasonCode.NOT_EXECUTED,
            "no execution attempt exists for this authorisation. An authorisation nobody has "
            "acted on yet keeps its reservation: not executing is not evidence that nothing "
            "will be sent",
        )

    dominant = dominant_execution(records)
    assert dominant is not None  # narrowing; records is non-empty
    state: ExecutionState = getattr(dominant, "state", ExecutionState.CREATED)
    filled = int(getattr(dominant, "filled_quantity", 0) or 0)
    execution_id = getattr(dominant, "execution_id", None)
    broker_order_id = getattr(dominant, "broker_order_id", None)

    # Any unresolved attempt locks the capital, whatever the tidiest sibling
    # says. Absence of an acknowledgement is not absence of an order.
    uncertain = [
        record for record in records if getattr(record, "state", None) is ExecutionState.UNKNOWN
    ]

    currency_ok, currency_detail = _currency_matches(reservation, dominant, policy)
    if filled > 0 and not currency_ok:
        return _hold(reservation, ReservationReasonCode.CURRENCY_MISMATCH, currency_detail)

    consumed_target, from_actual = _consumed_target(reservation, dominant, filled, policy)
    released_target, reason = _released_target(
        reservation,
        state=state,
        filled=filled,
        consumed=consumed_target,
        policy=policy,
        blocked=bool(uncertain),
    )

    authorized = reservation.authorized_amount
    over = max(consumed_target - authorized, Decimal("0"))
    remaining = authorized + over - consumed_target - released_target
    resolved_state = _state_for(
        consumed=consumed_target,
        released=released_target,
        remaining=remaining,
        blocked=bool(uncertain),
    )
    if over > 0:
        # The broker reports more spent than was ever authorised. That is a
        # fact about the account, not an accounting adjustment to make quietly:
        # the reservation records it as a correction, which is the only shape in
        # which the model will accept consumed capital past the authorisation.
        reason = ReservationReasonCode.BROKER_CORRECTION
    if uncertain:
        reason = ReservationReasonCode.EXECUTION_UNKNOWN

    detail = _detail(
        state=state,
        reason=reason,
        blocked=bool(uncertain),
        consumed=consumed_target,
        released=released_target,
        from_actual=from_actual,
    )
    return ReservationOutcome(
        state=resolved_state,
        reason_code=reason,
        event_type=_event_type(resolved_state, reason),
        consumed_delta=consumed_target - reservation.consumed_amount,
        released_delta=released_target - reservation.released_amount,
        over_delta=over - reservation.over_authorized_amount,
        consumed_quantity_delta=min(filled, reservation.authorized_quantity)
        - reservation.consumed_quantity,
        consumed_from_actual_fills=from_actual,
        execution_id=execution_id,
        broker_order_id=broker_order_id,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _hold(
    reservation: Reservation, reason: ReservationReasonCode, detail: str
) -> ReservationOutcome:
    """Change nothing, and say why. The default answer whenever proof is absent."""
    state = (
        ReservationState.UNKNOWN
        if reason is ReservationReasonCode.EXECUTION_UNKNOWN
        else reservation.state
    )
    return ReservationOutcome(
        state=state,
        reason_code=reason,
        event_type=(
            ReservationEventType.RESERVATION_RETAINED_UNKNOWN
            if state is ReservationState.UNKNOWN
            else ReservationEventType.RESERVATION_OBSERVED
        ),
        detail=detail,
    )


def _currency_matches(
    reservation: Reservation, record: object, policy: ReservationPolicyConfig
) -> tuple[bool, str]:
    """Whether the fill's money is in the campaign's unit of account.

    No FX. Milestone 7 refuses to size a position in a currency the campaign
    does not hold and no deterministic rate source exists; consuming a
    reservation at an invented rate here would undo that refusal at the one
    point where the number becomes permanent.
    """
    currency = getattr(record, "currency", None)
    if currency is None or currency == reservation.currency:
        return True, ""
    if policy.allow_currency_conversion:  # pragma: no cover - refused at config load
        return True, ""
    return False, (
        f"the execution settled in {currency} but this reservation is denominated in "
        f"{reservation.currency}, and no deterministic FX rate source exists. The capital is "
        f"left committed rather than consumed at an invented rate"
    )


def _consumed_target(
    reservation: Reservation,
    record: object,
    filled: int,
    policy: ReservationPolicyConfig,
) -> tuple[Decimal, bool]:
    """How much capital the fills actually took, and whether that came from them.

    Nothing filled, or consumption switched off, means *unchanged* — not zero.
    Returning zero would produce a negative delta against a reservation that
    has already consumed capital, quietly handing spent money back to the
    campaign.
    """
    if filled <= 0 or not policy.consume_on_fill:
        return reservation.consumed_amount, reservation.consumed_from_actual_fills
    actual = executed_capital(record) if policy.use_actual_fill_economics else None
    if actual is not None:
        return actual, True
    return authorised_capital_for(reservation, filled=filled), False


def _released_target(
    reservation: Reservation,
    *,
    state: ExecutionState,
    filled: int,
    consumed: Decimal,
    policy: ReservationPolicyConfig,
    blocked: bool,
) -> tuple[Decimal, ReservationReasonCode]:
    """How much capital returns to the campaign, and on what evidence."""
    unspent = max(reservation.authorized_amount - consumed, Decimal("0"))

    if blocked:
        # An unresolved attempt exists. Nothing is released, whatever else the
        # broker has told us about the other attempts.
        return reservation.released_amount, ReservationReasonCode.EXECUTION_UNKNOWN

    if state is ExecutionState.FAILED:
        if policy.release_on_execution_failed:
            return unspent, ReservationReasonCode.EXECUTION_FAILED_BEFORE_SUBMISSION
        return reservation.released_amount, (
            ReservationReasonCode.EXECUTION_FAILED_BEFORE_SUBMISSION
        )

    if state is ExecutionState.REJECTED:
        if policy.release_on_broker_rejected:
            return unspent, ReservationReasonCode.BROKER_REJECTED
        return reservation.released_amount, ReservationReasonCode.BROKER_REJECTED

    if state is ExecutionState.CANCELLED:
        reason = (
            ReservationReasonCode.CANCELLED_AFTER_PARTIAL_FILL
            if filled > 0
            else ReservationReasonCode.CANCELLED_WITHOUT_FILL
        )
        if policy.release_on_cancelled_without_fill:
            # A cancellation after a partial fill releases only the remainder;
            # the filled part is already consumed above. Releasing the whole
            # reservation would return capital that is sitting in a position.
            return unspent, reason
        return reservation.released_amount, reason

    if state is ExecutionState.EXPIRED:
        reason = (
            ReservationReasonCode.CANCELLED_AFTER_PARTIAL_FILL
            if filled > 0
            else ReservationReasonCode.EXPIRED_WITHOUT_FILL
        )
        if policy.release_on_expired_without_fill:
            return unspent, reason
        return reservation.released_amount, reason

    if state is ExecutionState.FILLED:
        # The trade is done. Anything the fill did not use is genuinely free —
        # a limit order that filled below its limit costs less than authorised.
        return unspent, ReservationReasonCode.FILLED

    if state is ExecutionState.PARTIALLY_FILLED:
        # The remainder may still fill. Only a terminal outcome frees it, and
        # release_remainder_on_partial_fill fails to load precisely so that
        # this cannot be configured away.
        return reservation.released_amount, ReservationReasonCode.PARTIALLY_FILLED

    if state is ExecutionState.UNKNOWN:
        return reservation.released_amount, ReservationReasonCode.EXECUTION_UNKNOWN

    return reservation.released_amount, ReservationReasonCode.ORDER_WORKING


def _state_for(
    *, consumed: Decimal, released: Decimal, remaining: Decimal, blocked: bool
) -> ReservationState:
    if blocked and remaining > 0:
        return ReservationState.UNKNOWN
    if consumed > 0 and remaining > 0:
        return ReservationState.PARTIALLY_CONSUMED
    if consumed > 0:
        return ReservationState.CONSUMED
    if released > 0 and remaining == 0:
        return ReservationState.RELEASED
    return ReservationState.RESERVED


def _event_type(state: ReservationState, reason: ReservationReasonCode) -> ReservationEventType:
    if state is ReservationState.UNKNOWN:
        return ReservationEventType.RESERVATION_RETAINED_UNKNOWN
    if state is ReservationState.CONSUMED:
        return ReservationEventType.RESERVATION_CONSUMED
    if state is ReservationState.PARTIALLY_CONSUMED:
        return ReservationEventType.RESERVATION_PARTIALLY_CONSUMED
    if state is ReservationState.RELEASED:
        return ReservationEventType.RESERVATION_RELEASED
    if reason is ReservationReasonCode.BROKER_CORRECTION:
        return ReservationEventType.RESERVATION_CORRECTED
    return ReservationEventType.RESERVATION_OBSERVED


def _detail(
    *,
    state: ExecutionState,
    reason: ReservationReasonCode,
    blocked: bool,
    consumed: Decimal,
    released: Decimal,
    from_actual: bool,
) -> str:
    if blocked:
        return (
            "an execution against this authorisation is UNKNOWN, so its capital stays "
            "committed. The order may be live at the broker; only observing the broker can "
            "settle it, and no amount of elapsed time is evidence"
        )
    basis = "actual fill economics" if from_actual else "the authorisation's own unit cost"
    return (
        f"execution is {state.value}: consumed {consumed} ({basis}), released {released} "
        f"({reason.value})"
    )
