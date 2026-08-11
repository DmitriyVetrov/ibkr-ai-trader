"""Translate IBKR ``Fill`` objects into :class:`BrokerExecution`.

Executions are the ground truth for what was actually traded. A position is
reconstructed from these, never from the orders that were submitted — an order
that was sent is not evidence that it filled.

Pure functions over duck-typed inputs; nothing here imports ``ib_async``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trading_system.broker.ibkr.conversion import (
    to_decimal,
    to_order_side,
    to_security_type,
    to_utc,
)
from trading_system.domain.models import BrokerExecution

__all__ = ["IBKR_SOURCE", "to_broker_execution", "to_broker_executions"]

IBKR_SOURCE = "IBKR"


def to_broker_execution(
    fill: Any,
    as_of: datetime,
    *,
    source: str = IBKR_SOURCE,
) -> BrokerExecution:
    """Convert one IBKR ``Fill`` (contract + execution + commissionReport)."""
    contract = getattr(fill, "contract", None)
    execution = getattr(fill, "execution", None)
    if contract is None or execution is None:
        raise ValueError("IBKR fill is missing its contract or execution")

    execution_id = _optional_str(getattr(execution, "execId", None))
    if execution_id is None:
        raise ValueError("IBKR execution has no execId")

    quantity = to_decimal(getattr(execution, "shares", None))
    price = to_decimal(getattr(execution, "price", None))
    if quantity is None or quantity <= 0:
        raise ValueError(f"IBKR execution {execution_id} has no usable quantity")
    if price is None or price <= 0:
        raise ValueError(f"IBKR execution {execution_id} has no usable price")

    side = to_order_side(getattr(execution, "side", None))
    if side is None:
        raise ValueError(
            f"IBKR execution {execution_id} has an unrecognised side: "
            f"{getattr(execution, 'side', None)!r}"
        )

    executed_at = to_utc(getattr(execution, "time", None)) or to_utc(getattr(fill, "time", None))
    if executed_at is None:
        raise ValueError(f"IBKR execution {execution_id} has no timestamp")

    commission_report = getattr(fill, "commissionReport", None)

    return BrokerExecution(
        execution_id=execution_id,
        broker_order_id=_broker_order_id(execution),
        account_id=_optional_str(getattr(execution, "acctNumber", None)),
        as_of=as_of,
        source=source,
        contract_id=_positive_int(getattr(contract, "conId", None)),
        symbol=str(getattr(contract, "symbol", "") or "UNKNOWN"),
        security_type=to_security_type(getattr(contract, "secType", None)),
        side=side,
        quantity=quantity,
        price=price,
        executed_at=executed_at,
        commission=to_decimal(getattr(commission_report, "commission", None)),
        currency=_optional_str(getattr(commission_report, "currency", None)),
    )


def to_broker_executions(
    fills: list[Any],
    as_of: datetime,
    *,
    source: str = IBKR_SOURCE,
) -> list[BrokerExecution]:
    return [to_broker_execution(fill, as_of, source=source) for fill in fills]


def _broker_order_id(execution: Any) -> str | None:
    """permId first: it is stable across sessions, unlike orderId."""
    perm_id = _positive_int(getattr(execution, "permId", None))
    if perm_id is not None:
        return str(perm_id)
    order_id = _positive_int(getattr(execution, "orderId", None))
    return str(order_id) if order_id is not None else None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None
