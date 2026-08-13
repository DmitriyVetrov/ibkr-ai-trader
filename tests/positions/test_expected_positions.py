"""Projecting confirmed fills onto the internal ledger (brief sections 14-17, 45, 75).

The rule this file exists to pin: **only a confirmed broker fill establishes a
position.** An allocation, a submitted order, an acknowledgement and an
``UNKNOWN`` submission all establish nothing, and a partial fill establishes
exactly what filled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.positions.factories import (
    CALL_KEY,
    EXPIRATION,
    MASKED,
    NOW,
    PUT_KEY,
    broker_execution,
    execution_record,
    option_position,
    straddle_legs,
)
from trading_system.domain.enums import (
    BrokerReadStatus,
    ExecutionState,
    OptionRight,
    OrderSide,
    StrategyType,
    StructureStatus,
    TradingMode,
)
from trading_system.positions.expected import (
    expected_from_execution,
    expected_from_fills,
    project_expected_positions,
    strategy_position_for,
)
from trading_system.positions.fills import ContractTerms, to_observed_fill
from trading_system.positions.snapshot import build_position_snapshot, unavailable_snapshot

pytestmark = pytest.mark.unit

TERMS = ContractTerms(
    expiration=EXPIRATION, strike=Decimal("180.00"), right=OptionRight.CALL, multiplier=100
)


def _fill(
    *,
    execution_id: str = "exec-1",
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = Decimal("1"),
    price: Decimal = Decimal("5.95"),
    at: datetime = NOW,
    linked: str | None = "execution-1",
    commission: Decimal | None = Decimal("1.30"),
):
    return to_observed_fill(
        broker_execution(
            execution_id=execution_id,
            side=side,
            quantity=quantity,
            price=price,
            executed_at=at,
            commission=commission,
        ),
        observed_at=NOW,
        account_reference=MASKED,
        terms=TERMS,
        execution_id=linked,
    )


# ---------------------------------------------------------------------------
# The arithmetic (brief section 75)
# ---------------------------------------------------------------------------
def test_buy_one_gives_one() -> None:
    [position] = expected_from_fills([_fill()], as_of=NOW, account_reference=MASKED)
    assert position.quantity == Decimal("1")


def test_buy_two_gives_two() -> None:
    fills = [
        _fill(execution_id="e1", quantity=Decimal("1")),
        _fill(execution_id="e2", quantity=Decimal("1")),
    ]
    [position] = expected_from_fills(fills, as_of=NOW, account_reference=MASKED)
    assert position.quantity == Decimal("2")


def test_buy_two_then_sell_one_gives_one() -> None:
    fills = [
        _fill(execution_id="e1", quantity=Decimal("2")),
        _fill(execution_id="e2", side=OrderSide.SELL, quantity=Decimal("1")),
    ]
    [position] = expected_from_fills(fills, as_of=NOW, account_reference=MASKED)
    assert position.quantity == Decimal("1")
    assert position.bought_quantity == Decimal("2")
    assert position.sold_quantity == Decimal("1")


def test_buy_one_then_sell_one_gives_zero_and_is_still_a_record() -> None:
    """A closed position is zero, which is different from having no record."""
    fills = [
        _fill(execution_id="e1", quantity=Decimal("1")),
        _fill(execution_id="e2", side=OrderSide.SELL, quantity=Decimal("1")),
    ]
    [position] = expected_from_fills(fills, as_of=NOW, account_reference=MASKED)
    assert position.quantity == Decimal("0")
    assert position.is_open is False
    assert len(position.fill_ids) == 2


def test_direction_comes_from_the_fill_not_from_the_strategy() -> None:
    """The four shipped strategies are long today. The arithmetic is not."""
    [position] = expected_from_fills(
        [_fill(side=OrderSide.SELL, quantity=Decimal("3"))],
        as_of=NOW,
        account_reference=MASKED,
    )
    assert position.quantity == Decimal("-3")


# ---------------------------------------------------------------------------
# Prices and units
# ---------------------------------------------------------------------------
def test_the_average_price_is_volume_weighted_in_quoted_terms() -> None:
    fills = [
        _fill(execution_id="e1", quantity=Decimal("1"), price=Decimal("6.00")),
        _fill(execution_id="e2", quantity=Decimal("3"), price=Decimal("5.00")),
    ]
    [position] = expected_from_fills(fills, as_of=NOW, account_reference=MASKED)
    assert position.average_price == Decimal("5.25")


def test_the_average_cost_carries_the_multiplier() -> None:
    [position] = expected_from_fills(
        [_fill(price=Decimal("6.05"))], as_of=NOW, account_reference=MASKED
    )
    assert position.average_price == Decimal("6.05")
    assert position.average_cost == Decimal("605.00")


def test_a_commission_missing_on_any_fill_marks_the_total_incomplete() -> None:
    fills = [
        _fill(execution_id="e1", commission=Decimal("1.30")),
        _fill(execution_id="e2", commission=None),
    ]
    [position] = expected_from_fills(fills, as_of=NOW, account_reference=MASKED)
    assert position.commission_complete is False


def test_a_closed_position_reports_no_entry_price() -> None:
    """What it cost and what it returned is a profit-and-loss question."""
    fills = [
        _fill(execution_id="e1", quantity=Decimal("1")),
        _fill(execution_id="e2", side=OrderSide.SELL, quantity=Decimal("1")),
    ]
    [position] = expected_from_fills(fills, as_of=NOW, account_reference=MASKED)
    assert position.average_price is None
    assert position.total_cost is None


# ---------------------------------------------------------------------------
# Only a fill makes a position (brief section 15)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "state",
    [
        ExecutionState.CREATED,
        ExecutionState.VALIDATED,
        ExecutionState.SUBMISSION_PENDING,
        ExecutionState.SUBMITTED,
        ExecutionState.UNKNOWN,
        ExecutionState.REJECTED,
        ExecutionState.FAILED,
    ],
)
def test_an_execution_that_never_filled_establishes_no_position(state) -> None:
    assert (
        expected_from_execution(
            execution_record(state=state, filled_quantity=0), as_of=NOW, account_reference=MASKED
        )
        == []
    )


def test_an_unknown_execution_establishes_no_position_even_with_a_broker_order() -> None:
    """The invariant carried forward from Milestone 8: UNKNOWN is not filled."""
    record = execution_record(state=ExecutionState.UNKNOWN, filled_quantity=0)
    projection = project_expected_positions(
        fills=[], executions=[record], as_of=NOW, account_reference=MASKED
    )
    assert projection.positions == ()
    assert record.execution_id in projection.contributed_nothing


def test_a_filled_execution_establishes_exactly_what_filled() -> None:
    [position] = expected_from_execution(
        execution_record(quantity=10, filled_quantity=10), as_of=NOW, account_reference=MASKED
    )
    assert position.quantity == Decimal("10")


# ---------------------------------------------------------------------------
# Partial fills (brief section 16)
# ---------------------------------------------------------------------------
def test_a_partial_fill_establishes_the_filled_quantity_not_the_ordered_one() -> None:
    [position] = expected_from_execution(
        execution_record(state=ExecutionState.PARTIALLY_FILLED, quantity=10, filled_quantity=4),
        as_of=NOW,
        account_reference=MASKED,
    )
    assert position.quantity == Decimal("4")


def test_a_later_complete_fill_establishes_the_whole_quantity() -> None:
    [position] = expected_from_execution(
        execution_record(state=ExecutionState.FILLED, quantity=10, filled_quantity=10),
        as_of=NOW,
        account_reference=MASKED,
    )
    assert position.quantity == Decimal("10")


def test_the_remaining_quantity_is_never_inferred() -> None:
    """Four of ten is four. Nothing here rounds it up to what was ordered."""
    [position] = expected_from_execution(
        execution_record(state=ExecutionState.PARTIALLY_FILLED, quantity=10, filled_quantity=4),
        as_of=NOW,
        account_reference=MASKED,
    )
    assert position.quantity != Decimal("10")


# ---------------------------------------------------------------------------
# Multi-leg (brief section 17)
# ---------------------------------------------------------------------------
def test_a_straddle_produces_one_expected_position_per_leg() -> None:
    positions = expected_from_execution(
        execution_record(
            legs=straddle_legs(), strategy=StrategyType.LONG_STRADDLE, quantity=1, filled_quantity=1
        ),
        as_of=NOW,
        account_reference=MASKED,
    )
    assert {position.key for position in positions} == {CALL_KEY, PUT_KEY}
    assert all(position.quantity == Decimal("1") for position in positions)


def test_a_combos_leg_price_is_left_unknown_rather_than_split() -> None:
    """A combo's average fill price is the price of the structure, not a leg."""
    positions = expected_from_execution(
        execution_record(
            legs=straddle_legs(), strategy=StrategyType.LONG_STRADDLE, quantity=1, filled_quantity=1
        ),
        as_of=NOW,
        account_reference=MASKED,
    )
    assert all(position.average_price is None for position in positions)


def test_a_single_leg_orders_price_is_the_legs_price() -> None:
    [position] = expected_from_execution(
        execution_record(average_fill_price=Decimal("6.05")),
        as_of=NOW,
        account_reference=MASKED,
    )
    assert position.average_price == Decimal("6.05")


def test_a_complete_straddle_is_reported_complete() -> None:
    record = execution_record(
        legs=straddle_legs(), strategy=StrategyType.LONG_STRADDLE, quantity=1, filled_quantity=1
    )
    snapshot = _snapshot_with(call=Decimal("1"), put=Decimal("1"))
    structure = strategy_position_for(record, snapshot=snapshot, as_of=NOW, account=MASKED)
    assert structure is not None
    assert structure.status is StructureStatus.COMPLETE


def test_a_half_held_straddle_is_partial_not_complete_and_not_missing() -> None:
    """The case that matters: a naked long call where a straddle was authorised."""
    record = execution_record(
        legs=straddle_legs(), strategy=StrategyType.LONG_STRADDLE, quantity=1, filled_quantity=1
    )
    snapshot = _snapshot_with(call=Decimal("1"), put=None)
    structure = strategy_position_for(record, snapshot=snapshot, as_of=NOW, account=MASKED)
    assert structure is not None
    assert structure.status is StructureStatus.PARTIAL


def test_a_structure_the_broker_does_not_hold_at_all_is_missing() -> None:
    record = execution_record(
        legs=straddle_legs(), strategy=StrategyType.LONG_STRADDLE, quantity=1, filled_quantity=1
    )
    snapshot = _snapshot_with(call=None, put=None)
    structure = strategy_position_for(record, snapshot=snapshot, as_of=NOW, account=MASKED)
    assert structure is not None
    assert structure.status is StructureStatus.MISSING


def test_an_unreadable_broker_makes_a_structure_unknown_not_missing() -> None:
    """'We could not look' and 'the broker holds none' are different findings."""
    record = execution_record(
        legs=straddle_legs(), strategy=StrategyType.LONG_STRADDLE, quantity=1, filled_quantity=1
    )
    snapshot = unavailable_snapshot(
        broker="SIMULATOR",
        account_id="DU1234567",
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
        status=BrokerReadStatus.UNAVAILABLE,
        detail="connection refused",
    )
    structure = strategy_position_for(record, snapshot=snapshot, as_of=NOW, account=MASKED)
    assert structure is not None
    assert structure.status is StructureStatus.UNKNOWN
    assert all(leg.observed_quantity is None for leg in structure.legs)


def test_two_broker_positions_are_never_collapsed_into_one_fake_contract() -> None:
    record = execution_record(
        legs=straddle_legs(), strategy=StrategyType.LONG_STRADDLE, quantity=1, filled_quantity=1
    )
    snapshot = _snapshot_with(call=Decimal("1"), put=Decimal("1"))
    structure = strategy_position_for(record, snapshot=snapshot, as_of=NOW, account=MASKED)
    assert structure is not None
    assert len(structure.legs) == 2
    assert {leg.key for leg in structure.legs} == {CALL_KEY, PUT_KEY}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def test_recorded_fills_are_preferred_over_an_executions_own_counts() -> None:
    record = execution_record(quantity=2, filled_quantity=2)
    projection = project_expected_positions(
        fills=[_fill(quantity=Decimal("2"), linked=record.execution_id)],
        executions=[record],
        as_of=NOW,
        account_reference=MASKED,
    )
    assert projection.covered_by_fills == (record.execution_id,)
    assert projection.covered_by_execution_record == ()
    [position] = projection.positions
    assert position.quantity == Decimal("2")


def test_an_execution_with_no_recorded_fills_falls_back_to_its_own_counts() -> None:
    """IBKR's execution list is session-scoped; last week's fills are not in it."""
    record = execution_record(quantity=2, filled_quantity=2)
    projection = project_expected_positions(
        fills=[], executions=[record], as_of=NOW, account_reference=MASKED
    )
    assert projection.covered_by_execution_record == (record.execution_id,)
    [position] = projection.positions
    assert position.quantity == Decimal("2")


def test_an_execution_is_never_counted_twice() -> None:
    record = execution_record(quantity=2, filled_quantity=2)
    projection = project_expected_positions(
        fills=[_fill(quantity=Decimal("2"), linked=record.execution_id)],
        executions=[record],
        as_of=NOW,
        account_reference=MASKED,
    )
    [position] = projection.positions
    assert position.quantity == Decimal("2")


def test_a_fill_no_execution_of_ours_explains_does_not_enter_the_internal_ledger() -> None:
    """Otherwise an orphan broker position would silently agree with itself."""
    projection = project_expected_positions(
        fills=[_fill(linked=None)], executions=[], as_of=NOW, account_reference=MASKED
    )
    assert projection.positions == ()


def test_the_projection_is_deterministic() -> None:
    record = execution_record(quantity=2, filled_quantity=2)
    fills = [
        _fill(execution_id="e1", quantity=Decimal("1"), linked=record.execution_id),
        _fill(
            execution_id="e2",
            quantity=Decimal("1"),
            at=datetime(2026, 8, 10, 14, 45, tzinfo=UTC),
            linked=record.execution_id,
        ),
    ]
    first = project_expected_positions(
        fills=fills, executions=[record], as_of=NOW, account_reference=MASKED
    )
    second = project_expected_positions(
        fills=list(reversed(fills)), executions=[record], as_of=NOW, account_reference=MASKED
    )
    assert [p.model_dump() for p in first.positions] == [p.model_dump() for p in second.positions]


def _snapshot_with(*, call: Decimal | None, put: Decimal | None):
    positions = []
    if call is not None:
        positions.append(option_position(quantity=call, right=OptionRight.CALL))
    if put is not None:
        positions.append(
            option_position(
                contract_id=100002,
                quantity=put,
                right=OptionRight.PUT,
                strike=Decimal("180.00"),
            )
        )
    if not positions:
        return build_position_snapshot(
            [],
            broker="SIMULATOR",
            account_id="DU1234567",
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
        )
    return build_position_snapshot(
        positions,
        broker="SIMULATOR",
        account_id="DU1234567",
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
    )
