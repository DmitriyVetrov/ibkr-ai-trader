"""Broker rejections (brief sections 42.6 and 20).

Milestone 7 approved the trade against the prices that were visible then. The
market moves, and IBKR refuses. The rule is that execution records the refusal
and changes nothing: it does not shrink the order to fit, does not reprice it,
and does not touch the authorisation.

A trade that no longer fits needs a new Milestone 7 authorisation — one that
was sized against the prices that actually exist.
"""

from __future__ import annotations

import pytest

from trading_system.domain.enums import ExecutionReasonCode, ExecutionState, OrderStatus
from trading_system.execution.execution_engine import ExecutionEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(repository, clock):
    def _engine(broker):
        return ExecutionEngine(broker=broker, repository=repository, clock=clock)

    return _engine


def test_a_broker_rejection_is_recorded_as_such(
    engine, make_record, make_intent, fake_broker
) -> None:
    broker = fake_broker(status=OrderStatus.REJECTED, message="insufficient buying power")

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.state is ExecutionState.REJECTED
    assert ExecutionReasonCode.BROKER_REJECTED in outcome.record.reason_codes


def test_the_broker_message_is_preserved_verbatim(
    engine, make_record, make_intent, fake_broker
) -> None:
    """Never rewritten. The broker's words are the evidence of what happened."""
    broker = fake_broker(status=OrderStatus.REJECTED, message="Order rejected - reason:201")

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.broker_message == "Order rejected - reason:201"


def test_a_rejection_does_not_change_the_authorisation(
    engine, make_record, make_intent, fake_broker, approved_allocation
) -> None:
    """Brief section 6: execution never mutates what Milestone 7 decided."""
    before = approved_allocation.model_dump()
    broker = fake_broker(status=OrderStatus.REJECTED, message="no")

    engine(broker).submit(make_record(), make_intent())

    assert approved_allocation.model_dump() == before


def test_a_rejection_does_not_shrink_the_order(
    engine, make_record, make_intent, fake_broker
) -> None:
    """Brief section 20: it does not modify quantity to make the order fit."""
    broker = fake_broker(status=OrderStatus.REJECTED, message="insufficient funds")
    record = make_record(quantity=5)

    outcome = engine(broker).submit(record, make_intent(quantity=5))

    assert outcome.record.quantity == 5
    assert outcome.record.filled_quantity == 0
    assert broker.orders_submitted == 1, "no second, smaller order was attempted"


def test_a_rejection_is_terminal(engine, make_record, make_intent, fake_broker) -> None:
    from trading_system.execution.state_machine import is_terminal

    broker = fake_broker(status=OrderStatus.REJECTED, message="no")
    outcome = engine(broker).submit(make_record(), make_intent())

    assert is_terminal(outcome.record.state)


def test_a_rejection_claims_no_position(engine, make_record, make_intent, fake_broker) -> None:
    broker = fake_broker(status=OrderStatus.REJECTED, message="no")

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.filled_quantity == 0
    assert outcome.record.average_fill_price is None
    assert outcome.record.fills == []


def test_the_capital_authorised_survives_a_rejection(
    engine, make_record, make_intent, fake_broker
) -> None:
    """The authorised amount is never overwritten by what actually happened."""
    broker = fake_broker(status=OrderStatus.REJECTED, message="no")
    record = make_record()

    outcome = engine(broker).submit(record, make_intent())

    assert outcome.record.capital_commitment == record.capital_commitment
    assert outcome.record.maximum_loss == record.maximum_loss
    assert outcome.record.executed_capital is None


# ---------------------------------------------------------------------------
# An acknowledgement without an identity
# ---------------------------------------------------------------------------
def test_an_acknowledgement_with_no_order_id_is_uncertain(
    engine, make_record, make_intent, fake_broker
) -> None:
    """An order we cannot name is one we cannot cancel or reconcile.

    Recording it as tracked would be a claim the system cannot honour.
    """
    broker = fake_broker(status=OrderStatus.SUBMITTED, broker_order_id=None)

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.state is ExecutionState.UNKNOWN


def test_the_record_cannot_express_submitted_without_an_order_id(make_record) -> None:
    from pydantic import ValidationError

    payload = make_record().model_dump(mode="json") | {
        "state": "SUBMITTED",
        "broker_order_id": None,
    }
    with pytest.raises(ValidationError, match="broker order id"):
        type(make_record()).model_validate(payload)


# ---------------------------------------------------------------------------
# Acknowledgement is not execution
# ---------------------------------------------------------------------------
def test_an_acknowledged_order_is_submitted_not_filled(
    engine, make_record, make_intent, fake_broker
) -> None:
    """Brief section 12. The single most consequential distinction here."""
    broker = fake_broker(status=OrderStatus.SUBMITTED, filled_quantity=0)

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.state is ExecutionState.SUBMITTED
    assert outcome.record.filled_quantity == 0


def test_nothing_infers_a_fill_from_the_submitted_quantity(
    engine, make_record, make_intent, fake_broker
) -> None:
    broker = fake_broker(status=OrderStatus.SUBMITTED, filled_quantity=0)

    outcome = engine(broker).submit(make_record(quantity=10), make_intent(quantity=10))

    assert outcome.record.quantity == 10
    assert outcome.record.filled_quantity == 0
    assert outcome.record.remaining_quantity == 10


def test_the_report_never_says_filled_for_an_acknowledgement(
    engine, make_record, make_intent, fake_broker
) -> None:
    from trading_system.execution.report import render_execution

    broker = fake_broker(status=OrderStatus.SUBMITTED)
    outcome = engine(broker).submit(make_record(), make_intent())

    text = render_execution(outcome.record)
    assert "NOT a fill" in text
