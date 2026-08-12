"""The execution store (brief section 42.10).

This ledger is the record of what was actually sent to a broker. If it
disagrees with reality the system either places a trade twice or believes it
holds a position it does not, so the properties tested here are the same ones
the allocation ledger has, held to a higher standard:

* an execution record is written once and never rewritten;
* later broker news is *appended*, and the current state is folded from the
  history, so an order that reported two fills can still show it once reported
  one;
* a dry run never enters the ledger at all.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    ExecutionEventType,
    ExecutionReasonCode,
    ExecutionState,
    OrderStatus,
)
from trading_system.execution.models import (
    ExecutionEvent,
    ExecutionRunCounts,
    ExecutionRunResult,
    execution_identifier,
)
from trading_system.execution.store import ExecutionStoreError

from .conftest import NOW

pytestmark = pytest.mark.unit


def _event(record, *, sequence: int, state: ExecutionState, **overrides) -> ExecutionEvent:
    fields = {
        "event_id": f"evt-{sequence}",
        "execution_id": record.execution_id,
        "sequence": sequence,
        "event_type": ExecutionEventType.EXECUTION_SUBMITTED,
        "state": state,
        "occurred_at": NOW,
        "observed_at": NOW,
        "source": "SIMULATOR",
    }
    fields.update(overrides)
    return ExecutionEvent(**fields)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------
def test_a_record_reads_back_identically(repository, make_record) -> None:
    record = make_record()
    repository.save(record)

    assert repository.base(record.execution_id) == record


def test_writing_different_content_to_an_existing_id_raises(repository, make_record) -> None:
    record = make_record()
    repository.save(record)

    with pytest.raises(ExecutionStoreError, match="immutable"):
        repository.save(record.model_copy(update={"quantity": 7}))


def test_a_dry_run_is_never_stored(repository, make_record) -> None:
    """A diagnostic in this ledger would make a later run believe an order went out."""
    diagnostic = make_record(state=ExecutionState.VALIDATED, dry_run=True)

    with pytest.raises(ExecutionStoreError, match="dry run"):
        repository.save(diagnostic)
    assert repository.history() == []


def test_a_dry_run_run_record_is_never_stored(repository, versions) -> None:
    from trading_system.domain.enums import ExecutionRunStatus, TradingMode

    run = ExecutionRunResult(
        run_id="execrun-dry",
        campaign_id="campaign-001",
        as_of=NOW,
        generated_at=NOW,
        status=ExecutionRunStatus.DRY_RUN,
        trading_mode=TradingMode.PAPER,
        dry_run=True,
        broker="NONE",
        policy_version="2026.08.10-1",
        versions=versions,
    )
    with pytest.raises(ExecutionStoreError, match="dry run"):
        repository.save_run(run)


# ---------------------------------------------------------------------------
# Append-only history and folding
# ---------------------------------------------------------------------------
def test_the_current_state_is_folded_from_the_events(repository, make_record) -> None:
    record = make_record()
    repository.save(record)
    repository.append_event(
        _event(record, sequence=1, state=ExecutionState.SUBMITTED, broker_order_id="b-1")
    )

    current = repository.current(record.execution_id)

    assert current is not None
    assert current.state is ExecutionState.SUBMITTED
    assert current.broker_order_id == "b-1"


def test_the_base_record_is_untouched_by_later_events(repository, make_record) -> None:
    """The whole point of appending: the original is still there to read."""
    record = make_record()
    repository.save(record)
    repository.append_event(
        _event(record, sequence=1, state=ExecutionState.SUBMITTED, broker_order_id="b-1")
    )

    assert repository.base(record.execution_id).state is ExecutionState.SUBMISSION_PENDING


def test_intermediate_states_are_preserved(repository, make_record) -> None:
    """An order that reported two fills can still show it once reported one."""
    record = make_record(quantity=10)
    repository.save(record)
    repository.append_event(
        _event(record, sequence=1, state=ExecutionState.SUBMITTED, broker_order_id="b-1")
    )
    repository.append_event(
        _event(
            record,
            sequence=2,
            state=ExecutionState.PARTIALLY_FILLED,
            event_type=ExecutionEventType.EXECUTION_PARTIAL_FILL,
            filled_quantity=4,
            remaining_quantity=6,
        )
    )
    repository.append_event(
        _event(
            record,
            sequence=3,
            state=ExecutionState.FILLED,
            event_type=ExecutionEventType.EXECUTION_FILLED,
            filled_quantity=10,
            remaining_quantity=0,
        )
    )

    events = repository.events(record.execution_id)
    assert [event.state for event in events] == [
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.FILLED,
    ]
    assert repository.current(record.execution_id).filled_quantity == 10


def test_events_are_ordered_by_sequence_not_arrival(repository, make_record) -> None:
    """Two observations can share a timestamp; the sequence is what happened."""
    record = make_record(quantity=10)
    repository.save(record)
    repository.append_event(
        _event(
            record,
            sequence=2,
            state=ExecutionState.PARTIALLY_FILLED,
            event_type=ExecutionEventType.EXECUTION_PARTIAL_FILL,
            filled_quantity=4,
        )
    )
    repository.append_event(
        _event(record, sequence=1, state=ExecutionState.SUBMITTED, broker_order_id="b-1")
    )

    assert [event.sequence for event in repository.events(record.execution_id)] == [1, 2]
    assert repository.current(record.execution_id).state is ExecutionState.PARTIALLY_FILLED


def test_a_replayed_event_does_not_duplicate(repository, make_record) -> None:
    record = make_record()
    repository.save(record)
    event = _event(record, sequence=1, state=ExecutionState.SUBMITTED, broker_order_id="b-1")
    repository.append_event(event)
    repository.append_event(event)

    assert len(repository.events(record.execution_id)) == 1


def test_a_history_that_cannot_be_replayed_raises(repository, make_record) -> None:
    """A contradiction is surfaced, not quietly skipped.

    Silently ignoring it would leave the wrong state on screen while the file
    on disk said something else.
    """
    record = make_record()
    repository.save(record)
    repository.append_event(
        _event(record, sequence=1, state=ExecutionState.FILLED, filled_quantity=1)
    )
    repository.append_event(
        _event(record, sequence=2, state=ExecutionState.SUBMITTED, broker_order_id="b-1")
    )

    with pytest.raises(ExecutionStoreError, match="cannot be replayed"):
        repository.current(record.execution_id)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
def test_executions_are_found_by_request_id(repository, make_record) -> None:
    first = make_record(execution_request_id="req-a", attempt=0)
    second = make_record(execution_request_id="req-a", attempt=1)
    other = make_record(execution_request_id="req-b")
    for record in (first, second, other):
        repository.save(record)

    found = repository.for_request("req-a")

    assert {r.execution_id for r in found} == {first.execution_id, second.execution_id}


def test_executions_are_found_by_allocation_id(repository, make_record) -> None:
    mine = make_record(allocation_id="allocation-mine")
    theirs = make_record(execution_request_id="req-b", allocation_id="allocation-theirs")
    repository.save(mine)
    repository.save(theirs)

    found = repository.for_allocation("allocation-mine")

    assert [r.execution_id for r in found] == [mine.execution_id]


def test_the_live_index_reflects_folded_state(repository, make_record) -> None:
    """A record that has since been rejected must not read as live."""
    record = make_record()
    repository.save(record)
    repository.append_event(
        _event(
            record,
            sequence=1,
            state=ExecutionState.REJECTED,
            event_type=ExecutionEventType.EXECUTION_REJECTED,
            reason_code=ExecutionReasonCode.BROKER_REJECTED,
            broker_status=OrderStatus.REJECTED,
        )
    )

    assert repository.live_for_request(record.execution_request_id) == []


def test_an_unknown_execution_reads_as_none(repository) -> None:
    assert repository.current("execution-nope") is None
    assert repository.base("execution-nope") is None
    assert repository.events("execution-nope") == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_execution_ids_are_deterministic() -> None:
    first = execution_identifier(execution_request_id="req-a", attempt=0)
    second = execution_identifier(execution_request_id="req-a", attempt=0)
    assert first == second


def test_each_attempt_has_its_own_id() -> None:
    assert execution_identifier(execution_request_id="req-a", attempt=0) != execution_identifier(
        execution_request_id="req-a", attempt=1
    )


def test_a_stored_record_round_trips_through_json(repository, make_record) -> None:
    record = make_record(
        average_fill_price=Decimal("6.05"),
        filled_quantity=1,
        state=ExecutionState.FILLED,
        broker_order_id="b-1",
    )
    repository.save(record)

    assert repository.base(record.execution_id) == record


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def test_a_run_reads_back_and_is_immutable(repository, make_record, versions) -> None:
    from trading_system.domain.enums import ExecutionRunStatus, TradingMode

    record = make_record(state=ExecutionState.SUBMITTED, broker_order_id="b-1")
    run = ExecutionRunResult(
        run_id="execrun-0001",
        campaign_id="campaign-001",
        as_of=NOW,
        generated_at=NOW,
        status=ExecutionRunStatus.SUCCESS,
        trading_mode=TradingMode.PAPER,
        broker="SIMULATOR",
        policy_version="2026.08.10-1",
        executions=[record],
        counts=ExecutionRunCounts(considered=1, submitted=1),
        orders_submitted=1,
        versions=versions,
    )
    repository.save_run(run)

    assert repository.get_run("execrun-0001") == run
    assert repository.latest_run() == run
    with pytest.raises(ExecutionStoreError, match="immutable"):
        repository.save_run(run.model_copy(update={"orders_submitted": 2}))


def test_run_history_is_newest_first(repository, versions) -> None:
    from trading_system.domain.enums import ExecutionRunStatus, TradingMode

    def _run(run_id: str, at):
        return ExecutionRunResult(
            run_id=run_id,
            campaign_id="campaign-001",
            as_of=at,
            generated_at=at,
            status=ExecutionRunStatus.NOTHING_SUBMITTED,
            trading_mode=TradingMode.PAPER,
            broker="SIMULATOR",
            policy_version="2026.08.10-1",
            versions=versions,
        )

    repository.save_run(_run("execrun-old", NOW - timedelta(hours=2)))
    repository.save_run(_run("execrun-new", NOW))

    assert [entry.run_id for entry in repository.run_history()] == ["execrun-new", "execrun-old"]
