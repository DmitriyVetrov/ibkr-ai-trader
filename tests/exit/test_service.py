"""The exit service: discovery, evaluation, and the refusal to trade.

The property this file exists to pin down is that **evaluation never trades**.
It is structural rather than a flag: ``evaluate`` and ``monitor`` construct no
broker, build no order, and reach Milestone 8 only when explicitly authorised.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.domain.enums import (
    ExitDecisionType,
    ExitReasonCode,
    ExitRunStatus,
    PositionLifecycleState,
    StructureStatus,
    TrailingStopState,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Discovery: positions come from Milestone 9, not from anywhere else
# ---------------------------------------------------------------------------
def test_an_open_position_is_discovered_from_confirmed_fills_and_broker_reality(
    open_long_call,
) -> None:
    service, execution = open_long_call

    positions = service.open_positions()

    assert len(positions) == 1
    position = positions[0]
    assert position.underlying == "NVDA"
    assert position.entry.execution_id == execution.execution_id
    assert position.expected_units == 2
    assert position.observed_units == 2
    assert position.structure.status is StructureStatus.COMPLETE


def test_an_execution_that_filled_nothing_establishes_no_position(
    build_exit_service, data_repo, stored_research
) -> None:
    """A submitted order is not a position; only a confirmed fill is."""
    from trading_system.domain.enums import ExecutionState

    factories.store_quotes(data_repo, [factories.option_quote()])
    execution = factories.entry_execution(
        state=ExecutionState.SUBMITTED,
        filled=0,
        average_fill_price=None,
        research_report_id=stored_research,
    )
    service = build_exit_service(executions=[execution], snapshot=factories.position_snapshot())

    assert service.open_positions() == []


def test_a_position_starts_its_life_open_and_unevaluated(
    open_long_call,
) -> None:
    service, _ = open_long_call

    position = service.open_positions()[0]

    assert position.lifecycle.state is PositionLifecycleState.OPEN
    assert position.lifecycle.evaluations == 0


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def test_a_healthy_position_waits_and_records_why(open_long_call) -> None:
    service, _ = open_long_call

    run = service.monitor()

    assert run.result.status is ExitRunStatus.SUCCESS
    assert run.result.counts.evaluated == 1
    assert run.result.counts.waiting == 1
    decision = run.result.decisions[0]
    assert decision.decision is ExitDecisionType.WAIT
    assert decision.exit_quote == Decimal("6.50")
    assert decision.entry_cost == Decimal("600.00")


def test_evaluation_submits_nothing(open_long_call) -> None:
    """Read off the run, which reads it off Milestone 8. Not asserted by hand."""
    service, _ = open_long_call

    run = service.monitor()

    assert run.orders_submitted == 0
    assert run.result.execution_authorized is False
    assert run.result.exit_execution_ids == []


def test_a_triggered_exit_is_recorded_and_still_submits_nothing(
    build_exit_service, data_repo, stored_research
) -> None:
    """An EXIT verdict from an unauthorised run is a *decision*, not an order."""
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("2.00"), ask=Decimal("2.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )

    run = service.monitor()

    assert run.result.counts.exiting == 1
    assert run.result.decisions[0].primary_reason is ExitReasonCode.MAX_LOSS_REACHED
    assert run.orders_submitted == 0
    assert all(outcome.submission is None for outcome in run.outcomes)


def test_the_lifecycle_advances_to_monitoring_on_a_wait(open_long_call) -> None:
    service, _ = open_long_call

    service.monitor()

    lifecycle = service.lifecycle(service.open_positions()[0].position_id)
    assert lifecycle is not None
    assert lifecycle.state is PositionLifecycleState.MONITORING
    assert lifecycle.evaluations == 1


def test_the_lifecycle_advances_to_exit_required_on_a_trigger(
    build_exit_service, data_repo, stored_research
) -> None:
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("2.00"), ask=Decimal("2.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    position_id = service.open_positions()[0].position_id

    service.monitor()

    lifecycle = service.lifecycle(position_id)
    assert lifecycle is not None
    assert lifecycle.state is PositionLifecycleState.EXIT_REQUIRED


def test_a_trailing_stop_moves_the_lifecycle_and_persists(
    build_exit_service, data_repo, exit_repo, stored_research
) -> None:
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("9.00"), ask=Decimal("9.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    position_id = service.open_positions()[0].position_id

    service.monitor()

    trailing = exit_repo.trailing(position_id)
    assert trailing is not None
    assert trailing.state is TrailingStopState.ARMED
    assert trailing.peak_quote == Decimal("9.00")
    lifecycle = service.lifecycle(position_id)
    assert lifecycle is not None
    assert lifecycle.state is PositionLifecycleState.TRAILING_ACTIVE


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_no_usable_snapshot_evaluates_nothing_and_says_so(
    build_exit_service, data_repo, stored_research
) -> None:
    """ "We could not look" is not "there is nothing there"."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(usable=False),
    )

    run = service.monitor()

    assert run.result.status is ExitRunStatus.BROKER_DATA_UNAVAILABLE
    assert run.result.decisions == []
    assert "NOT an empty account" in (run.result.status_detail or "")


def test_no_open_position_is_the_ordinary_answer(
    build_exit_service,
) -> None:
    service = build_exit_service(snapshot=factories.position_snapshot())

    run = service.monitor()

    assert run.result.status is ExitRunStatus.NO_POSITIONS
    assert run.orders_submitted == 0


def test_an_unpriced_position_blocks_rather_than_being_judged(
    build_exit_service, stored_research
) -> None:
    """No quote was collected at all: the structure is unpriced and blocks."""
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )

    run = service.monitor()

    assert run.result.status is ExitRunStatus.PARTIAL
    assert run.result.counts.blocked == 1
    assert run.result.decisions[0].primary_reason is ExitReasonCode.MARKET_DATA_UNAVAILABLE


def test_a_position_the_broker_no_longer_holds_becomes_closed(
    build_exit_service, data_repo, stored_research
) -> None:
    """Closure is broker reality, never an inference from a submitted order."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot([]),
    )
    position_id = service.open_positions()[0].position_id

    run = service.monitor()

    assert run.result.counts.closed == 1
    lifecycle = service.lifecycle(position_id)
    assert lifecycle is not None
    assert lifecycle.state is PositionLifecycleState.CLOSED
    assert lifecycle.open_quantity == 0
    assert lifecycle.terminal


def test_a_closed_position_never_reopens(build_exit_service, data_repo, stored_research) -> None:
    """Whatever the broker reports next, ``CLOSED`` is terminal.

    A later snapshot showing contracts under those ids is a new position or a
    reconciliation finding, and either is better than a record that silently
    reopened.
    """
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot([]),
    )
    position_id = service.open_positions()[0].position_id
    service.monitor()

    service.monitor()

    lifecycle = service.lifecycle(position_id)
    assert lifecycle is not None
    assert lifecycle.state is PositionLifecycleState.CLOSED


def test_a_dry_run_writes_nothing(open_long_call, exit_repo) -> None:
    service, _ = open_long_call
    position_id = service.open_positions()[0].position_id

    run = service.monitor(dry_run=True)

    assert run.result.counts.evaluated == 1
    assert run.stored is False
    assert exit_repo.history() == []
    assert exit_repo.lifecycle(position_id) is None
    assert exit_repo.trailing(position_id) is None
    assert run.orders_submitted == 0


def test_evaluation_switched_off_evaluates_nothing(
    build_exit_service, system_config, data_repo, stored_research
) -> None:
    factories.store_quotes(data_repo, [factories.option_quote()])
    disabled = system_config.model_copy(
        update={"exit": system_config.exit.model_copy(update={"enabled": False})}
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
        config=disabled,
    )

    run = service.monitor()

    assert run.result.status is ExitRunStatus.CONFIGURATION_ERROR
    assert "evaluation only" in (run.result.status_detail or "")


# ---------------------------------------------------------------------------
# The request handed to Milestone 8
# ---------------------------------------------------------------------------
def test_no_request_is_built_for_a_wait(open_long_call) -> None:
    service, _ = open_long_call
    run = service.monitor()

    assert service.build_request(run.outcomes[0], at=NOW) is None


def test_a_request_copies_the_quantity_the_broker_reports(
    build_exit_service, data_repo, stored_research
) -> None:
    """Nothing in this milestone computes a quantity."""
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("2.00"), ask=Decimal("2.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    run = service.monitor()

    request = service.build_request(run.outcomes[0], at=NOW)

    assert request is not None
    assert request.quantity == 2
    assert request.close_whole_strategy is True
    assert request.exit_authorized is True
    assert request.reference_quote == Decimal("2.00")
    assert request.exit_reason is ExitReasonCode.MAX_LOSS_REACHED


def test_a_request_carries_the_entry_provenance_by_id(
    build_exit_service, data_repo, stored_research
) -> None:
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("2.00"), ask=Decimal("2.20"))]
    )
    execution = factories.entry_execution(research_report_id=stored_research)
    service = build_exit_service(executions=[execution], snapshot=factories.position_snapshot())
    run = service.monitor()

    request = service.build_request(run.outcomes[0], at=NOW)

    assert request is not None
    assert request.entry_execution_id == execution.execution_id
    assert request.allocation_id == execution.allocation_id
    assert request.opportunity_id == execution.opportunity_id
    assert request.purchase_card_id == execution.purchase_card_id


# ---------------------------------------------------------------------------
# Telemetry seams (Milestone 11 will attach to these; none of them decides)
# ---------------------------------------------------------------------------
def test_the_named_operations_exist_as_service_methods() -> None:
    """Stable boundaries a tracer can wrap without changing trading logic."""
    from trading_system.exit.service import ExitService

    for operation in ("open_positions", "evaluate", "monitor", "build_request", "confirm"):
        assert callable(getattr(ExitService, operation))


def test_no_telemetry_vendor_is_imported(repo_root) -> None:
    """Milestone 11 is not implemented here."""
    package = repo_root / "src" / "trading_system" / "exit"
    for source in sorted(package.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for vendor in ("opentelemetry", "prometheus_client", "tempo", "loki"):
            assert f"import {vendor}" not in text
