"""The whole loop, and the lineage that survives it (brief section 83).

.. code-block:: text

    universe -> research -> strategy -> contract -> risk -> allocation
             -> execution -> fill -> position -> reconciliation

One claim, checked link by link: **every artifact id remains traceable.** After
a reconciliation, "why does this account hold this contract" is answerable by
following identifiers from the broker position back to the universe run that
first proposed looking at the underlying — without inference, and without
consulting a model's memory.

The second claim is the one that makes the first worth having: the whole
workflow submits exactly one order — the execution — and reconciliation adds
none, against a broker that would have recorded any of them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.integration.test_execution_to_position import (  # noqa: F401 - fixtures by name
    ACCOUNT,
    _hold,
    _report_fill,
    _submit,
    broker,
    loop,
)
from tests.integration.test_research_to_allocation import (  # noqa: F401 - fixtures by name
    NOW,
    _run_everything,
    workflow,
)

from trading_system.domain.enums import (
    ExecutionState,
    ReconciliationRunStatus,
    ReservationState,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def reconciled(loop):  # noqa: F811
    """Run the loop to completion: authorised, executed, filled, reconciled."""
    execution_service, reconciliation, connection, authorisation = loop
    record = _submit(execution_service, connection, fill=authorisation.quantity)
    _hold(connection, record, units=record.filled_quantity)
    _report_fill(connection, record, units=record.filled_quantity)
    run = reconciliation.run()
    return authorisation, record, run, reconciliation, connection


# ---------------------------------------------------------------------------
# 83. Lineage
# ---------------------------------------------------------------------------
def test_the_execution_traces_back_to_the_universe_run(reconciled, workflow) -> None:  # noqa: F811
    """Nine links, each one an id on the artifact that descends from it."""
    authorisation, record, _, _, _ = reconciled
    research, *_ = workflow

    assert research.universe_run_id == "universe-run-001"
    assert authorisation.research_run_id == research.run_id
    assert authorisation.research_report_id == research.reports[0].report_id
    assert authorisation.strategy_decision_id is not None
    assert authorisation.contract_selection_id is not None
    assert authorisation.risk_evaluation.evaluation_id is not None
    assert record.allocation_id == authorisation.allocation_id
    assert record.purchase_card_id.startswith("card-")
    assert record.execution_id.startswith("execution-")


def test_every_artifact_in_the_chain_names_the_one_before_it(reconciled) -> None:
    authorisation, record, run, _, _ = reconciled

    assert record.opportunity_id == authorisation.opportunity_id
    assert record.contract_selection_id == authorisation.contract_selection_id
    assert record.strategy_decision_id == authorisation.strategy_decision_id
    assert record.research_report_id == authorisation.research_report_id
    assert record.account_snapshot_id == authorisation.account_snapshot_id
    assert record.execution_id in run.result.execution_ids


def test_the_position_traces_back_to_the_execution_that_created_it(reconciled) -> None:
    _, record, run, _, _ = reconciled

    [position] = [p for p in run.projection.positions if p.quantity != 0]
    assert position.execution_ids == [record.execution_id]
    assert position.allocation_ids == [record.allocation_id]
    assert position.opportunity_ids == [record.opportunity_id]
    assert position.fill_ids


def test_the_reconciliation_names_the_snapshot_and_the_ledgers_it_compared(
    reconciled,
) -> None:
    _, record, run, reconciliation, _ = reconciled

    assert run.result.position_snapshot_id == run.capture.snapshot.snapshot_id
    assert run.result.account_snapshot_id is not None
    assert record.execution_id in run.result.execution_ids
    assert run.result.reservation_ids == [reconciliation.reservations.all()[0].reservation_id]


def test_the_reservation_traces_back_to_the_authorisation_and_the_execution(
    reconciled,
) -> None:
    authorisation, record, _, reconciliation, _ = reconciled

    [held] = reconciliation.reservations.all()
    assert held.allocation_id == authorisation.allocation_id
    assert held.opportunity_id == authorisation.opportunity_id
    assert held.execution_id == record.execution_id
    assert held.contract_selection_id == authorisation.contract_selection_id
    assert held.research_report_id == authorisation.research_report_id


def test_the_stored_reconciliation_can_be_read_back_by_id(reconciled) -> None:
    _, _, run, reconciliation, _ = reconciled

    stored = reconciliation.get(run.result.reconciliation_id)
    assert stored is not None
    assert stored.content_hash == run.result.content_hash
    assert stored.status is run.result.status


# ---------------------------------------------------------------------------
# The loop actually closes
# ---------------------------------------------------------------------------
def test_the_loop_ends_in_agreement(reconciled) -> None:
    _, _, run, _, _ = reconciled
    assert run.result.status is ReconciliationRunStatus.MATCH
    assert run.result.blocks_new_executions is False


def test_the_capital_moved_from_reserved_to_consumed(reconciled) -> None:
    """The Milestone 7 limitation, finally closed by the milestone that can tell."""
    authorisation, _, _, reconciliation, _ = reconciled

    [held] = reconciliation.reservations.all()
    assert held.state is ReservationState.CONSUMED
    assert held.authorized_amount == authorisation.capital_committed
    assert held.consumed_amount > 0
    assert held.consumed_from_actual_fills is True


def test_the_campaign_capital_reflects_what_was_actually_spent(reconciled) -> None:
    _, _, _, reconciliation, _ = reconciled

    capital = reconciliation.reservations.capital()
    assert capital.consumed_total > 0
    assert capital.locked_by_unknown == Decimal("0")
    assert capital.available == capital.allocatable - capital.committed_total


def test_the_execution_is_filled_and_the_broker_agrees(reconciled) -> None:
    _, record, run, _, _ = reconciled

    assert record.state is ExecutionState.FILLED
    assert run.result.broker_position_count == 1
    assert run.result.expected_position_count == 1


# ---------------------------------------------------------------------------
# One order, and only one
# ---------------------------------------------------------------------------
def test_the_whole_workflow_submits_exactly_one_order(reconciled) -> None:
    _, _, run, reconciliation, connection = reconciled

    reconciliation.run()
    reconciliation.run()

    assert connection.orders_submitted == 1
    assert run.orders_submitted == 0
    assert run.corrective_orders == 0


def test_reconciliation_places_no_corrective_order_however_wrong_things_are(
    reconciled,
) -> None:
    _, _, _, reconciliation, connection = reconciled
    connection.state.positions = []

    run = reconciliation.run()

    assert run.result.status is ReconciliationRunStatus.MISMATCH
    assert run.result.orders_submitted == 0
    assert run.result.corrective_orders == 0
    assert connection.orders_submitted == 1


def test_the_milestone_1_report_still_speaks_for_the_whole_chain(reconciled) -> None:
    """The narrow boundary the rest of the system was built against."""
    _, _, run, _, _ = reconciled

    report = run.result.to_reconciliation_report()
    assert report.status.value == "MATCHED"
    assert report.discrepancies == []
    assert report.blocks_new_executions is False
