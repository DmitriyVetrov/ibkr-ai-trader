"""Ambiguous submissions (brief sections 42.5 and 16).

The scenario: the client sends an order, IBKR accepts it, and the client times
out before the acknowledgement arrives. There is no way to tell that apart from
"the order never arrived", so the system must not try — it records ``UNKNOWN``
and stops.

The failure this prevents is the expensive one. A retry places the trade twice,
and the second position is invisible to every limit that approved the first.
"""

from __future__ import annotations

import pytest

from trading_system.broker.base import (
    BrokerConnectionError,
    BrokerError,
    BrokerResponseError,
    BrokerTimeoutError,
    ReadOnlyBrokerError,
)
from trading_system.domain.enums import ExecutionReasonCode, ExecutionState
from trading_system.execution.execution_engine import ExecutionEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(repository, clock):
    def _engine(broker):
        return ExecutionEngine(broker=broker, repository=repository, clock=clock)

    return _engine


# ---------------------------------------------------------------------------
# The ambiguous case
# ---------------------------------------------------------------------------
def test_a_timeout_produces_an_uncertain_submission(
    engine, make_record, make_intent, fake_broker
) -> None:
    broker = fake_broker(raise_on_submit=BrokerTimeoutError("IBKR did not answer"))

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.state is ExecutionState.UNKNOWN
    assert outcome.uncertain
    assert ExecutionReasonCode.BROKER_TIMEOUT in outcome.record.reason_codes


def test_a_timeout_is_never_reported_as_a_failure(
    engine, make_record, make_intent, fake_broker
) -> None:
    """FAILED means *provably not sent*, and a timeout proves nothing."""
    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.state is not ExecutionState.FAILED


def test_an_uncertain_submission_counts_as_possibly_live(
    engine, make_record, make_intent, fake_broker
) -> None:
    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.submitted, "UNKNOWN means an order may exist at the broker"


def test_there_is_no_automatic_retry(engine, make_record, make_intent, fake_broker) -> None:
    """Brief section 38: the engine sends once and stops."""
    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))

    engine(broker).submit(make_record(), make_intent())

    assert broker.orders_submitted == 1
    assert len(broker.received) == 1


def test_the_attempt_is_counted_even_though_it_was_never_answered(
    engine, make_record, make_intent, fake_broker
) -> None:
    """The counter is incremented *before* the call, deliberately.

    Counting only successes would let exactly this case report zero submitted
    orders — the one circumstance where a caller must not believe nothing was
    sent.
    """
    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))

    engine(broker).submit(make_record(), make_intent())

    assert broker.orders_submitted == 1


def test_configuration_cannot_switch_automatic_retry_on(system_config) -> None:
    """`auto_retry_on_timeout: true` fails to load, rather than being ignored."""
    from pydantic import ValidationError

    from trading_system.infrastructure.settings import ExecutionConfig

    payload = system_config.execution.model_dump() | {"auto_retry_on_timeout": True}
    with pytest.raises(ValidationError, match="two positions"):
        ExecutionConfig.model_validate(payload)


# ---------------------------------------------------------------------------
# Which failures are provable, and which are not
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        BrokerResponseError("the broker answered with nonsense"),
        BrokerConnectionError("the connection dropped mid-call"),
        BrokerError("something went wrong"),
        RuntimeError("something unexpected"),
    ],
)
def test_any_failure_after_the_attempt_began_is_uncertain(
    engine, make_record, make_intent, fake_broker, error
) -> None:
    """Nothing that happens once bytes may have moved is treated as non-delivery."""
    broker = fake_broker(raise_on_submit=error)

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.state is ExecutionState.UNKNOWN


def test_a_read_only_broker_is_a_provable_failure(
    engine, make_record, make_intent, fake_broker
) -> None:
    """Refused by our own guard, before any bytes moved."""
    broker = fake_broker(read_only=True)

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.state is ExecutionState.FAILED
    assert ExecutionReasonCode.BROKER_READ_ONLY in outcome.record.reason_codes
    assert broker.orders_submitted == 0
    assert broker.received == []


def test_a_disconnected_broker_is_a_provable_failure(
    engine, make_record, make_intent, fake_broker
) -> None:
    broker = fake_broker(connected=False)

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.state is ExecutionState.FAILED
    assert ExecutionReasonCode.BROKER_DISCONNECTED in outcome.record.reason_codes
    assert broker.orders_submitted == 0


def test_a_read_only_refusal_never_reaches_the_submission_hook(
    engine, make_record, make_intent, fake_broker
) -> None:
    """The guard is on the final ``place_order``, so a subclass cannot bypass it."""
    broker = fake_broker(read_only=True)

    with pytest.raises(ReadOnlyBrokerError):
        broker.place_order(make_intent())
    assert broker.received == []


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_an_uncertain_submission_is_resolved_by_looking_not_by_sending(
    engine, make_record, make_intent, fake_broker, repository
) -> None:
    """The supported response: ask the broker what it has."""
    from decimal import Decimal

    from trading_system.domain.enums import OrderSide, OrderStatus, SecurityType
    from trading_system.domain.models import BrokerOrder

    from .conftest import NOW

    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))
    driver = engine(broker)
    outcome = driver.submit(make_record(), make_intent())
    assert outcome.record.state is ExecutionState.UNKNOWN

    # The order was in fact live all along. The broker now says so.
    broker.script.open_orders = [
        BrokerOrder(
            broker_order_id="fake-order-1",
            as_of=NOW,
            source="FAKE",
            symbol="NVDA",
            security_type=SecurityType.OPTION,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            order_type="LMT",
            status=OrderStatus.SUBMITTED,
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            updated_at=NOW,
        )
    ]
    resolved = driver.resolve(outcome.record.model_copy(update={"broker_order_id": "fake-order-1"}))

    assert resolved.record.state is ExecutionState.SUBMITTED
    assert broker.orders_submitted == 1, "resolving must never submit"


def test_resolution_that_finds_nothing_leaves_the_state_unresolved(
    engine, make_record, make_intent, fake_broker
) -> None:
    """Absence from the open orders is also what a filled order looks like.

    So it is not resolved to "not sent" — that would be the one reading that
    makes a retry look safe.
    """
    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))
    driver = engine(broker)
    outcome = driver.submit(make_record(), make_intent())

    resolved = driver.resolve(outcome.record)

    assert resolved.record.state is ExecutionState.UNKNOWN
    assert ExecutionReasonCode.UNKNOWN_BROKER_STATE in resolved.record.reason_codes


def test_the_uncertain_record_explains_itself_without_hedging(
    engine, make_record, make_intent, fake_broker
) -> None:
    """The state most likely to be misread as "it did not go through"."""
    from trading_system.execution.report import render_execution

    broker = fake_broker(raise_on_submit=BrokerTimeoutError("no answer"))
    outcome = engine(broker).submit(make_record(), make_intent())

    text = render_execution(outcome.record)
    assert "UNCERTAIN" in text
    assert "Do NOT resubmit" in text
