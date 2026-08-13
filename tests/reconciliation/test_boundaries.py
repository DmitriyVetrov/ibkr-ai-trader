"""Reconciliation is read-only with respect to the broker (brief sections 28, 70, 81).

The import-closure checks live in ``tests/positions/test_boundaries.py``, which
covers all three Milestone 9 packages at once. What this file adds is the
*behavioural* half: run reconciliation against a broker that would happily have
taken an order, and prove it was never asked.

A broker that *could* have traded and was not asked is better evidence than one
that would have refused.
"""

from __future__ import annotations

import inspect

import pytest

from tests.positions.factories import (
    ACCOUNT,
    broker_execution,
    broker_order,
    execution_record,
    option_position,
)
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import ExecutionState, TradingMode
from trading_system.infrastructure.clock import FixedClock

pytestmark = pytest.mark.unit


@pytest.fixture
def writable_broker(clock: FixedClock) -> SimulatedBroker:
    """A broker that WOULD take an order. Nothing here ever asks it to."""
    return SimulatedBroker(
        SimulatedBrokerState(
            account_id=ACCOUNT,
            currency="EUR",
            positions=[option_position()],
            open_orders=[broker_order()],
            executions=[broker_execution()],
        ),
        clock=clock,
        trading_mode=TradingMode.PAPER,
        read_only=False,
    )


def test_a_full_run_against_a_writable_broker_submits_nothing(
    make_service, writable_broker
) -> None:
    service = make_service(broker=writable_broker)
    run = service.run()

    assert writable_broker.orders_submitted == 0
    assert writable_broker.book.orders == {}
    assert run.orders_submitted == 0
    assert run.result.orders_submitted == 0


def test_a_run_that_finds_everything_wrong_still_submits_nothing(
    make_service, writable_broker
) -> None:
    """Discrepancies are findings, not triggers."""
    service = make_service(broker=writable_broker)
    service.executions.seed(
        execution_record(state=ExecutionState.FAILED, filled_quantity=0).model_copy(
            update={"broker_order_id": "ord-1"}
        )
    )

    run = service.run()

    assert run.result.counts.critical >= 1
    assert writable_broker.orders_submitted == 0
    assert run.corrective_orders == 0


def test_a_run_never_cancels_an_orphan_order(make_service, writable_broker) -> None:
    service = make_service(broker=writable_broker)
    run = service.run()

    assert any(
        finding.finding_type.value == "ORPHAN_BROKER_ORDER" for finding in run.result.findings
    )
    # The order is still there, untouched: reconciliation reports it and stops.
    # Read off the simulator's own state rather than through the connection,
    # which the service has already closed — one short-lived connection per run.
    assert [order.broker_order_id for order in writable_broker.state.open_orders] == ["ord-1"]


def test_the_reconciliation_service_has_no_method_that_could_trade() -> None:
    from trading_system.reconciliation.service import ReconciliationService

    public = [name for name in dir(ReconciliationService) if not name.startswith("_")]
    for forbidden in ("submit", "place", "cancel", "close", "hedge", "adopt", "repair"):
        assert not any(forbidden in name for name in public), (
            f"ReconciliationService exposes a {forbidden}-like method: {public}"
        )


def test_the_engine_is_a_pure_function_of_its_arguments() -> None:
    """No broker, no repository, no clock in the constructor or the call."""
    from trading_system.reconciliation.engine import ReconciliationEngine

    constructor = set(inspect.signature(ReconciliationEngine.__init__).parameters)
    assert constructor == {"self", "config"}

    call = set(inspect.signature(ReconciliationEngine.reconcile).parameters)
    assert call == {"self", "inputs"}


def test_the_engine_never_writes_anything() -> None:
    source = inspect.getsource(
        __import__("trading_system.reconciliation.engine", fromlist=["engine"])
    )
    for forbidden in ("open(", ".save(", ".append_event(", "Path("):
        assert forbidden not in source


def test_reconciliation_only_ever_holds_a_read_only_broker(make_service) -> None:
    service = make_service()
    state = service.positions.read_broker_state()
    assert state.read_only is True


def test_the_run_reports_its_zero_counts_from_the_broker_not_from_a_constant(
    make_service, writable_broker
) -> None:
    """Evidence, not an assertion: the count comes off the broker itself."""
    service = make_service(broker=writable_broker)
    run = service.run()

    assert run.capture.state.orders_submitted == writable_broker.orders_submitted
    assert run.orders_submitted == writable_broker.orders_submitted


def test_a_reconciliation_never_edits_an_execution_into_agreement(
    make_service, writable_broker
) -> None:
    """A FAILED execution stays FAILED even when the broker has its order."""
    service = make_service(broker=writable_broker)
    record = execution_record(state=ExecutionState.FAILED, filled_quantity=0).model_copy(
        update={"broker_order_id": "ord-1"}
    )
    service.executions.seed(record)

    service.run()

    current = service.executions.current(record.execution_id)
    assert current is not None
    assert current.state is ExecutionState.FAILED


def test_a_reconciliation_never_edits_a_position_into_agreement(
    make_service, writable_broker
) -> None:
    service = make_service(broker=writable_broker)
    run = service.run()

    # The broker holds a position the internal ledger knows nothing about, and
    # the internal ledger still knows nothing about it afterwards.
    assert run.result.broker_position_count == 1
    assert run.result.expected_position_count == 0
    assert service.positions.expected().positions == ()


def test_a_dry_run_still_reads_the_broker_but_writes_nothing(make_service, writable_broker) -> None:
    service = make_service(broker=writable_broker)
    run = service.run(dry_run=True)

    assert run.result.broker_position_count == 1
    assert run.stored is False
    assert writable_broker.orders_submitted == 0
