"""The execution service: the composition root (brief sections 4, 30, 62).

Two switches must both be on before an order is sent, and neither implies the
other:

* ``execution.enabled`` in configuration — the system-level permission;
* an explicit authorisation on the run — the decision to act today.

Everything else here is about the service consuming Milestone 7 rather than
re-deciding it: it selects among existing authorisations, it copies their
figures, and it refuses a symbol no allocation covered rather than allocating
one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    ExecutionReasonCode,
    ExecutionRunStatus,
    ExecutionState,
    OrderStatus,
)
from trading_system.execution.service import ExecutionService

pytestmark = pytest.mark.unit


@pytest.fixture
def service(settings_paper, system_config, clock, tmp_path, stub_repositories):
    """A service over the shipped configuration, which ships execution OFF."""
    return ExecutionService(
        settings=settings_paper,
        config=system_config,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )


@pytest.fixture
def enabled_service(settings_paper, system_config, clock, tmp_path, stub_repositories):
    """The same service with submission switched on, for the paths that send."""
    config = system_config.model_copy(
        update={"execution": system_config.execution.model_copy(update={"enabled": True})}
    )
    return ExecutionService(
        settings=settings_paper,
        config=config,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )


# ---------------------------------------------------------------------------
# The two switches
# ---------------------------------------------------------------------------
def test_without_authorisation_nothing_is_built_or_sent(service, approved_allocation) -> None:
    """Brief section 4: an allocation id alone never means "send it"."""
    run = service.run(allocation_ids=[approved_allocation.allocation_id])

    assert run.result.status is ExecutionRunStatus.NOT_AUTHORIZED
    assert run.result.orders_submitted == 0
    assert run.result.executions == []


def test_authorisation_alone_is_not_enough_while_execution_is_disabled(
    service, approved_allocation
) -> None:
    run = service.run(allocation_ids=[approved_allocation.allocation_id], authorized=True)

    assert run.result.status is ExecutionRunStatus.EXECUTION_DISABLED
    assert run.result.orders_submitted == 0


def test_the_request_model_cannot_express_an_unauthorised_request(make_request) -> None:
    """There is no shape in which "load this" and "send this" are the same call."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="execution_authorized=True"):
        make_request(execution_authorized=False)


def test_authorisation_cannot_be_switched_off_in_configuration(system_config) -> None:
    from pydantic import ValidationError

    from trading_system.infrastructure.settings import ExecutionConfig

    payload = system_config.execution.model_dump() | {"require_explicit_authorization": False}
    with pytest.raises(ValidationError, match="boundary this milestone exists to draw"):
        ExecutionConfig.model_validate(payload)


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------
def test_an_authorised_enabled_run_submits_exactly_one_order(
    enabled_service, approved_allocation, fake_broker
) -> None:
    broker = fake_broker()

    run = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id], authorized=True, broker=broker
    )

    assert broker.orders_submitted == 1
    assert run.result.orders_submitted == 1
    assert run.result.status is ExecutionRunStatus.SUCCESS
    [record] = run.result.executions
    assert record.state is ExecutionState.SUBMITTED


def test_the_submitted_order_carries_the_authorised_figures(
    enabled_service, approved_allocation, fake_broker
) -> None:
    """Nothing is recomputed: quantity, capital and maximum loss are copied."""
    broker = fake_broker()

    run = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id], authorized=True, broker=broker
    )

    [record] = run.result.executions
    assert record.quantity == approved_allocation.quantity
    assert record.capital_commitment == approved_allocation.capital_committed
    assert record.maximum_loss == approved_allocation.total_max_loss
    assert record.reference_price == approved_allocation.unit_cost

    [sent] = broker.received
    assert sent.quantity == approved_allocation.quantity


def test_the_limit_price_is_in_quoted_terms_not_structure_money(
    enabled_service, approved_allocation, fake_broker
) -> None:
    """The factor-of-100 error, checked end to end."""
    broker = fake_broker()

    enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id], authorized=True, broker=broker
    )

    [sent] = broker.received
    multiplier = approved_allocation.legs[0].multiplier
    assert sent.limit_price is not None
    assert sent.limit_price < approved_allocation.unit_cost
    assert sent.limit_price <= approved_allocation.unit_cost / Decimal(multiplier)


def test_a_run_is_stored(enabled_service, approved_allocation, fake_broker) -> None:
    run = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id],
        authorized=True,
        broker=fake_broker(),
    )

    assert run.stored
    assert enabled_service.get_run(run.result.run_id) is not None
    assert len(enabled_service.history()) == 1


def test_a_second_run_over_the_same_authorisation_sends_nothing(
    enabled_service, approved_allocation, fake_broker
) -> None:
    """One opportunity, one order. The most important property in the milestone."""
    first_broker = fake_broker()
    enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id],
        authorized=True,
        broker=first_broker,
    )

    second_broker = fake_broker()
    second = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id],
        authorized=True,
        broker=second_broker,
    )

    assert second_broker.orders_submitted == 0
    assert second_broker.received == []
    [record] = second.result.executions
    assert ExecutionReasonCode.ALREADY_SUBMITTED in record.reason_codes
    assert second.result.counts.already_submitted == 1


def test_a_re_run_after_a_submission_is_a_different_run(
    enabled_service, approved_allocation, fake_broker
) -> None:
    """The ledger's state is part of a run's identity.

    The same authorisations executed against a ledger that has since recorded a
    submission are a different decision reaching a different answer. Deriving
    the id from the inputs alone would collide the two and the immutable store
    would refuse the second — the same lesson the allocation ledger records
    about the campaign's committed state.
    """
    first = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id],
        authorized=True,
        broker=fake_broker(),
    )
    second = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id],
        authorized=True,
        broker=fake_broker(),
    )

    assert first.result.run_id != second.result.run_id
    assert first.stored and second.stored


def test_repeating_a_refused_run_is_idempotent(enabled_service, approved_allocation) -> None:
    """An unchanged re-run lands on the same id and stores one record of it."""
    first = enabled_service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)
    second = enabled_service.run(allocation_ids=[approved_allocation.allocation_id], dry_run=True)

    assert first.result.run_id == second.result.run_id


def test_a_duplicate_refusal_is_not_written_to_the_ledger(
    enabled_service, approved_allocation, fake_broker
) -> None:
    """Nothing new was attempted, so nothing new is recorded as an attempt."""
    enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id],
        authorized=True,
        broker=fake_broker(),
    )
    assert len(enabled_service.history()) == 1

    enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id],
        authorized=True,
        broker=fake_broker(),
    )
    assert len(enabled_service.history()) == 1


def test_a_broker_rejection_is_recorded_and_the_run_reports_it(
    enabled_service, approved_allocation, fake_broker
) -> None:
    broker = fake_broker(status=OrderStatus.REJECTED, message="market closed")

    run = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id], authorized=True, broker=broker
    )

    assert run.result.status is ExecutionRunStatus.NOTHING_SUBMITTED
    [record] = run.result.executions
    assert record.state is ExecutionState.REJECTED


def test_an_uncertain_submission_makes_the_run_partial_never_successful(
    enabled_service, approved_allocation, fake_broker
) -> None:
    """An order whose fate is unknown is not a success; calling it one stops it
    being looked at."""
    from trading_system.broker.base import BrokerTimeoutError

    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))

    run = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id], authorized=True, broker=broker
    )

    assert run.result.status is ExecutionRunStatus.PARTIAL
    assert run.result.counts.uncertain == 1


# ---------------------------------------------------------------------------
# Selecting what to execute
# ---------------------------------------------------------------------------
def test_an_unknown_allocation_id_is_refused_not_created(enabled_service) -> None:
    run = enabled_service.run(allocation_ids=["allocation-nope"], authorized=True)

    assert run.result.status is ExecutionRunStatus.CONFIGURATION_ERROR
    assert "never creates one" in (run.result.status_detail or "")


def test_a_symbol_the_allocation_run_did_not_cover_is_refused(enabled_service) -> None:
    """This stage executes an authorisation; it never makes one."""
    run = enabled_service.run(symbols=["TSLA"], authorized=True)

    assert run.result.status is ExecutionRunStatus.CONFIGURATION_ERROR


def test_only_approved_authorisations_are_selected(
    enabled_service, approved_allocation, fake_broker
) -> None:
    run = enabled_service.run(authorized=True, broker=fake_broker())

    assert [r.allocation_id for r in run.result.executions] == [approved_allocation.allocation_id]


def test_a_missing_allocation_run_is_reported_honestly(
    settings_paper, system_config, clock, tmp_path, stub_repositories
) -> None:
    from .conftest import _StubRuns

    repositories = dict(stub_repositories, allocation_repository=_StubRuns(None))
    service = ExecutionService(
        settings=settings_paper,
        config=system_config.model_copy(
            update={"execution": system_config.execution.model_copy(update={"enabled": True})}
        ),
        clock=clock,
        root=tmp_path,
        **repositories,
    )

    run = service.run(authorized=True)

    assert run.result.status is ExecutionRunStatus.CONFIGURATION_ERROR
    assert "allocation run" in (run.result.status_detail or "")


# ---------------------------------------------------------------------------
# Refusals are recorded as fully as submissions
# ---------------------------------------------------------------------------
def test_a_refused_execution_is_stored_with_its_reason(
    enabled_service, approved_allocation, fake_broker
) -> None:
    """ "Why did nothing happen" is answerable from the same place as "what did
    we send"."""
    stale = approved_allocation.model_copy(update={"unit_cost": None})
    enabled_service._allocation_repository.run = (
        enabled_service._allocation_repository.run.model_copy(update={"allocations": [stale]})
    )

    run = enabled_service.run(authorized=True, broker=fake_broker())

    [record] = run.result.executions
    assert record.state is ExecutionState.REJECTED
    assert ExecutionReasonCode.PRICE_UNAVAILABLE in record.reason_codes
    assert enabled_service.repository.base(record.execution_id) is not None


def test_missing_provenance_is_distinct_from_a_refusal(
    settings_paper, system_config, clock, tmp_path, stub_repositories, fake_broker
) -> None:
    """ "We could not look" and "we declined" are different facts."""
    from .conftest import _StubRuns

    repositories = dict(stub_repositories, research_repository=_StubRuns(None))
    service = ExecutionService(
        settings=settings_paper,
        config=system_config.model_copy(
            update={"execution": system_config.execution.model_copy(update={"enabled": True})}
        ),
        clock=clock,
        root=tmp_path,
        **repositories,
    )

    run = service.run(authorized=True, broker=fake_broker())

    [record] = run.result.executions
    assert ExecutionReasonCode.PROVENANCE_UNAVAILABLE in record.reason_codes


def test_a_broker_that_cannot_be_opened_stops_the_run(
    settings_paper, system_config, clock, tmp_path, stub_repositories
) -> None:
    from trading_system.broker.base import BrokerConfigurationError

    def refuse(*args, **kwargs):
        raise BrokerConfigurationError("no gateway configured")

    service = ExecutionService(
        settings=settings_paper,
        config=system_config.model_copy(
            update={"execution": system_config.execution.model_copy(update={"enabled": True})}
        ),
        clock=clock,
        root=tmp_path,
        broker_factory=refuse,
        **stub_repositories,
    )

    run = service.run(authorized=True)

    assert run.result.status is ExecutionRunStatus.BROKER_UNAVAILABLE
    assert run.result.orders_submitted == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def test_a_submitted_order_can_be_cancelled(
    enabled_service, approved_allocation, fake_broker
) -> None:
    broker = fake_broker()
    run = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id], authorized=True, broker=broker
    )
    [record] = run.result.executions

    cancelled = enabled_service.cancel(record.execution_id, broker=broker)

    assert cancelled is not None
    assert cancelled.state is ExecutionState.CANCELLED
    assert broker.cancelled == ["fake-order-1"]
    assert broker.orders_submitted == 1, "cancelling must never submit"


def test_a_failed_cancellation_leaves_the_order_pending_cancel(
    enabled_service, approved_allocation, fake_broker
) -> None:
    """The order may still be working, and the record says exactly that."""
    from trading_system.broker.base import BrokerResponseError

    broker = fake_broker()
    run = enabled_service.run(
        allocation_ids=[approved_allocation.allocation_id], authorized=True, broker=broker
    )
    [record] = run.result.executions
    broker.script.raise_on_cancel = BrokerResponseError("cannot cancel")

    result = enabled_service.cancel(record.execution_id, broker=broker)

    assert result is not None
    assert result.state is ExecutionState.CANCEL_PENDING
    assert ExecutionReasonCode.CANCEL_FAILED in result.reason_codes


def test_cancelling_an_unknown_execution_reads_as_none(enabled_service, fake_broker) -> None:
    assert enabled_service.cancel("execution-nope", broker=fake_broker()) is None


# ---------------------------------------------------------------------------
# The service holds no model
# ---------------------------------------------------------------------------
def test_the_service_never_recalculates_a_quantity(enabled_service) -> None:
    import inspect

    source = inspect.getsource(ExecutionService)
    for forbidden in ("max_units", "AllocationEngine", "RiskEngine", "score_opportunity"):
        assert forbidden not in source, f"the execution service references {forbidden}"
