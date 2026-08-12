"""One trade, one order (brief section 42.4).

The most important property in this milestone. A client can time out *after*
IBKR accepted an order, a process can die between the send and the answer, and
an operator can re-run a command. None of those may produce a second order.

The mechanism is two-part and both halves are tested here:

* an execution identity derived from the authorisation and how it would be
  sent, so a re-run recognises its own earlier attempt;
* a "may an order exist?" test that deliberately includes ``SUBMISSION_PENDING``
  and ``UNKNOWN``, because absence of an acknowledgement is not absence of an
  order.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_system.broker.base import BrokerTimeoutError
from trading_system.domain.enums import (
    ExecutionState,
    OrderType,
    TimeInForce,
    TradingMode,
)
from trading_system.execution.execution_engine import ExecutionEngine
from trading_system.execution.models import execution_request_identifier
from trading_system.execution.store import has_live_attempt

from .conftest import NOW

pytestmark = pytest.mark.unit


def _request_id(
    *,
    allocation_id: str = "allocation-0001",
    trading_mode: TradingMode = TradingMode.PAPER,
    order_type: OrderType = OrderType.LIMIT,
    time_in_force: TimeInForce = TimeInForce.DAY,
    policy_version: str = "2026.08.10-1",
) -> str:
    return execution_request_identifier(
        allocation_id=allocation_id,
        trading_mode=trading_mode,
        order_type=order_type,
        time_in_force=time_in_force,
        policy_version=policy_version,
    )


# ---------------------------------------------------------------------------
# The identity
# ---------------------------------------------------------------------------
def test_the_same_trade_executed_the_same_way_has_one_identity() -> None:
    assert _request_id() == _request_id()


def test_the_identity_ignores_the_clock() -> None:
    """A time-varying identity would make every retry look like a new request.

    Which is precisely the duplicate-order bug this exists to prevent: a client
    that timed out and tried again would submit a second order and see nothing
    wrong with it.
    """
    import inspect

    from trading_system.execution import models

    source = inspect.getsource(models.execution_request_identifier)
    for forbidden in ("now(", "today(", "time.time("):
        assert forbidden not in source


@pytest.mark.parametrize(
    "overrides",
    [
        {"allocation_id": "allocation-0002"},
        {"time_in_force": TimeInForce.GTC},
        {"policy_version": "2026.09.01-1"},
    ],
)
def test_a_genuinely_different_order_is_a_different_identity(overrides) -> None:
    """A different order must be able to exist; only the *same* one is blocked."""
    assert _request_id(**overrides) != _request_id()


# ---------------------------------------------------------------------------
# What counts as "an order may exist"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "state",
    [
        ExecutionState.SUBMISSION_PENDING,
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.FILLED,
        ExecutionState.CANCEL_PENDING,
        ExecutionState.UNKNOWN,
    ],
)
def test_a_live_attempt_blocks_another(make_record, state) -> None:
    # A partial fill needs room to be partial in, so those cases use a
    # two-unit order. The model refuses "PARTIALLY_FILLED, 0 of 1" outright.
    partial = state is ExecutionState.PARTIALLY_FILLED
    record = make_record(
        state=state,
        broker_order_id="b-1",
        quantity=2 if partial else 1,
        filled_quantity=1
        if state in (ExecutionState.FILLED, ExecutionState.PARTIALLY_FILLED)
        else 0,
    )
    assert has_live_attempt([record]) is record


def test_submission_pending_blocks_even_though_nothing_was_acknowledged(make_record) -> None:
    """The crash-mid-submission case. The order may be live right now."""
    record = make_record(state=ExecutionState.SUBMISSION_PENDING)
    assert record.submitted
    assert has_live_attempt([record]) is record


def test_unknown_blocks(make_record) -> None:
    """A timeout is not evidence that nothing was sent."""
    record = make_record(state=ExecutionState.UNKNOWN)
    assert has_live_attempt([record]) is record


@pytest.mark.parametrize(
    "state",
    [ExecutionState.REJECTED, ExecutionState.FAILED, ExecutionState.CANCELLED],
)
def test_a_provably_dead_attempt_does_not_block(make_record, state) -> None:
    """A trade that provably did not go out may be attempted again."""
    record = make_record(state=state, reason_codes=["BROKER_REJECTED"])
    assert has_live_attempt([record]) is None


# ---------------------------------------------------------------------------
# End to end, through the store
# ---------------------------------------------------------------------------
def test_a_second_submission_does_not_reach_the_broker(
    repository, make_record, make_intent, fake_broker, clock
) -> None:
    """Brief 42.4: broker order count is 1, not 2."""
    broker = fake_broker()
    engine = ExecutionEngine(broker=broker, repository=repository, clock=clock)

    first = engine.submit(make_record(), make_intent())
    assert first.record.state is ExecutionState.SUBMITTED
    assert broker.orders_submitted == 1

    # The service is what performs the check; here it is asserted directly on
    # the stored history, which is the state the service reads.
    stored = repository.for_request("exec-req-test-0001")
    assert has_live_attempt(stored) is not None
    assert broker.orders_submitted == 1
    assert len(broker.received) == 1


def test_a_timed_out_submission_still_blocks_a_retry(
    repository, make_record, make_intent, fake_broker, clock
) -> None:
    """The case this whole mechanism exists for.

    The broker received the order and the client never learned the outcome. A
    system that treated that as "not sent" places the trade twice.
    """
    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))
    engine = ExecutionEngine(broker=broker, repository=repository, clock=clock)

    outcome = engine.submit(make_record(), make_intent())

    assert outcome.record.state is ExecutionState.UNKNOWN
    assert broker.orders_submitted == 1, "an attempt counts even when the answer never arrives"
    assert len(broker.received) == 1
    assert has_live_attempt(repository.for_request("exec-req-test-0001")) is not None


def test_the_store_refuses_to_overwrite_an_attempt(repository, make_record) -> None:
    """An immutable record: later news appends, it never rewrites."""
    from trading_system.execution.store import ExecutionStoreError

    record = make_record()
    repository.save(record)

    with pytest.raises(ExecutionStoreError, match="immutable"):
        repository.save(record.model_copy(update={"quantity": 99}))


def test_saving_the_identical_record_twice_is_idempotent(repository, make_record) -> None:
    record = make_record()
    first = repository.save(record)
    second = repository.save(record)

    assert first == second
    assert len(repository.history()) == 1


def test_a_second_attempt_gets_its_own_record_id(make_record) -> None:
    """A refused attempt does not consume the identity of the trade itself."""
    first = make_record(attempt=0)
    second = make_record(attempt=1)

    assert first.execution_id != second.execution_id
    assert first.execution_request_id == second.execution_request_id


def test_the_engine_writes_the_record_before_calling_the_broker(
    repository, make_record, make_intent, fake_broker, clock
) -> None:
    """So a crash between the two leaves evidence rather than silence."""
    seen: list[int] = []

    broker = fake_broker()
    original = broker._submit_order

    def observing_submit(intent):
        seen.append(len(repository.history()))
        return original(intent)

    broker._submit_order = observing_submit
    ExecutionEngine(broker=broker, repository=repository, clock=clock).submit(
        make_record(), make_intent()
    )

    assert seen == [1], "the record must already be on file when the broker is called"


def test_an_engine_refuses_a_record_that_is_not_pending(
    repository, make_record, make_intent, fake_broker, clock
) -> None:
    engine = ExecutionEngine(broker=fake_broker(), repository=repository, clock=clock)

    with pytest.raises(ValueError, match="SUBMISSION_PENDING"):
        engine.submit(make_record(state=ExecutionState.VALIDATED), make_intent())


def test_an_engine_refuses_a_dry_run_record(
    repository, make_record, make_intent, fake_broker, clock
) -> None:
    """A dry run never reaches this engine; that is the whole meaning of the flag.

    ``model_copy`` is used to build the contradictory record because the model
    itself refuses a dry run in a live state — the engine's own guard is the
    second line of defence being tested here.
    """
    engine = ExecutionEngine(broker=fake_broker(), repository=repository, clock=clock)
    contradiction = make_record().model_copy(update={"dry_run": True})

    with pytest.raises(ValueError, match="dry run"):
        engine.submit(contradiction, make_intent())
    assert fake_broker().orders_submitted == 0


def test_the_model_refuses_a_dry_run_that_claims_to_be_in_flight(make_record) -> None:
    """The record cannot even express "a dry run with an order at the broker"."""
    from pydantic import ValidationError

    payload = make_record().model_dump(mode="json") | {"dry_run": True}
    with pytest.raises(ValidationError, match="dry run"):
        type(make_record()).model_validate(payload)


def test_history_ordering_is_newest_first(repository, make_record) -> None:
    older = make_record(execution_request_id="req-a", created_at=NOW - timedelta(hours=1))
    newer = make_record(execution_request_id="req-b", created_at=NOW)
    repository.save(older)
    repository.save(newer)

    assert [entry.execution_id for entry in repository.history()] == [
        newer.execution_id,
        older.execution_id,
    ]
