"""Partial fills and partial consumption (brief sections 23, 25).

Two rows of the table, and both are about *not* rounding:

* a partial fill consumes what filled and leaves the rest committed, because
  the remainder may still be working at the broker;
* a cancellation after a partial fill consumes the filled portion and releases
  only the remainder — never the whole reservation, which would return capital
  that is sitting in a position.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.positions.factories import NOW, execution_record, reservation
from trading_system.domain.enums import (
    ExecutionState,
    ReservationReasonCode,
    ReservationState,
)
from trading_system.infrastructure.settings import ReservationPolicyConfig
from trading_system.reservations.lifecycle import resolve_reservation

pytestmark = pytest.mark.unit

#: 1,000.00 over 10 units, so the arithmetic is readable at a glance.
AUTHORIZED = Decimal("1000.00")


def _held():
    return reservation(authorized=AUTHORIZED, quantity=10)


def _apply(held, outcome):
    return held.with_event(
        outcome.to_event(held, sequence=0, occurred_at=NOW, observed_at=NOW, source="test")
    )


def test_a_partial_fill_consumes_only_what_filled(policy) -> None:
    held = _held()
    outcome = resolve_reservation(
        held,
        [
            execution_record(
                state=ExecutionState.PARTIALLY_FILLED,
                quantity=10,
                filled_quantity=4,
                average_fill_price=Decimal("1.00"),
                multiplier=100,
            )
        ],
        policy=policy,
    )
    updated = _apply(held, outcome)

    assert updated.state is ReservationState.PARTIALLY_CONSUMED
    assert updated.consumed_amount == Decimal("400.00")
    assert updated.remaining_amount == Decimal("600.00")
    assert updated.released_amount == Decimal("0")


def test_the_remainder_of_a_working_order_is_not_released(policy) -> None:
    """It may still fill. Only a terminal outcome frees it."""
    outcome = resolve_reservation(
        _held(),
        [
            execution_record(
                state=ExecutionState.PARTIALLY_FILLED,
                quantity=10,
                filled_quantity=4,
                average_fill_price=Decimal("1.00"),
            )
        ],
        policy=policy,
    )
    assert outcome.released_delta == Decimal("0")
    assert outcome.reason_code is ReservationReasonCode.PARTIALLY_FILLED


def test_a_cancellation_after_a_partial_fill_consumes_and_releases_the_rest(policy) -> None:
    held = _held()
    outcome = resolve_reservation(
        held,
        [
            execution_record(
                state=ExecutionState.CANCELLED,
                quantity=10,
                filled_quantity=4,
                average_fill_price=Decimal("1.00"),
                multiplier=100,
            )
        ],
        policy=policy,
    )
    updated = _apply(held, outcome)

    assert updated.consumed_amount == Decimal("400.00")
    assert updated.released_amount == Decimal("600.00")
    assert updated.remaining_amount == Decimal("0")
    assert updated.state is ReservationState.CONSUMED


def test_a_cancellation_after_a_partial_fill_does_not_release_everything(policy) -> None:
    """Releasing the whole reservation would free capital sitting in a position."""
    held = _held()
    outcome = resolve_reservation(
        held,
        [
            execution_record(
                state=ExecutionState.CANCELLED,
                quantity=10,
                filled_quantity=4,
                average_fill_price=Decimal("1.00"),
            )
        ],
        policy=policy,
    )
    assert outcome.released_delta < AUTHORIZED
    assert outcome.reason_code is ReservationReasonCode.CANCELLED_AFTER_PARTIAL_FILL


def test_a_later_complete_fill_consumes_the_rest(policy) -> None:
    held = _held()
    partial = _apply(
        held,
        resolve_reservation(
            held,
            [
                execution_record(
                    state=ExecutionState.PARTIALLY_FILLED,
                    quantity=10,
                    filled_quantity=4,
                    average_fill_price=Decimal("1.00"),
                )
            ],
            policy=policy,
        ),
    )
    complete = _apply(
        partial,
        resolve_reservation(
            partial,
            [
                execution_record(
                    state=ExecutionState.FILLED,
                    quantity=10,
                    filled_quantity=10,
                    average_fill_price=Decimal("1.00"),
                )
            ],
            policy=policy,
        ),
    )

    assert complete.consumed_amount == Decimal("1000.00")
    assert complete.remaining_amount == Decimal("0")
    assert complete.state is ReservationState.CONSUMED


def test_the_consumed_quantity_tracks_the_filled_units(policy) -> None:
    held = _held()
    outcome = resolve_reservation(
        held,
        [
            execution_record(
                state=ExecutionState.PARTIALLY_FILLED,
                quantity=10,
                filled_quantity=4,
                average_fill_price=Decimal("1.00"),
            )
        ],
        policy=policy,
    )
    assert _apply(held, outcome).consumed_quantity == 4


def test_a_broker_that_charged_more_than_was_authorised_is_recorded_as_a_correction(
    policy,
) -> None:
    """Not smoothed over, and not a crash: the overrun is a fact worth keeping."""
    held = _held()
    outcome = resolve_reservation(
        held,
        [
            execution_record(
                state=ExecutionState.FILLED,
                quantity=10,
                filled_quantity=10,
                average_fill_price=Decimal("1.50"),
                multiplier=100,
            )
        ],
        policy=policy,
    )
    updated = _apply(held, outcome)

    assert updated.consumed_amount == Decimal("1500.00")
    assert updated.over_authorized_amount == Decimal("500.00")
    assert ReservationReasonCode.BROKER_CORRECTION in updated.reason_codes
    assert updated.authorized_amount == AUTHORIZED


def test_the_configuration_that_would_free_a_working_remainder_fails_to_load() -> None:
    with pytest.raises(ValidationError, match="may still be working"):
        ReservationPolicyConfig(release_remainder_on_partial_fill=True)
