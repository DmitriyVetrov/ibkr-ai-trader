"""The guarded release path (brief section 69).

``reservations release`` exists so an operator can return capital the system
has proof was never spent. It is deliberately narrow, and the refusals are the
point:

* it refuses while any execution against the authorisation is ``UNKNOWN``;
* it refuses while an order is working at the broker;
* there is no force-release, in the service or in the CLI.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.positions.factories import execution_record
from trading_system.domain.enums import (
    ExecutionState,
    ReservationReasonCode,
    ReservationState,
)

pytestmark = pytest.mark.unit


def test_a_release_returns_capital_when_the_execution_provably_failed(
    service, execution_store
) -> None:
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.FAILED, filled_quantity=0
        )
    )

    update = service.release(held.reservation_id)

    assert update.applied is True
    assert update.reservation.state is ReservationState.RELEASED
    assert update.reservation.released_amount == held.authorized_amount


def test_a_release_is_refused_while_an_execution_is_unknown(service, execution_store) -> None:
    """The refusal that makes every other guard in this milestone worth having."""
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.UNKNOWN, filled_quantity=0
        )
    )

    update = service.release(held.reservation_id)

    assert update.applied is False
    assert update.outcome.reason_code is ReservationReasonCode.RELEASE_REFUSED_UNKNOWN
    assert "may be live at the broker" in update.outcome.detail
    assert service.get(held.reservation_id).committed_amount == held.authorized_amount


def test_a_release_is_refused_while_an_order_is_working(service, execution_store) -> None:
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.SUBMITTED, filled_quantity=0
        )
    )

    update = service.release(held.reservation_id)

    assert update.applied is False
    assert update.outcome.reason_code is ReservationReasonCode.ORDER_WORKING


def test_releasing_an_unexecuted_authorisation_moves_nothing(service) -> None:
    """Not having executed is not proof that nothing will be sent."""
    service.sync()
    [held] = service.all()

    update = service.release(held.reservation_id)

    assert update.applied is False
    assert update.outcome.reason_code is ReservationReasonCode.NOT_EXECUTED


def test_releasing_twice_returns_the_capital_once(service, execution_store) -> None:
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.FAILED, filled_quantity=0
        )
    )

    first = service.release(held.reservation_id)
    second = service.release(held.reservation_id)

    assert first.applied is True
    assert second.applied is False
    assert service.get(held.reservation_id).released_amount == held.authorized_amount


def test_releasing_an_unknown_reservation_id_raises(service) -> None:
    service.sync()
    with pytest.raises(KeyError):
        service.release("reservation-nope")


def test_the_service_exposes_no_force_release() -> None:
    """A dangerous generic escape hatch is not something to have and not use."""
    from trading_system.reservations.service import ReservationService

    members = dir(ReservationService)
    assert "force_release" not in members
    assert not any("force" in name for name in members)


def test_a_release_frees_capital_for_the_campaign(service, execution_store) -> None:
    service.sync()
    [held] = service.all()
    before = service.capital()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.FAILED, filled_quantity=0
        )
    )

    service.release(held.reservation_id)
    after = service.capital()

    assert after.available == before.available + held.authorized_amount
    assert after.released_total == held.authorized_amount
    assert after.committed_total == Decimal("0")
