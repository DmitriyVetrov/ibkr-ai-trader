"""Running reconciliation twice changes nothing (brief sections 37, 79).

The claim, stated economically rather than structurally: a second run over
identical broker state must not duplicate a fill, duplicate a position, release
capital twice, consume capital twice or create a second economic record. It may
record that it looked again — that is a re-observation, and it is the only
thing that changes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.positions.factories import (
    ACCOUNT,
    broker_execution,
    execution_record,
    option_position,
)
from trading_system.broker.simulator import SimulatedBrokerState
from trading_system.domain.enums import ExecutionState

pytestmark = pytest.mark.unit


@pytest.fixture
def account() -> SimulatedBrokerState:
    return SimulatedBrokerState(
        account_id=ACCOUNT,
        currency="EUR",
        positions=[option_position()],
        open_orders=[],
        executions=[broker_execution()],
    )


def test_a_second_run_lands_on_the_same_reconciliation(make_service, account) -> None:
    service = make_service(account)
    first = service.run()
    second = service.run()

    assert first.result.reconciliation_id == second.result.reconciliation_id
    assert first.result.content_hash == second.result.content_hash
    assert first.is_new is True
    assert second.is_new is False


def test_a_second_run_records_a_re_observation_not_a_second_comparison(
    make_service, account
) -> None:
    service = make_service(account)
    service.run()
    service.run()

    history = service.history()
    assert len(history) == 2
    assert sum(entry.reobserved for entry in history) == 1


def test_a_second_run_records_no_second_fill(make_service, account) -> None:
    service = make_service(account)
    first = service.run()
    second = service.run()

    assert len(first.capture.recorded_fills) == 1
    assert second.capture.recorded_fills == ()
    assert len(service.positions.fills.all()) == 1


def test_a_second_run_records_no_second_position(make_service, account) -> None:
    service = make_service(account)
    first = service.run()
    second = service.run()

    assert first.capture.snapshot.snapshot_id == second.capture.snapshot.snapshot_id
    stored = {entry.snapshot_id for entry in service.positions.repository.history()}
    assert len(stored) == 1


def test_a_second_run_does_not_release_capital_twice(make_service, account) -> None:
    service = make_service(account)
    service.run()
    [held] = service.reservations.all()
    service.executions.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.FAILED, filled_quantity=0
        )
    )

    service.run()
    once = service.reservations.get(held.reservation_id)
    service.run()
    twice = service.reservations.get(held.reservation_id)

    assert once is not None and twice is not None
    assert once.released_amount == twice.released_amount
    assert twice.released_amount == held.authorized_amount


def test_a_second_run_does_not_consume_capital_twice(make_service, account) -> None:
    service = make_service(account)
    service.run()
    [held] = service.reservations.all()
    service.executions.seed(
        execution_record(
            allocation_id=held.allocation_id,
            state=ExecutionState.FILLED,
            quantity=2,
            filled_quantity=2,
            average_fill_price=Decimal("5.00"),
        )
    )

    service.run()
    once = service.reservations.get(held.reservation_id)
    service.run()
    twice = service.reservations.get(held.reservation_id)

    assert once is not None and twice is not None
    assert once.consumed_amount == twice.consumed_amount == Decimal("1000.00")


def test_a_second_run_appends_no_second_reservation_event(make_service, account) -> None:
    service = make_service(account)
    service.run()
    [held] = service.reservations.all()
    service.executions.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.FAILED, filled_quantity=0
        )
    )

    service.run()
    after_first = len(service.reservations.repository.events(held.reservation_id))
    service.run()
    after_second = len(service.reservations.repository.events(held.reservation_id))

    assert after_first == after_second == 1


def test_a_second_run_does_not_resolve_an_execution_twice(make_service, account) -> None:
    service = make_service(account)
    service.run()
    [held] = service.reservations.all()
    record = execution_record(
        allocation_id=held.allocation_id, state=ExecutionState.UNKNOWN, filled_quantity=0
    )
    service.executions.seed(record)

    service.run()
    after_first = len(service.executions.events(record.execution_id))
    service.run()
    after_second = len(service.executions.events(record.execution_id))

    assert after_first == after_second


def test_the_campaign_capital_is_unchanged_by_a_second_run(make_service, account) -> None:
    """The property that matters most: economic state is identical."""
    service = make_service(account)
    service.run()
    before = service.reservations.capital()
    service.run()
    after = service.reservations.capital()

    assert before.committed_total == after.committed_total
    assert before.consumed_total == after.consumed_total
    assert before.released_total == after.released_total
    assert before.available == after.available


def test_neither_run_submits_an_order(make_service, account) -> None:
    service = make_service(account)
    first = service.run()
    second = service.run()

    assert first.orders_submitted == 0
    assert second.orders_submitted == 0
    assert service.broker.orders_submitted == 0


def test_a_dry_run_writes_nothing_at_all(make_service, account) -> None:
    service = make_service(account)
    run = service.run(dry_run=True)

    assert run.stored is False
    assert service.latest() is None
    assert service.positions.repository.history() == []
    assert service.positions.fills.all() == []
    assert run.orders_submitted == 0
