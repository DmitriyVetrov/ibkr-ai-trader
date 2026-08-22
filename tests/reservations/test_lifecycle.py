"""When committed capital may move (brief sections 21-26, 76).

The table this file pins, one test per row:

.. code-block:: text

    no execution yet         -> RESERVED   nothing moves
    FAILED before submission -> RELEASED   proof it never left the process
    REJECTED by the broker   -> RELEASED   the broker refused it
    CANCELLED, no fill       -> RELEASED   the broker cancelled it unfilled
    EXPIRED, no fill         -> RELEASED   it stopped working unfilled
    SUBMITTED                -> RESERVED   the order is working
    PARTIALLY_FILLED         -> PARTIALLY_CONSUMED
    FILLED                   -> CONSUMED   at what actually traded
    UNKNOWN                  -> UNKNOWN    the capital stays locked
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.positions.factories import NOW, execution_record, reservation
from trading_system.domain.enums import (
    ExecutionState,
    ReservationEventType,
    ReservationReasonCode,
    ReservationState,
)
from trading_system.reservations.lifecycle import (
    authorised_capital_for,
    dominant_execution,
    executed_capital,
    resolve_reservation,
)
from trading_system.reservations.models import Reservation

pytestmark = pytest.mark.unit

AUTHORIZED = Decimal("1190.00")


def _held(**kwargs) -> Reservation:
    return reservation(authorized=AUTHORIZED, quantity=2, **kwargs)


def _apply(held, outcome):
    """Fold an outcome onto a reservation, exactly as the service does."""
    return held.with_event(
        outcome.to_event(held, sequence=0, occurred_at=NOW, observed_at=NOW, source="test")
    )


# ---------------------------------------------------------------------------
# Nothing to go on
# ---------------------------------------------------------------------------
def test_an_authorisation_nobody_executed_keeps_its_capital(policy) -> None:
    """Not having executed is not evidence that nothing will be sent."""
    outcome = resolve_reservation(_held(), [], policy=policy)
    assert outcome.state is ReservationState.RESERVED
    assert outcome.reason_code is ReservationReasonCode.NOT_EXECUTED
    assert outcome.changed is False


# ---------------------------------------------------------------------------
# Proof the capital was not spent (brief section 21)
# ---------------------------------------------------------------------------
def test_a_failed_execution_releases_the_capital(policy) -> None:
    outcome = resolve_reservation(
        _held(), [execution_record(state=ExecutionState.FAILED, filled_quantity=0)], policy=policy
    )
    assert outcome.state is ReservationState.RELEASED
    assert outcome.reason_code is ReservationReasonCode.EXECUTION_FAILED_BEFORE_SUBMISSION
    assert outcome.released_delta == AUTHORIZED


def test_a_broker_rejection_releases_the_capital(policy) -> None:
    outcome = resolve_reservation(
        _held(),
        [execution_record(state=ExecutionState.REJECTED, filled_quantity=0)],
        policy=policy,
    )
    assert outcome.state is ReservationState.RELEASED
    assert outcome.reason_code is ReservationReasonCode.BROKER_REJECTED


def test_a_cancellation_with_no_fill_releases_the_capital(policy) -> None:
    outcome = resolve_reservation(
        _held(),
        [execution_record(state=ExecutionState.CANCELLED, filled_quantity=0)],
        policy=policy,
    )
    assert outcome.state is ReservationState.RELEASED
    assert outcome.reason_code is ReservationReasonCode.CANCELLED_WITHOUT_FILL
    assert outcome.released_delta == AUTHORIZED


def test_an_expiry_with_no_fill_releases_the_capital(policy) -> None:
    outcome = resolve_reservation(
        _held(), [execution_record(state=ExecutionState.EXPIRED, filled_quantity=0)], policy=policy
    )
    assert outcome.state is ReservationState.RELEASED
    assert outcome.reason_code is ReservationReasonCode.EXPIRED_WITHOUT_FILL


# ---------------------------------------------------------------------------
# A working order keeps its capital
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "state",
    [ExecutionState.SUBMITTED, ExecutionState.SUBMISSION_PENDING, ExecutionState.CANCEL_PENDING],
)
def test_a_working_order_keeps_its_capital(policy, state) -> None:
    outcome = resolve_reservation(
        _held(), [execution_record(state=state, filled_quantity=0)], policy=policy
    )
    assert outcome.state is ReservationState.RESERVED
    assert outcome.reason_code is ReservationReasonCode.ORDER_WORKING
    assert outcome.released_delta == Decimal("0")


# ---------------------------------------------------------------------------
# Fills consume (brief section 24)
# ---------------------------------------------------------------------------
def test_a_full_fill_consumes_what_actually_traded(policy) -> None:
    outcome = resolve_reservation(
        _held(),
        [
            execution_record(
                state=ExecutionState.FILLED,
                quantity=2,
                filled_quantity=2,
                average_fill_price=Decimal("5.90"),
                multiplier=100,
            )
        ],
        policy=policy,
    )
    assert outcome.state is ReservationState.CONSUMED
    assert outcome.consumed_delta == Decimal("1180.00")
    assert outcome.consumed_from_actual_fills is True


def test_a_full_fill_releases_what_the_market_did_not_charge(policy) -> None:
    """A limit order that filled below its limit costs less than authorised."""
    held = _held()
    outcome = resolve_reservation(
        held,
        [
            execution_record(
                state=ExecutionState.FILLED,
                quantity=2,
                filled_quantity=2,
                average_fill_price=Decimal("5.90"),
            )
        ],
        policy=policy,
    )
    updated = _apply(held, outcome)
    assert updated.consumed_amount == Decimal("1180.00")
    assert updated.released_amount == Decimal("10.00")
    assert updated.remaining_amount == Decimal("0")


def test_both_the_authorised_and_the_executed_figure_survive(policy) -> None:
    held = _held()
    outcome = resolve_reservation(
        held,
        [
            execution_record(
                state=ExecutionState.FILLED, filled_quantity=2, average_fill_price=Decimal("5.90")
            )
        ],
        policy=policy,
    )
    updated = _apply(held, outcome)
    assert updated.authorized_amount == AUTHORIZED
    assert updated.consumed_amount == Decimal("1180.00")


def test_the_multiplier_is_in_the_money_figure() -> None:
    """A reservation consuming 11.80 where 1,180.00 was meant is the bug here."""
    record = execution_record(
        state=ExecutionState.FILLED,
        filled_quantity=2,
        average_fill_price=Decimal("5.90"),
        multiplier=100,
    )
    assert executed_capital(record) == Decimal("1180.00")


def test_an_execution_with_no_fill_price_has_no_executed_capital() -> None:
    record = execution_record(state=ExecutionState.SUBMITTED, filled_quantity=0)
    assert executed_capital(record) is None


def test_without_broker_economics_the_authorisations_own_arithmetic_is_used(policy) -> None:
    """Exact, not estimated: Milestone 7's unit cost times what actually traded."""
    held = _held()
    record = execution_record(
        state=ExecutionState.PARTIALLY_FILLED, quantity=2, filled_quantity=1, multiplier=0
    )
    outcome = resolve_reservation(held, [record], policy=policy)
    assert outcome.consumed_from_actual_fills is False
    assert outcome.consumed_delta == authorised_capital_for(held, filled=1)
    assert outcome.consumed_delta == Decimal("595.00")


# ---------------------------------------------------------------------------
# Precedence between several attempts
# ---------------------------------------------------------------------------
def test_a_fill_outranks_a_refusal_when_both_exist() -> None:
    records = [
        execution_record(execution_id="execution-a", state=ExecutionState.REJECTED),
        execution_record(execution_id="execution-b", state=ExecutionState.FILLED),
    ]
    dominant = dominant_execution(records)
    assert dominant is not None
    assert dominant.state is ExecutionState.FILLED


def test_an_unknown_outranks_a_working_order() -> None:
    records = [
        execution_record(execution_id="execution-a", state=ExecutionState.SUBMITTED),
        execution_record(execution_id="execution-b", state=ExecutionState.UNKNOWN),
    ]
    dominant = dominant_execution(records)
    assert dominant is not None
    assert dominant.state is ExecutionState.UNKNOWN


def test_precedence_breaks_ties_deterministically() -> None:
    records = [
        execution_record(execution_id="execution-b", state=ExecutionState.SUBMITTED),
        execution_record(execution_id="execution-a", state=ExecutionState.SUBMITTED),
    ]
    first = dominant_execution(records)
    second = dominant_execution(list(reversed(records)))
    assert first is not None and second is not None
    assert first.execution_id == second.execution_id


# ---------------------------------------------------------------------------
# Applying an outcome twice moves nothing (brief section 79)
# ---------------------------------------------------------------------------
def test_resolving_an_already_resolved_reservation_moves_nothing(policy) -> None:
    held = _held()
    records = [execution_record(state=ExecutionState.FILLED, filled_quantity=2)]
    updated = _apply(held, resolve_reservation(held, records, policy=policy))

    again = resolve_reservation(updated, records, policy=policy)

    assert again.consumed_delta == Decimal("0")
    assert again.released_delta == Decimal("0")
    assert again.changed is False


def test_a_release_is_never_applied_twice(policy) -> None:
    held = _held()
    records = [execution_record(state=ExecutionState.FAILED, filled_quantity=0)]
    released = _apply(held, resolve_reservation(held, records, policy=policy))

    again = resolve_reservation(released, records, policy=policy)

    assert again.released_delta == Decimal("0")
    assert released.released_amount == AUTHORIZED


# ---------------------------------------------------------------------------
# Currency (brief section 57)
# ---------------------------------------------------------------------------
def test_a_fill_in_another_currency_is_reported_not_converted(policy) -> None:
    """The reservation is in the campaign's traded currency; the fill is not.

    No FX here, deliberately, and this is not the same question the campaign's
    envelope answers. The envelope is converted once, at authorisation, against
    a rate captured with the account balance. This is the moment the number
    becomes permanent, there is no captured rate at hand, and consuming at one
    fetched now would undo the authorisation's arithmetic.
    """
    outcome = resolve_reservation(
        _held(),
        [execution_record(state=ExecutionState.FILLED, filled_quantity=2, currency="GBP")],
        policy=policy,
    )
    assert outcome.reason_code is ReservationReasonCode.CURRENCY_MISMATCH
    assert outcome.consumed_delta == Decimal("0")
    assert "invented rate" in outcome.detail


def test_an_event_carries_the_conclusion_that_produced_it(policy) -> None:
    held = _held()
    outcome = resolve_reservation(
        held, [execution_record(state=ExecutionState.FILLED, filled_quantity=2)], policy=policy
    )
    event = outcome.to_event(
        held, sequence=3, occurred_at=NOW, observed_at=NOW, source="reconciliation"
    )
    assert event.sequence == 3
    assert event.event_type is ReservationEventType.RESERVATION_CONSUMED
    assert event.reason_code is ReservationReasonCode.FILLED
    assert event.source == "reconciliation"
