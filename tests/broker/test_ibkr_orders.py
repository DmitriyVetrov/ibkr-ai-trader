"""Translating IBKR orders into domain models.

The distinction this suite protects: a submitted order is not a filled order,
and a partly filled order is neither.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.broker.ibkr.orders import open_orders, to_broker_order, to_broker_orders
from trading_system.domain.enums import OrderSide, OrderStatus, SecurityType

from .conftest import BROKER_NOW, make_stock_contract, make_trade


@pytest.mark.unit
def test_order_is_fully_translated() -> None:
    order = to_broker_order(make_trade(), BROKER_NOW)

    assert order.symbol == "NVDA"
    assert order.security_type is SecurityType.OPTION
    assert order.side is OrderSide.BUY
    assert order.quantity == Decimal("3")
    assert order.order_type == "LMT"
    assert order.limit_price == Decimal("5.8")
    assert order.time_in_force == "DAY"
    assert order.source == "IBKR"


@pytest.mark.unit
def test_perm_id_is_preferred_as_the_order_identity() -> None:
    """permId is stable across sessions; orderId is not."""
    order = to_broker_order(make_trade(order_id=5, perm_id=900001), BROKER_NOW)
    assert order.broker_order_id == "900001"
    assert order.perm_id == 900001


@pytest.mark.unit
def test_order_id_is_used_when_perm_id_is_absent() -> None:
    trade = make_trade(perm_id=0)
    trade.orderStatus.permId = 0
    order = to_broker_order(trade, BROKER_NOW)
    assert order.broker_order_id == "5"


@pytest.mark.unit
def test_order_without_any_identifier_is_rejected() -> None:
    trade = make_trade(order_id=0, perm_id=0)
    trade.orderStatus.permId = 0
    trade.orderStatus.orderId = 0
    with pytest.raises(ValueError, match="permId nor orderId"):
        to_broker_order(trade, BROKER_NOW)


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("ibkr_status", "expected"),
    [
        ("PendingSubmit", OrderStatus.PENDING_SUBMIT),
        ("ApiPending", OrderStatus.PENDING_SUBMIT),
        ("PreSubmitted", OrderStatus.SUBMITTED),
        ("Submitted", OrderStatus.SUBMITTED),
        ("Filled", OrderStatus.FILLED),
        ("Cancelled", OrderStatus.CANCELLED),
        ("ApiCancelled", OrderStatus.CANCELLED),
    ],
)
def test_status_mapping(ibkr_status: str, expected: OrderStatus) -> None:
    order = to_broker_order(make_trade(status=ibkr_status), BROKER_NOW)
    assert order.status is expected


@pytest.mark.unit
def test_partial_fill_is_derived_from_the_fill_count() -> None:
    """IBKR reports a partly filled order as Submitted with filled > 0."""
    order = to_broker_order(
        make_trade(status="Submitted", total_quantity=3.0, filled=1.0, remaining=2.0),
        BROKER_NOW,
    )
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("1")
    assert order.remaining_quantity == Decimal("2")
    assert order.quantity == Decimal("3")


@pytest.mark.unit
def test_pending_cancel_is_still_live() -> None:
    """A requested cancel is not a confirmed one; the order can still fill."""
    order = to_broker_order(make_trade(status="PendingCancel"), BROKER_NOW)
    assert order.status is OrderStatus.SUBMITTED


@pytest.mark.unit
def test_inactive_is_treated_as_rejected() -> None:
    """Fail safe: never assume an inactive order is working."""
    order = to_broker_order(make_trade(status="Inactive"), BROKER_NOW)
    assert order.status is OrderStatus.REJECTED


@pytest.mark.unit
def test_unknown_status_does_not_become_filled() -> None:
    order = to_broker_order(make_trade(status="SomethingNew"), BROKER_NOW)
    assert order.status is not OrderStatus.FILLED


@pytest.mark.unit
def test_submitted_order_is_not_reported_as_filled() -> None:
    order = to_broker_order(make_trade(status="Submitted", filled=0.0), BROKER_NOW)
    assert order.status is OrderStatus.SUBMITTED
    assert order.filled_quantity == Decimal("0")
    assert order.average_fill_price is None


# ---------------------------------------------------------------------------
# Field handling
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_unset_stop_price_is_none() -> None:
    """auxPrice arrives as IBKR's unset-double sentinel, not as None."""
    order = to_broker_order(make_trade(), BROKER_NOW)
    assert order.stop_price is None


@pytest.mark.unit
def test_sell_side_is_translated() -> None:
    order = to_broker_order(make_trade(action="SELL"), BROKER_NOW)
    assert order.side is OrderSide.SELL


@pytest.mark.unit
def test_unrecognised_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="unrecognised action"):
        to_broker_order(make_trade(action="WHAT"), BROKER_NOW)


@pytest.mark.unit
def test_quantity_falls_back_to_filled_plus_remaining() -> None:
    trade = make_trade(total_quantity=0.0, filled=1.0, remaining=2.0)
    order = to_broker_order(trade, BROKER_NOW)
    assert order.quantity == Decimal("3")


@pytest.mark.unit
def test_stock_order_is_supported() -> None:
    order = to_broker_order(make_trade(contract=make_stock_contract()), BROKER_NOW)
    assert order.security_type is SecurityType.STOCK


@pytest.mark.unit
def test_translating_many_orders() -> None:
    orders = to_broker_orders([make_trade(), make_trade(perm_id=900002)], BROKER_NOW)
    assert {o.broker_order_id for o in orders} == {"900001", "900002"}


@pytest.mark.unit
def test_open_orders_filter_excludes_finished_orders() -> None:
    orders = to_broker_orders(
        [
            make_trade(status="Submitted", perm_id=1),
            make_trade(status="Filled", perm_id=2),
            make_trade(status="Cancelled", perm_id=3),
            make_trade(status="Submitted", filled=1.0, perm_id=4),
        ],
        BROKER_NOW,
    )
    assert {o.broker_order_id for o in open_orders(orders)} == {"1", "4"}
