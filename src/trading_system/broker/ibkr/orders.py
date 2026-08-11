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
from trading_system.domain.enums import OrderStatus
from trading_system.domain.models import BrokerOrder

__all__ = ["IBKR_SOURCE", "to_broker_order", "to_broker_orders"]

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
