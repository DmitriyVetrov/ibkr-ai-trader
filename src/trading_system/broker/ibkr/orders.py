"""Translate IBKR ``Trade``/``Order`` objects into :class:`BrokerOrder`.

An order is not a position, and a submitted order is not a filled order. Both
distinctions are preserved here: the filled quantity comes from the broker's
order status, never inferred from the fact that an order exists.

Pure functions over duck-typed inputs; nothing here imports ``ib_async``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_system.broker.ibkr.conversion import (
    to_decimal,
    to_order_side,
    to_order_status,
    to_security_type,
    to_utc,
)
from trading_system.domain.enums import OrderStatus, TradingMode
from trading_system.domain.models import BrokerOrder, ExecutionResult, Fill

__all__ = [
    "IBKR_SOURCE",
    "to_broker_order",
    "to_broker_orders",
    "to_execution_result",
]

IBKR_SOURCE = "IBKR"

#: IBKR order statuses that mean the order is still working.
_LIVE_STATUSES = frozenset(
    {OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}
)


def to_broker_order(
    trade: Any,
    as_of: datetime,
    *,
    account_id: str | None = None,
    source: str = IBKR_SOURCE,
) -> BrokerOrder:
    """Convert one IBKR ``Trade`` (contract + order + orderStatus)."""
    contract = getattr(trade, "contract", None)
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    if contract is None or order is None:
        raise ValueError("IBKR trade is missing its contract or order")

    filled = to_decimal(getattr(status, "filled", None)) or Decimal("0")
    remaining = to_decimal(getattr(status, "remaining", None))
    quantity = to_decimal(getattr(order, "totalQuantity", None))
    if quantity is None or quantity <= 0:
        # Fall back to filled + remaining rather than rejecting the order: the
        # broker knows about it, so we must represent it.
        quantity = filled + (remaining or Decimal("0"))
    if quantity <= 0:
        raise ValueError("IBKR order has no usable quantity")

    side = to_order_side(getattr(order, "action", None))
    if side is None:
        action = getattr(order, "action", None)
        raise ValueError(f"IBKR order has an unrecognised action: {action!r}")

    return BrokerOrder(
        broker_order_id=_order_id(order, status),
        account_id=_optional_str(getattr(order, "account", None)) or account_id,
        as_of=as_of,
        source=source,
        client_order_id=_optional_str(str(getattr(order, "clientId", "") or "")),
        perm_id=_positive_int(getattr(order, "permId", None)),
        contract_id=_positive_int(getattr(contract, "conId", None)),
        symbol=str(getattr(contract, "symbol", "") or "UNKNOWN"),
        security_type=to_security_type(getattr(contract, "secType", None)),
        local_symbol=_optional_str(getattr(contract, "localSymbol", None)),
        side=side,
        quantity=quantity,
        order_type=str(getattr(order, "orderType", "") or "UNKNOWN"),
        limit_price=to_decimal(getattr(order, "lmtPrice", None)),
        stop_price=to_decimal(getattr(order, "auxPrice", None)),
        time_in_force=_optional_str(getattr(order, "tif", None)),
        status=to_order_status(getattr(status, "status", None), filled),
        filled_quantity=filled,
        remaining_quantity=remaining,
        average_fill_price=_positive(to_decimal(getattr(status, "avgFillPrice", None))),
        submitted_at=_log_time(trade, first=True),
        updated_at=_log_time(trade, first=False) or as_of,
    )


def to_broker_orders(
    trades: list[Any],
    as_of: datetime,
    *,
    account_id: str | None = None,
    source: str = IBKR_SOURCE,
) -> list[BrokerOrder]:
    return [to_broker_order(trade, as_of, account_id=account_id, source=source) for trade in trades]


def open_orders(orders: list[BrokerOrder]) -> list[BrokerOrder]:
    """Only orders that can still fill."""
    return [o for o in orders if o.status in _LIVE_STATUSES]


def to_execution_result(
    trade: Any,
    *,
    intent_id: str,
    as_of: datetime,
    trading_mode: TradingMode,
    orders_submitted: int = 1,
    source: str = IBKR_SOURCE,
) -> ExecutionResult:
    """Convert what IBKR returned from a submission into an execution result.

    Milestone 8's most safety-critical translation, and every line of it is
    about not claiming more than IBKR said:

    * the fill count comes from ``orderStatus.filled`` — never from the
      quantity we sent, which is what we *asked* for and says nothing about
      what happened;
    * an average fill price of zero is ``None``. IBKR reports zero for "no
      fills yet", and a zero price on a record means somebody bought something
      for nothing;
    * ``orders_submitted`` is passed in rather than assumed, because the count
      belongs to the broker object that did the submitting.

    A trade with no identifier is returned with ``broker_order_id=None``, which
    the execution layer treats as an uncertain submission rather than a tracked
    one — an order we cannot name is one we cannot cancel.
    """
    order = getattr(trade, "order", None)
    status_object = getattr(trade, "orderStatus", None)
    if order is None:
        raise ValueError("IBKR trade is missing its order")

    filled = to_decimal(getattr(status_object, "filled", None)) or Decimal("0")
    status = to_order_status(getattr(status_object, "status", None), filled)
    average = _positive(to_decimal(getattr(status_object, "avgFillPrice", None)))

    return ExecutionResult(
        intent_id=intent_id,
        broker=source,
        broker_order_id=_optional_order_id(order, status_object),
        status=status,
        orders_submitted=orders_submitted,
        filled_quantity=int(filled),
        average_fill_price=average,
        fills=_fills_of(trade),
        submitted_at=_log_time(trade, first=True) or as_of,
        last_update_at=_log_time(trade, first=False) or as_of,
        message=_message_of(trade, status_object),
        trading_mode=trading_mode,
    )


def _fills_of(trade: Any) -> list[Fill]:
    """Every execution report attached to the trade, translated.

    A fill without a price or a quantity is skipped rather than defaulted:
    a fill of zero contracts at zero euros is not a conservative record of a
    fill, it is a false one.
    """
    fills: list[Fill] = []
    for index, entry in enumerate(getattr(trade, "fills", None) or []):
        execution = getattr(entry, "execution", None)
        if execution is None:
            continue
        quantity = to_decimal(getattr(execution, "shares", None))
        price = to_decimal(getattr(execution, "price", None))
        executed_at = to_utc(getattr(execution, "time", None))
        if quantity is None or quantity <= 0 or price is None or price <= 0 or executed_at is None:
            continue
        commission = Decimal("0")
        report = getattr(entry, "commissionReport", None)
        if report is not None:
            commission = to_decimal(getattr(report, "commission", None)) or Decimal("0")
        fills.append(
            Fill(
                fill_id=str(getattr(execution, "execId", None) or f"fill-{index}"),
                leg_index=0,
                quantity=int(quantity),
                price=price,
                commission=max(commission, Decimal("0")),
                filled_at=executed_at,
            )
        )
    return fills


def _optional_order_id(order: Any, status: Any) -> str | None:
    """The order's identifier, or ``None``. Never invented."""
    try:
        return _order_id(order, status)
    except ValueError:
        return None


def _message_of(trade: Any, status: Any) -> str | None:
    """The broker's own words about this order, if it said any."""
    for entry in reversed(getattr(trade, "log", None) or []):
        message = getattr(entry, "message", None)
        if isinstance(message, str) and message.strip():
            return message.strip()
    text = getattr(status, "whyHeld", None)
    return text.strip() if isinstance(text, str) and text.strip() else None


def _order_id(order: Any, status: Any) -> str:
    """Prefer permId: it is stable across sessions, unlike orderId."""
    perm_id = _positive_int(getattr(order, "permId", None)) or _positive_int(
        getattr(status, "permId", None)
    )
    if perm_id is not None:
        return str(perm_id)
    order_id = _positive_int(getattr(order, "orderId", None)) or _positive_int(
        getattr(status, "orderId", None)
    )
    if order_id is not None:
        return str(order_id)
    raise ValueError("IBKR order has neither permId nor orderId")


def _log_time(trade: Any, *, first: bool) -> datetime | None:
    entries = getattr(trade, "log", None)
    if not entries:
        return None
    entry = entries[0] if first else entries[-1]
    return to_utc(getattr(entry, "time", None))


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _positive(value: Any) -> Any:
    if value is None or value <= 0:
        return None
    return value
