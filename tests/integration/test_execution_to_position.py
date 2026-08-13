"""Execution to fill to position to reconciliation (brief section 82).

The loop Milestone 9 closes, over deterministic simulated data:

.. code-block:: text

    approved allocation -> execution -> broker accepts -> broker fills
                        -> position snapshot -> expected position
                        -> reconciliation

Four scenarios, exactly as the brief specifies them:

* the broker fills and holds the position  -> MATCH
* the broker does not fill                 -> no position
* the broker fills partly                  -> PARTIAL, and partial capital
* the broker holds something different     -> POSITION_MISMATCH

The Milestone 5-8 chain is reused rather than reimplemented, so this file tests
the *new* stage against the same artifacts everything else is tested against.
The broker is writable throughout: a broker that could have taken another order
and was never asked is better evidence than one that would have refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from tests.integration.test_research_to_allocation import (  # noqa: F401 - fixtures by name
    NOW,
    _run_everything,
    workflow,
)

from trading_system.broker.simulator.broker import SimulatedBroker, SimulatedBrokerState
from trading_system.broker.simulator.execution import fill_order
from trading_system.domain.enums import (
    BrokerReadStatus,
    ExecutionState,
    OrderSide,
    ReconciliationFindingType,
    ReconciliationRunStatus,
    ReservationState,
    SecurityType,
    StructureStatus,
    TradingMode,
)
from trading_system.domain.models import BrokerExecution, BrokerPosition
from trading_system.execution.models import ExecutionRecord
from trading_system.execution.service import ExecutionService
from trading_system.execution.store import FilesystemExecutionRepository
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings
from trading_system.positions.service import PositionService
from trading_system.reconciliation.service import ReconciliationService
from trading_system.reservations.service import ReservationService

pytestmark = pytest.mark.integration

ACCOUNT = "DU0000000"


@pytest.fixture
def broker() -> Iterator[SimulatedBroker]:
    """A writable broker holding nothing at all, so the test controls the account.

    Overrides the shared fixture deliberately: the default simulated portfolio
    is scenery, and a position test that inherited it would be asserting
    against someone else's holdings.
    """
    connection = SimulatedBroker(
        SimulatedBrokerState(account_id=ACCOUNT, currency="EUR"),
        trading_mode=TradingMode.PAPER,
        read_only=False,
        clock=FixedClock(NOW),
    )
    connection.connect()
    try:
        yield connection
    finally:
        connection.disconnect()


@pytest.fixture
def loop(tmp_path: Path, system_config, workflow, broker: SimulatedBroker):  # noqa: F811
    """The Milestone 5-8 chain, plus the Milestone 9 services over the same stores."""
    allocation_service = workflow[3]
    _run_everything(workflow)

    settings = Settings(_env_file=None, trading_mode="PAPER")
    clock = FixedClock(NOW)
    enabled = system_config.model_copy(
        update={"execution": system_config.execution.model_copy(update={"enabled": True})}
    )
    executions = FilesystemExecutionRepository(tmp_path / "data" / "execution")
    execution_service = ExecutionService(
        settings=settings,
        config=enabled,
        clock=clock,
        execution_repository=executions,
        allocation_repository=allocation_service.repository,
        research_repository=allocation_service._research_repository,
        strategy_repository=allocation_service._strategy_repository,
        broker_factory=lambda *a, **k: broker,
        root=tmp_path,
    )
    reconciliation = ReconciliationService(
        settings=settings,
        config=enabled,
        clock=clock,
        position_service=PositionService(
            settings=settings,
            config=enabled,
            clock=clock,
            execution_repository=executions,
            broker_factory=lambda *a, **k: broker,
            root=tmp_path,
        ),
        reservation_service=ReservationService(
            settings=settings,
            config=enabled,
            clock=clock,
            allocation_repository=allocation_service.repository,
            execution_repository=executions,
            root=tmp_path,
        ),
        execution_repository=executions,
        root=tmp_path,
    )
    [authorisation] = [
        allocation
        for allocation in (allocation_service.repository.latest().allocations)
        if allocation.approved
    ]
    return execution_service, reconciliation, broker, authorisation


def _submit(
    execution_service: ExecutionService, broker: SimulatedBroker, *, fill: int = 0
) -> ExecutionRecord:
    """Arrange how much the broker fills, then submit the authorisation."""
    broker.state.book.fill_on_submit = fill
    run = execution_service.run(authorized=True)
    [record] = run.result.executions
    return record


def _hold(broker: SimulatedBroker, record: ExecutionRecord, *, units: int) -> None:
    """Make the broker report the position those fills would have created."""
    broker.state.positions = [
        BrokerPosition(
            account_id=ACCOUNT,
            symbol=record.underlying,
            security_type=SecurityType.OPTION,
            as_of=NOW,
            source=broker.name,
            contract_id=leg.contract_id,
            currency=leg.currency,
            multiplier=leg.multiplier,
            quantity=Decimal(units * leg.ratio),
            average_cost=(record.average_fill_price or Decimal("1")) * Decimal(leg.multiplier),
            expiration=leg.expiration,
            strike=leg.strike,
            right=leg.right,
        )
        for leg in record.legs
    ]


def _report_fill(broker: SimulatedBroker, record: ExecutionRecord, *, units: int) -> None:
    """Make the broker report the execution that produced the position."""
    broker.state.executions = [
        BrokerExecution(
            execution_id=f"sim-exec-{leg.leg_index}",
            broker_order_id=record.broker_order_id,
            account_id=ACCOUNT,
            as_of=NOW,
            source=broker.name,
            contract_id=leg.contract_id,
            symbol=record.underlying,
            security_type=SecurityType.OPTION,
            side=OrderSide.BUY,
            quantity=Decimal(units * leg.ratio),
            price=record.average_fill_price or Decimal("1"),
            executed_at=NOW,
            commission=Decimal("1.30"),
            currency=leg.currency,
        )
        for leg in record.legs
    ]


# ---------------------------------------------------------------------------
# The broker fills, and holds what it filled
# ---------------------------------------------------------------------------
def test_a_filled_execution_becomes_a_position_the_broker_agrees_with(loop) -> None:
    execution_service, reconciliation, broker, authorisation = loop

    record = _submit(execution_service, broker, fill=authorisation.quantity)
    assert record.state is ExecutionState.FILLED
    _hold(broker, record, units=record.filled_quantity)
    _report_fill(broker, record, units=record.filled_quantity)

    run = reconciliation.run()

    assert run.result.status is ReconciliationRunStatus.MATCH
    assert run.result.by_type(ReconciliationFindingType.POSITION_MATCH)
    assert run.orders_submitted == 0


def test_the_expected_position_comes_from_the_fill_and_not_the_order(loop) -> None:
    execution_service, reconciliation, broker, authorisation = loop

    record = _submit(execution_service, broker, fill=authorisation.quantity)
    _hold(broker, record, units=record.filled_quantity)
    _report_fill(broker, record, units=record.filled_quantity)
    run = reconciliation.run()

    [expected] = [p for p in run.projection.positions if p.quantity != 0]
    assert expected.quantity == Decimal(record.filled_quantity)
    assert expected.execution_ids == [record.execution_id]


def test_a_filled_execution_consumes_its_reservation(loop) -> None:
    execution_service, reconciliation, broker, authorisation = loop

    record = _submit(execution_service, broker, fill=authorisation.quantity)
    _hold(broker, record, units=record.filled_quantity)
    _report_fill(broker, record, units=record.filled_quantity)
    reconciliation.run()

    [held] = reconciliation.reservations.all()
    assert held.state is ReservationState.CONSUMED
    assert held.consumed_amount > 0
    assert held.allocation_id == record.allocation_id


def test_the_whole_loop_submits_exactly_one_order(loop) -> None:
    """One authorisation, one order — and reconciliation adds none."""
    execution_service, reconciliation, broker, authorisation = loop

    record = _submit(execution_service, broker, fill=authorisation.quantity)
    _hold(broker, record, units=record.filled_quantity)
    reconciliation.run()
    reconciliation.run()

    assert broker.orders_submitted == 1


# ---------------------------------------------------------------------------
# The broker does not fill
# ---------------------------------------------------------------------------
def test_an_unfilled_order_establishes_no_position(loop) -> None:
    execution_service, reconciliation, broker, _ = loop

    record = _submit(execution_service, broker, fill=0)
    assert record.state is ExecutionState.SUBMITTED

    run = reconciliation.run()

    assert run.projection.positions == ()
    assert run.result.expected_position_count == 0


def test_an_unfilled_order_keeps_its_capital_committed(loop) -> None:
    execution_service, reconciliation, broker, _ = loop

    _submit(execution_service, broker, fill=0)
    reconciliation.run()

    [held] = reconciliation.reservations.all()
    assert held.state is ReservationState.RESERVED
    assert held.consumed_amount == Decimal("0")
    assert held.released_amount == Decimal("0")


def test_a_working_order_matches_the_brokers_open_order(loop) -> None:
    execution_service, reconciliation, broker, _ = loop

    _submit(execution_service, broker, fill=0)
    run = reconciliation.run()

    assert run.result.by_type(ReconciliationFindingType.ORDER_MATCH)


# ---------------------------------------------------------------------------
# The broker fills partly
# ---------------------------------------------------------------------------
def test_a_partial_fill_establishes_the_filled_quantity_only(loop) -> None:
    execution_service, reconciliation, broker, _ = loop

    record = _submit(execution_service, broker, fill=0)
    if record.quantity < 2:
        pytest.skip("the shipped campaign authorised a single unit; no partial fill exists")

    fill_order(broker.state.book, record.broker_order_id or "", units=1, at=NOW)
    resolved = execution_service.resolve(record.execution_id)
    assert resolved is not None
    assert resolved.state is ExecutionState.PARTIALLY_FILLED

    _hold(broker, resolved, units=1)
    run = reconciliation.run()

    [expected] = [p for p in run.projection.positions if p.quantity != 0]
    assert expected.quantity == Decimal("1")
    assert expected.quantity < Decimal(record.quantity)


def test_a_partially_filled_structure_is_reported_as_partial(loop) -> None:
    """A structure the broker holds only part of is neither complete nor absent."""
    execution_service, reconciliation, broker, authorisation = loop

    record = _submit(execution_service, broker, fill=authorisation.quantity)
    if len(record.legs) < 2:
        pytest.skip("the shipped campaign authorised a single-leg structure")

    _hold(broker, record, units=record.filled_quantity)
    broker.state.positions = broker.state.positions[:1]
    run = reconciliation.run()

    assert any(
        structure.status is StructureStatus.PARTIAL for structure in run.projection.strategies
    )
    assert run.result.by_type(ReconciliationFindingType.PARTIAL_STRUCTURE)


# ---------------------------------------------------------------------------
# The broker holds something else
# ---------------------------------------------------------------------------
def test_a_broker_position_that_differs_is_a_quantity_mismatch(loop) -> None:
    execution_service, reconciliation, broker, authorisation = loop

    record = _submit(execution_service, broker, fill=authorisation.quantity)
    _hold(broker, record, units=record.filled_quantity)
    _report_fill(broker, record, units=record.filled_quantity)
    # The account moved underneath us: the broker now holds one contract fewer.
    broker.state.positions = [
        position.model_copy(update={"quantity": position.quantity - Decimal("1")})
        for position in broker.state.positions
    ]

    run = reconciliation.run()

    assert run.result.status is ReconciliationRunStatus.MISMATCH
    findings = run.result.by_type(ReconciliationFindingType.POSITION_QUANTITY_MISMATCH)
    assert findings
    assert findings[0].delta == "-1"


def test_a_vanished_broker_position_is_reported_and_never_replaced(loop) -> None:
    execution_service, reconciliation, broker, authorisation = loop

    record = _submit(execution_service, broker, fill=authorisation.quantity)
    _report_fill(broker, record, units=record.filled_quantity)
    broker.state.positions = []

    run = reconciliation.run()

    assert run.result.by_type(ReconciliationFindingType.EXPECTED_POSITION_MISSING)
    assert run.orders_submitted == 0
    assert run.corrective_orders == 0
    assert broker.orders_submitted == 1  # the original submission, and nothing since


def test_an_unreadable_broker_is_never_reconciled_against(loop, monkeypatch) -> None:
    execution_service, reconciliation, broker, authorisation = loop
    record = _submit(execution_service, broker, fill=authorisation.quantity)
    _hold(broker, record, units=record.filled_quantity)

    from trading_system.broker.base import BrokerConnectionError

    def refuse():
        raise BrokerConnectionError("gateway down")

    monkeypatch.setattr(broker, "get_positions", refuse)
    run = reconciliation.run()

    assert run.result.status is ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE
    assert run.result.positions_read is BrokerReadStatus.UNAVAILABLE
    assert not run.result.by_type(ReconciliationFindingType.EXPECTED_POSITION_MISSING)
