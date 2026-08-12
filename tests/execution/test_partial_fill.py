"""Partial fills (brief sections 42.7 and 10).

A partially filled order is a real, smaller position. Reporting it as filled
would claim a structure that does not exist; reporting it as unfilled would
hide exposure the account actually holds. Both are ways of being wrong about
what is owned, so the counts are tracked exactly and never inferred.

The other rule tested here: **numbers outrank labels**. Where a broker's status
says ``FILLED`` and its own counts say four of ten, the counts win — and the
disagreement is recorded rather than smoothed over, because one of the two is a
bug.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.domain.enums import ExecutionState, OrderStatus
from trading_system.domain.models import Fill
from trading_system.execution.execution_engine import ExecutionEngine
from trading_system.execution.fill_tracker import state_for

from .conftest import NOW

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(repository, clock):
    def _engine(broker):
        return ExecutionEngine(broker=broker, repository=repository, clock=clock)

    return _engine


def _fill(quantity: int, price: str, index: int = 1) -> Fill:
    return Fill(
        fill_id=f"fill-{index}",
        leg_index=0,
        quantity=quantity,
        price=Decimal(price),
        filled_at=NOW,
    )


# ---------------------------------------------------------------------------
# The brief's worked example: 10 submitted, 4 fill, then 6 more
# ---------------------------------------------------------------------------
def test_four_of_ten_is_partially_filled(engine, make_record, make_intent, fake_broker) -> None:
    broker = fake_broker(
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=4,
        average_fill_price=Decimal("6.00"),
        fills=[_fill(4, "6.00")],
    )

    outcome = engine(broker).submit(make_record(quantity=10), make_intent(quantity=10))

    assert outcome.record.state is ExecutionState.PARTIALLY_FILLED
    assert outcome.record.filled_quantity == 4
    assert outcome.record.remaining_quantity == 6


def test_the_remaining_six_complete_the_order(
    engine, make_record, make_intent, fake_broker, repository, clock
) -> None:
    from trading_system.domain.enums import OrderSide, SecurityType
    from trading_system.domain.models import BrokerOrder
    from trading_system.execution.fill_tracker import event_from_broker_order

    broker = fake_broker(
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=4,
        average_fill_price=Decimal("6.00"),
        fills=[_fill(4, "6.00")],
    )
    outcome = engine(broker).submit(make_record(quantity=10), make_intent(quantity=10))

    completed = event_from_broker_order(
        outcome.record,
        BrokerOrder(
            broker_order_id="fake-order-1",
            as_of=NOW,
            source="FAKE",
            symbol="NVDA",
            security_type=SecurityType.OPTION,
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            order_type="LMT",
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("10"),
            remaining_quantity=Decimal("0"),
            average_fill_price=Decimal("6.02"),
            updated_at=NOW,
        ),
        sequence=2,
        observed_at=NOW,
        source="FAKE",
    )
    final = outcome.record.with_event(completed)

    assert final.state is ExecutionState.FILLED
    assert final.filled_quantity == 10
    assert final.remaining_quantity == 0


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------
def test_a_fill_can_never_exceed_the_submitted_quantity(make_record) -> None:
    """A broker cannot execute more than was sent, so this is a tracking bug."""
    from pydantic import ValidationError

    payload = make_record().model_dump(mode="json") | {
        "quantity": 5,
        "filled_quantity": 6,
        "state": "PARTIALLY_FILLED",
    }
    with pytest.raises(ValidationError, match="exceeds the submitted quantity"):
        type(make_record()).model_validate(payload)


def test_an_overreported_fill_is_clamped_at_the_submitted_quantity(
    engine, make_record, make_intent, fake_broker
) -> None:
    """A broker reporting more than was sent is contained, not believed."""
    broker = fake_broker(
        status=OrderStatus.FILLED, filled_quantity=99, average_fill_price=Decimal("6.00")
    )

    outcome = engine(broker).submit(make_record(quantity=2), make_intent(quantity=2))

    assert outcome.record.filled_quantity == 2


def test_filled_requires_the_whole_structure(make_record) -> None:
    """Reporting a partial as FILLED would claim a position that is not there."""
    from pydantic import ValidationError

    payload = make_record().model_dump(mode="json") | {
        "quantity": 10,
        "filled_quantity": 4,
        "state": "FILLED",
    }
    with pytest.raises(ValidationError, match="FILLED claims the whole structure"):
        type(make_record()).model_validate(payload)


def test_partially_filled_requires_a_fill_strictly_between(make_record) -> None:
    from pydantic import ValidationError

    payload = make_record().model_dump(mode="json") | {
        "quantity": 10,
        "filled_quantity": 0,
        "state": "PARTIALLY_FILLED",
    }
    with pytest.raises(ValidationError, match="strictly between"):
        type(make_record()).model_validate(payload)


# ---------------------------------------------------------------------------
# Numbers outrank labels
# ---------------------------------------------------------------------------
def test_a_broker_calling_a_partial_fill_filled_is_not_believed() -> None:
    state = state_for(
        OrderStatus.FILLED, filled_quantity=3, submitted_quantity=10, has_broker_order_id=True
    )
    assert state is ExecutionState.PARTIALLY_FILLED


def test_the_contradiction_is_recorded_rather_than_hidden(
    engine, make_record, make_intent, fake_broker
) -> None:
    """One of the two is a bug, and hiding it loses the evidence."""
    broker = fake_broker(
        status=OrderStatus.FILLED, filled_quantity=3, average_fill_price=Decimal("6.00")
    )

    outcome = engine(broker).submit(make_record(quantity=10), make_intent(quantity=10))

    assert outcome.record.state is ExecutionState.PARTIALLY_FILLED
    assert "counts are believed" in (outcome.record.failure_reason or "") or any(
        "counts are believed" in (event.detail or "") for event in outcome.events
    )


def test_a_broker_still_calling_it_submitted_while_it_fills_is_believed() -> None:
    """IBKR reports a partly filled working order as Submitted with a count."""
    state = state_for(
        OrderStatus.SUBMITTED, filled_quantity=2, submitted_quantity=10, has_broker_order_id=True
    )
    assert state is ExecutionState.PARTIALLY_FILLED


def test_no_fill_report_means_no_fill() -> None:
    """Not derived from the acknowledgement, the price, or hope."""
    state = state_for(
        OrderStatus.SUBMITTED, filled_quantity=0, submitted_quantity=10, has_broker_order_id=True
    )
    assert state is ExecutionState.SUBMITTED


def test_an_unrecognised_status_is_unknown_not_reassuring() -> None:
    assert (
        state_for(None, filled_quantity=0, submitted_quantity=1, has_broker_order_id=True)
        is ExecutionState.UNKNOWN
    )


# ---------------------------------------------------------------------------
# A cancelled order that partly filled
# ---------------------------------------------------------------------------
def test_a_cancelled_order_keeps_the_position_it_did_fill() -> None:
    """The state and the count together are the whole truth; neither alone is."""
    state = state_for(
        OrderStatus.CANCELLED, filled_quantity=3, submitted_quantity=10, has_broker_order_id=True
    )
    assert state is ExecutionState.CANCELLED


def test_the_report_says_a_partial_position_is_real(
    engine, make_record, make_intent, fake_broker
) -> None:
    from trading_system.execution.report import render_execution

    broker = fake_broker(
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=4,
        average_fill_price=Decimal("6.00"),
        fills=[_fill(4, "6.00")],
    )
    outcome = engine(broker).submit(make_record(quantity=10), make_intent(quantity=10))

    text = render_execution(outcome.record)
    assert "4 of 10" in text
    assert "not a whole one" in text


# ---------------------------------------------------------------------------
# Money on a partial fill
# ---------------------------------------------------------------------------
def test_executed_capital_reflects_what_traded_not_what_was_authorised(
    engine, make_record, make_intent, fake_broker
) -> None:
    broker = fake_broker(
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=4,
        average_fill_price=Decimal("6.00"),
        fills=[_fill(4, "6.00")],
    )

    outcome = engine(broker).submit(make_record(quantity=10), make_intent(quantity=10))

    assert outcome.record.executed_capital == Decimal("24.00")
    assert outcome.record.capital_commitment == make_record().capital_commitment


def test_executed_capital_is_none_until_something_trades(
    engine, make_record, make_intent, fake_broker
) -> None:
    """None, never zero: "nothing has traded" and "it traded for nothing" differ."""
    broker = fake_broker(status=OrderStatus.SUBMITTED)

    outcome = engine(broker).submit(make_record(), make_intent())

    assert outcome.record.executed_capital is None
    assert outcome.record.average_fill_price is None


def test_fills_accumulate_without_duplicating(
    engine, make_record, make_intent, fake_broker
) -> None:
    from trading_system.execution.fill_tracker import event_from_execution_result

    broker = fake_broker(
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=4,
        average_fill_price=Decimal("6.00"),
        fills=[_fill(4, "6.00", index=1)],
    )
    outcome = engine(broker).submit(make_record(quantity=10), make_intent(quantity=10))

    from trading_system.domain.models import ExecutionResult

    second = event_from_execution_result(
        outcome.record,
        ExecutionResult(
            intent_id="intent-0001",
            broker="FAKE",
            broker_order_id="fake-order-1",
            status=OrderStatus.FILLED,
            filled_quantity=10,
            average_fill_price=Decimal("6.01"),
            fills=[_fill(4, "6.00", index=1), _fill(6, "6.02", index=2)],
            submitted_at=NOW,
            last_update_at=NOW,
            trading_mode=outcome.record.trading_mode,
        ),
        sequence=2,
        observed_at=NOW,
        source="FAKE",
    )
    final = outcome.record.with_event(second)

    assert [fill.fill_id for fill in final.fills] == ["fill-1", "fill-2"]
    assert final.filled_quantity == 10
