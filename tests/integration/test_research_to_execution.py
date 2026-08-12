"""Research to strategy to contract to risk to allocation to execution.

Brief section 42.13. The whole workflow, over deterministic simulated data,
extended one stage past
:mod:`tests.integration.test_research_to_allocation` — which is deliberately
reused rather than reimplemented, so this file tests the *new* stage against
the same chain everything else is tested against.

Three claims:

* the chain still connects — an execution names the authorisation, the card,
  the contract selection, the strategy decision and the research report, so
  "why was this order sent" is answerable by following ids;
* **a dry run produces no orders**, proven against a writable broker that would
  have recorded one;
* the submission path works, and produces exactly one order per authorisation
  however many times it is run.

The broker is writable throughout. Nothing here depends on a read-only guard:
a broker that *could* have taken an order and was never asked is better
evidence than one that refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.test_research_to_allocation import (  # noqa: F401 - fixtures by name
    NOW,
    _run_everything,
    broker,
    workflow,
)

from trading_system.broker.simulator.broker import SimulatedBroker
from trading_system.domain.enums import (
    AllocationOutcome,
    ExecutionReasonCode,
    ExecutionRunStatus,
    ExecutionState,
    OrderStatus,
    OrderType,
    TradingMode,
)
from trading_system.execution.service import ExecutionService
from trading_system.execution.store import FilesystemExecutionRepository
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def executed(tmp_path: Path, system_config, workflow, broker: SimulatedBroker):  # noqa: F811
    """Run the Milestone 5-7 chain, then wire an execution service to its output.

    Execution is switched *on* here. The shipped configuration ships it off,
    which is tested in ``tests/execution``; this test is about what happens
    when it is deliberately enabled.
    """
    allocation_service = workflow[3]
    research, strategy_run, contract_run, allocation_run = _run_everything(workflow)

    enabled = system_config.model_copy(
        update={"execution": system_config.execution.model_copy(update={"enabled": True})}
    )
    service = ExecutionService(
        settings=Settings(_env_file=None, trading_mode="PAPER"),
        config=enabled,
        clock=FixedClock(NOW),
        execution_repository=FilesystemExecutionRepository(tmp_path / "data" / "execution"),
        allocation_repository=allocation_service.repository,
        research_repository=allocation_service._research_repository,
        strategy_repository=allocation_service._strategy_repository,
        broker_factory=lambda *args, **kwargs: broker,
        root=tmp_path,
    )
    return research, strategy_run, contract_run, allocation_run, service


# ---------------------------------------------------------------------------
# The chain still connects
# ---------------------------------------------------------------------------
def test_the_whole_chain_connects_through_to_execution(executed) -> None:
    *_, allocation_run, service = executed
    [authorisation] = [a for a in allocation_run.result.allocations if a.approved]

    run = service.run(authorized=True)

    [record] = run.result.executions
    assert record.allocation_id == authorisation.allocation_id
    assert record.contract_selection_id == authorisation.contract_selection_id
    assert record.strategy_decision_id == authorisation.strategy_decision_id
    assert record.research_report_id == authorisation.research_report_id
    assert record.opportunity_id == authorisation.opportunity_id


def test_the_execution_names_the_purchase_card_and_risk_decision(executed) -> None:
    """The Milestone 1 artifacts the specification requires before execution."""
    *_, service = executed

    run = service.run(authorized=True)

    [record] = run.result.executions
    assert record.purchase_card_id.startswith("card-")
    assert record.risk_decision_id.startswith("risk-")
    assert record.order_intent_id.startswith("intent-")


def test_the_execution_carries_the_authorised_figures_unchanged(executed) -> None:
    """Brief section 6: execution changes nothing Milestone 7 decided."""
    _, _, _, allocation_run, service = executed
    [authorisation] = [a for a in allocation_run.result.allocations if a.approved]

    run = service.run(authorized=True)

    [record] = run.result.executions
    assert record.quantity == authorisation.quantity
    assert record.capital_commitment == authorisation.capital_committed
    assert record.maximum_loss == authorisation.total_max_loss
    assert record.reference_price == authorisation.unit_cost
    assert [leg.contract_id for leg in record.legs] == [
        leg.contract_id for leg in authorisation.legs
    ]


# ---------------------------------------------------------------------------
# A dry run produces no orders
# ---------------------------------------------------------------------------
def test_a_dry_run_of_the_whole_chain_submits_nothing(
    executed,
    broker: SimulatedBroker,  # noqa: F811
) -> None:
    *_, service = executed

    run = service.run(dry_run=True)

    assert run.result.status is ExecutionRunStatus.DRY_RUN
    assert broker.orders_submitted == 0
    assert broker.book.orders == {}


def test_a_dry_run_leaves_the_broker_book_untouched(
    executed,
    broker: SimulatedBroker,  # noqa: F811
) -> None:
    """Brief section 33: open orders unchanged."""
    *_, service = executed
    before = dict(broker.book.orders)

    service.run(dry_run=True)

    assert broker.book.orders == before


def test_an_unauthorised_run_submits_nothing(
    executed,
    broker: SimulatedBroker,  # noqa: F811
) -> None:
    *_, service = executed

    run = service.run()

    assert run.result.status is ExecutionRunStatus.NOT_AUTHORIZED
    assert broker.orders_submitted == 0


# ---------------------------------------------------------------------------
# The submission path
# ---------------------------------------------------------------------------
def test_an_authorised_run_submits_exactly_one_order(
    executed,
    broker: SimulatedBroker,  # noqa: F811
) -> None:
    *_, service = executed

    run = service.run(authorized=True)

    assert broker.orders_submitted == 1
    assert run.result.orders_submitted == 1
    assert len(broker.book.orders) == 1


def test_the_order_reaching_the_broker_is_the_authorised_contract(
    executed,
    broker: SimulatedBroker,  # noqa: F811
) -> None:
    _, _, _, allocation_run, service = executed
    [authorisation] = [a for a in allocation_run.result.allocations if a.approved]

    service.run(authorized=True)

    [order] = list(broker.book.orders.values())
    assert order.intent.quantity == authorisation.quantity
    assert [leg.broker_contract_id for leg in order.intent.legs] == [
        leg.contract_id for leg in authorisation.legs
    ]
    assert order.intent.order_type is OrderType.LIMIT


def test_the_acknowledged_order_is_not_reported_as_filled(
    executed,
    broker: SimulatedBroker,  # noqa: F811
) -> None:
    """The simulator acknowledges and does not fill, exactly as IBKR would."""
    *_, service = executed

    run = service.run(authorized=True)

    [record] = run.result.executions
    assert record.state is ExecutionState.SUBMITTED
    assert record.broker_status is OrderStatus.SUBMITTED
    assert record.filled_quantity == 0
    assert record.average_fill_price is None


def test_running_twice_never_submits_twice(
    executed,
    broker: SimulatedBroker,  # noqa: F811
) -> None:
    """One authorisation, one order, however many times the command is run."""
    *_, service = executed

    service.run(authorized=True)
    second = service.run(authorized=True)

    assert broker.orders_submitted == 1
    assert len(broker.book.orders) == 1
    [record] = second.result.executions
    assert ExecutionReasonCode.ALREADY_SUBMITTED in record.reason_codes


def test_a_dry_run_after_a_submission_still_submits_nothing(
    executed,
    broker: SimulatedBroker,  # noqa: F811
) -> None:
    *_, service = executed
    service.run(authorized=True)

    service.run(dry_run=True)

    assert broker.orders_submitted == 1


# ---------------------------------------------------------------------------
# What is persisted
# ---------------------------------------------------------------------------
def test_the_execution_is_recorded_immutably_with_its_history(executed) -> None:
    *_, service = executed

    run = service.run(authorized=True)
    [record] = run.result.executions

    stored = service.repository.base(record.execution_id)
    assert stored is not None
    assert stored.state is ExecutionState.SUBMISSION_PENDING, (
        "the base record is written before the send and never rewritten"
    )
    current = service.repository.current(record.execution_id)
    assert current is not None and current.state is ExecutionState.SUBMITTED

    events = service.repository.events(record.execution_id)
    assert [event.state for event in events] == [
        ExecutionState.SUBMISSION_PENDING,
        ExecutionState.SUBMITTED,
    ]


def test_the_run_record_validates_against_its_schema(executed, load_schema) -> None:
    from jsonschema import Draft202012Validator

    *_, service = executed
    run = service.run(authorized=True)

    Draft202012Validator(load_schema("execution_run")).validate(run.result.model_dump(mode="json"))


def test_the_execution_projects_onto_the_milestone_one_boundary(executed, load_schema) -> None:
    """``execution_result.json`` is the Milestone 1 contract the rest was built against."""
    from jsonschema import Draft202012Validator

    *_, service = executed
    run = service.run(authorized=True)
    [record] = run.result.executions

    projection = record.to_execution_result()
    Draft202012Validator(load_schema("execution_result")).validate(
        projection.model_dump(mode="json")
    )
    assert projection.intent_id == record.order_intent_id
    assert projection.orders_submitted == 1


def test_the_purchase_card_validates_against_its_schema(executed, load_schema) -> None:
    from jsonschema import Draft202012Validator

    *_, service = executed
    _, _, _, allocation_run, _ = executed
    [authorisation] = [a for a in allocation_run.result.allocations if a.approved]

    plan = service.plan(authorisation, dry_run=True)

    assert plan.card is not None
    Draft202012Validator(load_schema("purchase_card")).validate(plan.card.model_dump(mode="json"))


def test_the_order_intent_validates_against_its_schema(executed, load_schema) -> None:
    from jsonschema import Draft202012Validator

    _, _, _, allocation_run, service = executed
    [authorisation] = [a for a in allocation_run.result.allocations if a.approved]

    plan = service.plan(authorisation, dry_run=True)

    assert plan.intent is not None
    Draft202012Validator(load_schema("order_intent")).validate(plan.intent.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# What execution did not do
# ---------------------------------------------------------------------------
def test_execution_did_not_alter_the_allocation_ledger(executed) -> None:
    _, _, _, allocation_run, service = executed
    before = [a.model_dump() for a in allocation_run.result.allocations]

    service.run(authorized=True)

    after = service._allocation_repository.latest()
    assert after is not None
    assert [a.model_dump() for a in after.allocations] == before


def test_execution_authorised_no_new_capital(executed) -> None:
    """Milestone 8 spends what Milestone 7 authorised and authorises nothing."""
    _, _, _, allocation_run, service = executed
    committed = allocation_run.result.allocated_this_run

    service.run(authorized=True)

    after = service._allocation_repository.latest()
    assert after is not None
    assert after.allocated_this_run == committed


def test_only_approved_authorisations_were_executed(executed) -> None:
    _, _, _, allocation_run, service = executed

    run = service.run(authorized=True)

    executed_ids = {record.allocation_id for record in run.result.executions}
    refused = {
        a.allocation_id
        for a in allocation_run.result.allocations
        if a.outcome is not AllocationOutcome.APPROVED
    }
    assert not (executed_ids & refused)


def test_the_execution_service_takes_no_model_client(executed) -> None:
    """Brief section 65. An execution engine needs no LLM.

    The transitive import closure is asserted in
    ``tests/execution/test_boundaries.py``; this is the constructor-level check
    on the service that actually ran the chain above.
    """
    import inspect

    *_, service = executed
    parameters = set(inspect.signature(type(service).__init__).parameters)

    assert "llm_client" not in parameters
    assert "agent" not in parameters
    assert "prompt" not in parameters


def test_the_trading_mode_is_paper_throughout(executed) -> None:
    *_, service = executed

    run = service.run(authorized=True)

    assert run.result.trading_mode is TradingMode.PAPER
    assert all(record.trading_mode is TradingMode.PAPER for record in run.result.executions)
