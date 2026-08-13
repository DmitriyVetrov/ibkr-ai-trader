"""Reservation invariants that must hold whatever route produced the record.

Property-style coverage of brief section 43 over the *service*, not the model:
whatever sequence of execution states a reservation is put through, the
accounting identity holds, nothing goes negative, and no capital is created or
destroyed.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from tests.positions.factories import execution_record
from trading_system.domain.enums import ExecutionState

pytestmark = pytest.mark.unit

#: Every state an execution can be in that a reservation must survive.
STATES = (
    ExecutionState.SUBMISSION_PENDING,
    ExecutionState.SUBMITTED,
    ExecutionState.PARTIALLY_FILLED,
    ExecutionState.FILLED,
    ExecutionState.CANCELLED,
    ExecutionState.EXPIRED,
    ExecutionState.REJECTED,
    ExecutionState.FAILED,
    ExecutionState.UNKNOWN,
)


def _filled_for(state: ExecutionState) -> int:
    if state is ExecutionState.FILLED:
        return 2
    if state is ExecutionState.PARTIALLY_FILLED:
        return 1
    return 0


@pytest.mark.parametrize("state", STATES)
def test_the_accounting_identity_holds_for_every_execution_state(
    service, execution_store, state
) -> None:
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id,
            state=state,
            quantity=2,
            filled_quantity=_filled_for(state),
            average_fill_price=Decimal("5.00"),
        )
    )

    service.apply_executions()
    updated = service.get(held.reservation_id)

    assert updated is not None
    total = updated.consumed_amount + updated.released_amount + updated.remaining_amount
    assert total == updated.authorized_amount + updated.over_authorized_amount


@pytest.mark.parametrize("state", STATES)
def test_no_amount_ever_goes_negative(service, execution_store, state) -> None:
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id,
            state=state,
            quantity=2,
            filled_quantity=_filled_for(state),
            average_fill_price=Decimal("5.00"),
        )
    )

    service.apply_executions()
    updated = service.get(held.reservation_id)

    assert updated is not None
    assert updated.consumed_amount >= 0
    assert updated.released_amount >= 0
    assert updated.remaining_amount >= 0


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pair
        for pair in itertools.product(
            (ExecutionState.SUBMITTED, ExecutionState.PARTIALLY_FILLED, ExecutionState.UNKNOWN),
            (
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.FILLED,
                ExecutionState.CANCELLED,
            ),
        )
    ],
)
def test_a_progression_never_creates_or_destroys_capital(
    service, execution_store, first, second
) -> None:
    """Whatever order the broker reports things in, the envelope is conserved."""
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id,
            state=first,
            quantity=2,
            filled_quantity=_filled_for(first),
            average_fill_price=Decimal("5.00"),
        )
    )
    service.apply_executions()

    service.apply_executions(
        executions=[
            execution_record(
                allocation_id=held.allocation_id,
                state=second,
                quantity=2,
                filled_quantity=_filled_for(second),
                average_fill_price=Decimal("5.00"),
            )
        ]
    )

    updated = service.get(held.reservation_id)
    assert updated is not None
    assert (
        updated.consumed_amount + updated.released_amount + updated.remaining_amount
        == updated.authorized_amount + updated.over_authorized_amount
    )


def test_applying_the_same_state_twice_moves_nothing(service, execution_store) -> None:
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id,
            state=ExecutionState.FILLED,
            quantity=2,
            filled_quantity=2,
            average_fill_price=Decimal("5.00"),
        )
    )

    service.apply_executions()
    once = service.get(held.reservation_id)
    service.apply_executions()
    twice = service.get(held.reservation_id)

    assert once is not None and twice is not None
    assert once.consumed_amount == twice.consumed_amount
    assert once.released_amount == twice.released_amount
    assert once.remaining_amount == twice.remaining_amount


def test_the_campaign_total_never_exceeds_the_allocatable_budget(service, execution_store) -> None:
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id,
            state=ExecutionState.FILLED,
            quantity=2,
            filled_quantity=2,
            average_fill_price=Decimal("5.00"),
        )
    )
    service.apply_executions()

    capital = service.capital()

    assert capital.committed_total <= capital.allocatable
    assert capital.available == capital.allocatable - capital.committed_total


def test_syncing_twice_creates_one_reservation_per_authorisation(service) -> None:
    first = service.sync()
    second = service.sync()

    assert len(first.created) == 1
    assert second.created == ()
    assert len(second.existing) == 1
    assert len(service.all()) == 1


def test_a_dry_run_application_writes_nothing(service, execution_store) -> None:
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.FAILED, filled_quantity=0
        )
    )

    updates = service.apply_executions(dry_run=True)

    assert updates[0].applied is False
    assert service.get(held.reservation_id).state is held.state
    assert service.repository.events(held.reservation_id) == []
