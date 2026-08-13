"""The reservation ledger's storage (brief sections 41-42).

Immutable base records, append-only events, and a current state that is a
*fold* of the history rather than a mutable balance. The property that matters
most: a replayed event records nothing, which is what stops a second
reconciliation over unchanged broker state from consuming or releasing the same
capital twice.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.positions.factories import NOW, reservation
from trading_system.domain.enums import (
    ReservationEventType,
    ReservationReasonCode,
    ReservationState,
)
from trading_system.reservations.models import ReservationEvent
from trading_system.reservations.store import ReservationStoreError

pytestmark = pytest.mark.unit


def _event(held, *, sequence: int = 0, consumed=Decimal("0"), released=Decimal("0")):
    state = ReservationState.RESERVED
    if consumed and not released:
        state = ReservationState.PARTIALLY_CONSUMED
    if released and not consumed:
        state = ReservationState.RELEASED
    if consumed and released:
        state = ReservationState.CONSUMED
    return ReservationEvent(
        event_id=f"resevt-{sequence}",
        reservation_id=held.reservation_id,
        sequence=sequence,
        event_type=ReservationEventType.RESERVATION_OBSERVED,
        state=state,
        occurred_at=NOW,
        observed_at=NOW,
        source="test",
        consumed_delta=consumed,
        released_delta=released,
        reason_code=ReservationReasonCode.FILLED if consumed else None,
    )


def test_a_reservation_round_trips(reservation_repository) -> None:
    held = reservation()
    reservation_repository.save(held)
    assert reservation_repository.base(held.reservation_id) == held
    assert reservation_repository.current(held.reservation_id) == held


def test_a_stored_reservation_is_immutable(reservation_repository) -> None:
    held = reservation()
    reservation_repository.save(held)
    with pytest.raises(ReservationStoreError, match="immutable"):
        reservation_repository.save(held.model_copy(update={"symbol": "SPY"}))


def test_saving_the_identical_reservation_again_is_accepted(reservation_repository) -> None:
    held = reservation()
    reservation_repository.save(held)
    reservation_repository.save(held)
    assert len(reservation_repository.history()) == 1


def test_the_current_record_is_a_fold_of_its_events(reservation_repository) -> None:
    held = reservation(authorized=Decimal("1000.00"))
    reservation_repository.save(held)
    reservation_repository.append_event(_event(held, sequence=0, consumed=Decimal("400.00")))

    current = reservation_repository.current(held.reservation_id)

    assert current is not None
    assert current.consumed_amount == Decimal("400.00")
    assert current.remaining_amount == Decimal("600.00")


def test_the_base_record_still_shows_what_was_authorised(reservation_repository) -> None:
    """A consumption appends; it does not edit."""
    held = reservation(authorized=Decimal("1000.00"))
    reservation_repository.save(held)
    reservation_repository.append_event(_event(held, sequence=0, consumed=Decimal("400.00")))

    base = reservation_repository.base(held.reservation_id)

    assert base is not None
    assert base.consumed_amount == Decimal("0")
    assert base.remaining_amount == Decimal("1000.00")


def test_a_replayed_event_records_nothing(reservation_repository) -> None:
    """The property that keeps a second reconciliation economically inert."""
    held = reservation(authorized=Decimal("1000.00"))
    reservation_repository.save(held)
    event = _event(held, sequence=0, consumed=Decimal("400.00"))

    assert reservation_repository.append_event(event) is True
    assert reservation_repository.append_event(event) is False

    current = reservation_repository.current(held.reservation_id)
    assert current is not None
    assert current.consumed_amount == Decimal("400.00")
    assert len(reservation_repository.events(held.reservation_id)) == 1


def test_events_are_replayed_in_sequence_order(reservation_repository) -> None:
    held = reservation(authorized=Decimal("1000.00"))
    reservation_repository.save(held)
    reservation_repository.append_event(_event(held, sequence=1, consumed=Decimal("100.00")))
    reservation_repository.append_event(_event(held, sequence=0, consumed=Decimal("300.00")))

    events = reservation_repository.events(held.reservation_id)

    assert [event.sequence for event in events] == [0, 1]
    current = reservation_repository.current(held.reservation_id)
    assert current is not None
    assert current.consumed_amount == Decimal("400.00")


def test_a_history_that_cannot_be_replayed_raises(reservation_repository) -> None:
    """A contradiction in a money ledger is worth surfacing loudly."""
    held = reservation(authorized=Decimal("1000.00"))
    reservation_repository.save(held)
    reservation_repository.append_event(_event(held, sequence=0, consumed=Decimal("5000.00")))

    with pytest.raises(ReservationStoreError, match="cannot be replayed"):
        reservation_repository.current(held.reservation_id)


def test_a_reservation_can_be_found_by_its_authorisation(reservation_repository) -> None:
    held = reservation()
    reservation_repository.save(held)
    found = reservation_repository.for_allocation(held.allocation_id)
    assert found is not None
    assert found.reservation_id == held.reservation_id


def test_reservations_can_be_listed_for_one_campaign(reservation_repository) -> None:
    reservation_repository.save(reservation())
    reservation_repository.save(
        reservation(
            allocation_id="allocation-2",
            opportunity_id="opportunity-2",
            campaign_id="campaign-other",
        )
    )
    assert len(reservation_repository.for_campaign("campaign-001")) == 1
    assert len(reservation_repository.all_current()) == 2


def test_an_unknown_reservation_is_none_rather_than_an_error(reservation_repository) -> None:
    assert reservation_repository.current("reservation-nope") is None
    assert reservation_repository.for_allocation("allocation-nope") is None
