"""Dry runs (brief section 33).

A dry run must do everything except send: load the authorisation, validate it,
build the purchase card, build the broker order, and show exactly what *would*
be submitted. It must not call the broker, create an order, or change broker
state.

The promise here is structural rather than a flag that has to be checked
correctly: a dry run never constructs a broker at all. There is no object it
could send through.
"""

from __future__ import annotations

import pytest

from trading_system.domain.enums import ExecutionRunStatus, ExecutionState
from trading_system.execution.service import ExecutionService

pytestmark = pytest.mark.unit


@pytest.fixture
def service(settings_paper, execution_disabled_config, clock, tmp_path, stub_repositories):
    """Execution switched off, so a dry run is exercised against the switch."""
    return ExecutionService(
        settings=settings_paper,
        config=execution_disabled_config,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )


def test_a_dry_run_submits_nothing(service, approved_allocation) -> None:
    run = service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    assert run.result.orders_submitted == 0
    assert run.result.status is ExecutionRunStatus.DRY_RUN


def test_a_dry_run_constructs_no_broker(service, approved_allocation, monkeypatch) -> None:
    """The strongest form of the promise: there is nothing to submit through."""
    from trading_system.broker import factory

    def explode(*args, **kwargs):
        raise AssertionError("a dry run must never build a broker")

    monkeypatch.setattr(factory, "build_execution_broker", explode)
    monkeypatch.setattr(factory, "build_broker", explode)

    run = service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    assert run.result.status is ExecutionRunStatus.DRY_RUN


def test_a_dry_run_persists_nothing(service, approved_allocation) -> None:
    service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    assert service.repository.history() == []
    assert service.repository.run_history() == []


def test_a_dry_run_still_builds_the_whole_order(service, approved_allocation) -> None:
    """It shows what would be sent, not merely that it looks fine."""
    run = service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    [plan] = run.plans
    assert plan.card is not None
    assert plan.risk_decision is not None
    assert plan.intent is not None
    assert plan.intent.limit_price is not None
    assert plan.submittable


def test_a_dry_run_works_while_execution_is_disabled(
    service, approved_allocation, execution_disabled_config
) -> None:
    """Which is what makes the master switch reviewable rather than opaque."""
    assert execution_disabled_config.execution.enabled is False

    run = service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    assert run.plans and run.plans[0].submittable


def test_a_dry_run_record_is_marked_and_cannot_be_live(service, approved_allocation) -> None:
    run = service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    [record] = run.result.executions
    assert record.dry_run is True
    assert record.state is ExecutionState.VALIDATED
    assert not record.submitted
    assert record.broker_order_id is None
    assert record.orders_submitted == 0


def test_a_dry_run_names_no_broker(service, approved_allocation) -> None:
    run = service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    assert run.result.broker == "NONE"


def test_the_run_record_refuses_to_describe_a_dry_run_that_submitted(versions) -> None:
    """A dry run that reached a broker is a bug, not a diagnostic."""
    from pydantic import ValidationError

    from trading_system.domain.enums import TradingMode
    from trading_system.execution.models import ExecutionRunResult

    from .conftest import NOW

    with pytest.raises(ValidationError, match="dry run"):
        ExecutionRunResult(
            run_id="execrun-1",
            campaign_id="campaign-001",
            as_of=NOW,
            generated_at=NOW,
            status=ExecutionRunStatus.DRY_RUN,
            trading_mode=TradingMode.PAPER,
            dry_run=True,
            broker="SIMULATOR",
            policy_version="2026.08.10-1",
            orders_submitted=1,
            versions=versions,
        )


def test_the_dry_run_report_says_so_plainly(service, approved_allocation) -> None:
    from trading_system.execution.report import render_execution_run, render_plan

    run = service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    summary = render_execution_run(run.result)
    assert "EXECUTION DRY RUN" in summary
    assert "NOT PERFORMED" in summary

    plan_text = render_plan(run.plans[0])
    assert "Broker submission: NOT PERFORMED" in plan_text
    assert "Would submit" in plan_text


def test_the_dry_run_shows_the_derived_limit_price(service, approved_allocation) -> None:
    """An operator reviewing a run needs the actual number that would be sent."""
    from trading_system.execution.report import render_plan

    run = service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)
    text = render_plan(run.plans[0])

    assert str(run.plans[0].intent.limit_price) in text
