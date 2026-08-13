"""The reconciliation store (brief sections 35-36, 38).

Immutable results, an append-only index, an append-only per-run event history,
and a re-observation rather than a second record when identical state is
compared again.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.positions.factories import NOW, option_position, stock_position
from trading_system.domain.enums import ReconciliationEventType
from trading_system.reconciliation.models import (
    ReconciliationEvent,
    reconciliation_event_identifier,
)
from trading_system.reconciliation.store import (
    FilesystemReconciliationRepository,
    ReconciliationStoreError,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def repository(tmp_path) -> FilesystemReconciliationRepository:
    return FilesystemReconciliationRepository(tmp_path / "data" / "reconciliation")


def test_a_result_round_trips(repository, engine, inputs_for, snapshot_of) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    _, is_new = repository.save(result)
    assert is_new is True
    assert repository.get(result.reconciliation_id) == result


def test_storing_identical_content_twice_is_a_re_observation(
    repository, engine, inputs_for, snapshot_of
) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    repository.save(result)
    _, is_new = repository.save(result)

    assert is_new is False
    assert len(repository.history()) == 2
    assert sum(entry.reobserved for entry in repository.history()) == 1


def test_a_stored_reconciliation_is_never_overwritten(
    repository, engine, inputs_for, snapshot_of
) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    repository.save(result)
    with pytest.raises(ReconciliationStoreError, match="immutable"):
        repository.save(result.model_copy(update={"content_hash": "something-else"}))


def test_the_latest_result_is_the_newest_observation(
    repository, engine, inputs_for, snapshot_of
) -> None:
    first = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    second = engine.reconcile(
        inputs_for(
            snapshot=snapshot_of([stock_position()]),
            as_of=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
        )
    )
    repository.save(first)
    repository.save(second)

    latest = repository.latest()
    assert latest is not None
    assert latest.reconciliation_id == second.reconciliation_id


def test_the_history_records_what_the_comparison_found(
    repository, engine, inputs_for, snapshot_of
) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([stock_position()])))
    repository.save(result)

    [entry] = repository.history()
    assert entry.status == result.status.value
    assert entry.mismatches == result.counts.mismatches
    assert entry.orders_submitted == 0


def test_events_are_appended_and_never_duplicated(
    repository, engine, inputs_for, snapshot_of
) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    repository.save(result)
    event = ReconciliationEvent(
        event_id=reconciliation_event_identifier(
            reconciliation_id=result.reconciliation_id,
            sequence=0,
            event_type=ReconciliationEventType.RECONCILIATION_STARTED.value,
        ),
        reconciliation_id=result.reconciliation_id,
        sequence=0,
        event_type=ReconciliationEventType.RECONCILIATION_STARTED,
        occurred_at=NOW,
        observed_at=NOW,
        source="test",
    )

    assert repository.append_event(event) is True
    assert repository.append_event(event) is False
    assert len(repository.events(result.reconciliation_id)) == 1


def test_events_are_returned_in_sequence_order(repository, engine, inputs_for, snapshot_of) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    repository.save(result)
    for sequence in (2, 0, 1):
        repository.append_event(
            ReconciliationEvent(
                event_id=f"recevt-{sequence}",
                reconciliation_id=result.reconciliation_id,
                sequence=sequence,
                event_type=ReconciliationEventType.INTERNAL_LEDGER_READ,
                occurred_at=NOW,
                observed_at=NOW,
                source="test",
            )
        )

    events = repository.events(result.reconciliation_id)
    assert [event.sequence for event in events] == [0, 1, 2]


def test_an_unknown_reconciliation_is_none_rather_than_an_error(repository) -> None:
    assert repository.get("reconciliation-nope") is None
    assert repository.latest() is None
    assert repository.events("reconciliation-nope") == []


def test_a_run_records_its_own_history(service) -> None:
    """The steps the brief names: started, captured, read, completed."""
    run = service.run()
    events = service.repository.events(run.result.reconciliation_id)
    kinds = [event.event_type for event in events]

    assert kinds[0] is ReconciliationEventType.RECONCILIATION_STARTED
    assert kinds[-1] is ReconciliationEventType.RECONCILIATION_COMPLETED
    assert ReconciliationEventType.INTERNAL_LEDGER_READ in kinds


def test_a_failed_broker_read_is_recorded_in_the_history(make_service, monkeypatch) -> None:
    from trading_system.broker.base import BrokerConnectionError

    service = make_service()

    def refuse():
        raise BrokerConnectionError("gateway down")

    monkeypatch.setattr(service.broker, "get_positions", refuse)
    run = service.run()

    kinds = [event.event_type for event in service.repository.events(run.result.reconciliation_id)]
    assert ReconciliationEventType.BROKER_READ_FAILED in kinds
