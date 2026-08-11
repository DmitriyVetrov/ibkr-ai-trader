"""Reconciliation: the broker is authoritative, and discrepancies are reported.

The behaviour these tests pin down is as much about what reconciliation must
*not* do — resolve, correct, or trade — as about what it detects.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.broker.ibkr.reconciliation import Reconciler, position_key
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import (
    DiscrepancyType,
    OrderStatus,
    ReconciliationStatus,
)
from trading_system.infrastructure.clock import FixedClock

from .conftest import RecordingBroker


@pytest.fixture
def reconciler(broker_clock: FixedClock) -> Reconciler:
    return Reconciler(broker_clock)


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_identical_state_matches(reconciler: Reconciler, simulated_broker: SimulatedBroker) -> None:
    report = reconciler.reconcile(
        simulated_broker,
        internal_positions=simulated_broker.get_positions(),
        internal_orders=simulated_broker.get_open_orders(),
        internal_executions=simulated_broker.get_executions(),
    )

    assert report.status is ReconciliationStatus.MATCHED
    assert report.discrepancies == []
    assert report.blocks_new_executions is False


@pytest.mark.unit
def test_empty_on_both_sides_matches(reconciler: Reconciler, broker_clock: FixedClock) -> None:
    broker = SimulatedBroker(SimulatedBrokerState(), clock=broker_clock)
    broker.connect()
    report = reconciler.reconcile(broker)

    assert report.status is ReconciliationStatus.MATCHED
    assert report.blocks_new_executions is False


# ---------------------------------------------------------------------------
# Disagreement
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_broker_position_unknown_internally_is_flagged(
    reconciler: Reconciler, simulated_broker: SimulatedBroker
) -> None:
    """The broker's position is real whether or not we knew about it."""
    report = reconciler.reconcile(simulated_broker)

    position_issues = [
        d for d in report.discrepancies if d.discrepancy_type is DiscrepancyType.POSITION_MISMATCH
    ]
    assert len(position_issues) == 2
    assert all(d.internal_value is None for d in position_issues)
    assert all(d.broker_value is not None for d in position_issues)
    assert report.blocks_new_executions is True


@pytest.mark.unit
def test_internal_position_missing_from_broker_is_flagged(
    reconciler: Reconciler, broker_clock: FixedClock, simulated_broker: SimulatedBroker
) -> None:
    """ "The database says we own it" is not evidence that we own it."""
    internal = simulated_broker.get_positions()
    empty = SimulatedBroker(SimulatedBrokerState(), clock=broker_clock)
    empty.connect()

    report = reconciler.reconcile(empty, internal_positions=internal)

    assert report.status is ReconciliationStatus.MISMATCH
    assert len(report.discrepancies) == 2
    assert all(d.broker_value is None for d in report.discrepancies)
    assert "do not assume it exists" in report.discrepancies[0].description


@pytest.mark.unit
def test_quantity_disagreement_is_flagged(
    reconciler: Reconciler, simulated_broker: SimulatedBroker
) -> None:
    """Four contracts internally, three at the broker: the broker wins."""
    internal = list(simulated_broker.get_positions())
    internal[0] = internal[0].model_copy(update={"quantity": Decimal("4")})

    report = reconciler.reconcile(
        simulated_broker,
        internal_positions=internal,
        internal_orders=simulated_broker.get_open_orders(),
        internal_executions=simulated_broker.get_executions(),
    )

    issues = [d for d in report.discrepancies if "quantity disagrees" in d.description]
    assert len(issues) == 1
    assert issues[0].internal_value == "4"
    assert issues[0].broker_value == "2"
    assert "broker is authoritative" in issues[0].description


@pytest.mark.unit
def test_order_status_disagreement_is_flagged(
    reconciler: Reconciler, simulated_broker: SimulatedBroker
) -> None:
    """The database says filled, the broker says submitted."""
    internal = [
        order.model_copy(update={"status": OrderStatus.FILLED})
        for order in simulated_broker.get_open_orders()
    ]

    report = reconciler.reconcile(
        simulated_broker,
        internal_positions=simulated_broker.get_positions(),
        internal_orders=internal,
        internal_executions=simulated_broker.get_executions(),
    )

    issues = [
        d for d in report.discrepancies if d.discrepancy_type is DiscrepancyType.ORDER_MISMATCH
    ]
    assert issues
    assert issues[0].internal_value == "FILLED"
    assert issues[0].broker_value == "SUBMITTED"


@pytest.mark.unit
def test_filled_quantity_disagreement_is_flagged(
    reconciler: Reconciler, simulated_broker: SimulatedBroker
) -> None:
    internal = [
        order.model_copy(update={"filled_quantity": Decimal("1")})
        for order in simulated_broker.get_open_orders()
    ]

    report = reconciler.reconcile(
        simulated_broker,
        internal_positions=simulated_broker.get_positions(),
        internal_orders=internal,
        internal_executions=simulated_broker.get_executions(),
    )

    assert any("filled quantity disagrees" in d.description for d in report.discrepancies)


@pytest.mark.unit
def test_internal_fill_unknown_to_the_broker_is_flagged(
    reconciler: Reconciler, simulated_broker: SimulatedBroker, broker_clock: FixedClock
) -> None:
    """A recorded fill the broker never saw is the most dangerous case."""
    phantom = simulated_broker.get_executions()[0].model_copy(update={"execution_id": "phantom-1"})

    report = reconciler.reconcile(
        simulated_broker,
        internal_positions=simulated_broker.get_positions(),
        internal_orders=simulated_broker.get_open_orders(),
        internal_executions=[*simulated_broker.get_executions(), phantom],
    )

    issues = [d for d in report.discrepancies if d.identifier == "phantom-1"]
    assert len(issues) == 1
    assert "must not be treated as real" in issues[0].description


@pytest.mark.unit
def test_execution_price_disagreement_is_flagged(
    reconciler: Reconciler, simulated_broker: SimulatedBroker
) -> None:
    internal = [
        execution.model_copy(update={"price": Decimal("6.50")})
        for execution in simulated_broker.get_executions()
    ]

    report = reconciler.reconcile(
        simulated_broker,
        internal_positions=simulated_broker.get_positions(),
        internal_orders=simulated_broker.get_open_orders(),
        internal_executions=internal,
    )

    assert any(
        d.discrepancy_type is DiscrepancyType.EXECUTION_MISMATCH for d in report.discrepancies
    )


# ---------------------------------------------------------------------------
# Fail safe
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_unreachable_broker_yields_broker_unavailable(
    reconciler: Reconciler, broker_clock: FixedClock
) -> None:
    """Not knowing is a result, not an exception — and it still blocks."""
    broker = SimulatedBroker(clock=broker_clock)  # never connected

    report = reconciler.reconcile(broker)

    assert report.status is ReconciliationStatus.BROKER_UNAVAILABLE
    assert report.blocks_new_executions is True
    assert report.discrepancies[0].discrepancy_type is DiscrepancyType.UNKNOWN


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    [ReconciliationStatus.MISMATCH, ReconciliationStatus.BROKER_UNAVAILABLE],
)
def test_anything_other_than_matched_blocks_execution(
    status: ReconciliationStatus, broker_clock: FixedClock
) -> None:
    from trading_system.domain.models import (
        ReconciliationDiscrepancy,
        ReconciliationReport,
    )

    report = ReconciliationReport(
        as_of=broker_clock.now(),
        broker="SIMULATOR",
        status=status,
        discrepancies=[
            ReconciliationDiscrepancy(
                discrepancy_type=DiscrepancyType.UNKNOWN,
                identifier="x",
                description="y",
            )
        ],
    )
    assert report.blocks_new_executions is True


@pytest.mark.unit
def test_reconciliation_attempts_no_correction(
    reconciler: Reconciler, recording_broker: RecordingBroker
) -> None:
    """It reports; it never trades to fix what it found."""
    before = recording_broker.get_positions()
    report = reconciler.reconcile(recording_broker)

    assert report.discrepancies
    assert recording_broker.mutation_attempts == []
    assert recording_broker.orders_submitted == 0
    assert recording_broker.get_positions() == before


@pytest.mark.unit
def test_report_counts_what_was_compared(
    reconciler: Reconciler, simulated_broker: SimulatedBroker
) -> None:
    report = reconciler.reconcile(simulated_broker)

    assert report.positions_compared == 2
    assert report.orders_compared == 1
    assert report.executions_compared == 1


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_position_key_distinguishes_option_terms(
    simulated_broker: SimulatedBroker,
) -> None:
    """Same underlying, different strike, is a different position."""
    option = next(p for p in simulated_broker.get_positions() if p.is_option)
    other_strike = option.model_copy(update={"strike": Decimal("190.00")})

    assert position_key(option) != position_key(other_strike)


@pytest.mark.unit
def test_position_key_ignores_contract_id(simulated_broker: SimulatedBroker) -> None:
    """Internal state may predate knowing the broker's contract id."""
    option = next(p for p in simulated_broker.get_positions() if p.is_option)
    without_id = option.model_copy(update={"contract_id": None})

    assert position_key(option) == position_key(without_id)


@pytest.mark.unit
def test_matched_report_cannot_carry_discrepancies(broker_clock: FixedClock) -> None:
    from pydantic import ValidationError

    from trading_system.domain.models import (
        ReconciliationDiscrepancy,
        ReconciliationReport,
    )

    with pytest.raises(ValidationError, match="MATCHED cannot carry"):
        ReconciliationReport(
            as_of=broker_clock.now(),
            broker="SIMULATOR",
            status=ReconciliationStatus.MATCHED,
            discrepancies=[
                ReconciliationDiscrepancy(
                    discrepancy_type=DiscrepancyType.UNKNOWN,
                    identifier="x",
                    description="y",
                )
            ],
        )


@pytest.mark.unit
def test_mismatch_must_name_a_discrepancy(broker_clock: FixedClock) -> None:
    from pydantic import ValidationError

    from trading_system.domain.models import ReconciliationReport

    with pytest.raises(ValidationError, match="must name at least one"):
        ReconciliationReport(
            as_of=broker_clock.now(),
            broker="SIMULATOR",
            status=ReconciliationStatus.MISMATCH,
        )
