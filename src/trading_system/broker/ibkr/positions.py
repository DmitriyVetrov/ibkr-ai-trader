"""Translate IBKR position objects into :class:`BrokerPosition`.

Pure functions over duck-typed inputs: they read attributes off whatever IBKR
object is handed to them and never import ``ib_async``. That keeps the mapping
testable with small fakes, without a library or a connection.

Two IBKR shapes are supported:

* ``PortfolioItem`` — includes market price, market value and P&L;
* ``Position`` — quantity and average cost only.

Prefer the former; the latter is the fallback when portfolio subscriptions are
unavailable, and its missing fields stay ``None`` rather than being defaulted
to zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trading_system.broker.ibkr.conversion import (
    to_date,
    to_decimal,
    to_option_right,
    to_security_type,
)
from trading_system.domain.enums import SecurityType
from trading_system.domain.models import BrokerPosition

__all__ = ["IBKR_SOURCE", "to_broker_position", "to_broker_positions"]

IBKR_SOURCE = "IBKR"


def _multiplier(contract: Any) -> int | None:
    """IBKR sends the contract multiplier as a string, and often as ''."""
    raw = getattr(contract, "multiplier", None)
    value = to_decimal(raw)
    if value is None or value <= 0:
        return None
    return int(value)


def to_broker_position(
    item: Any,
    as_of: datetime,
    *,
    account_id: str | None = None,
    source: str = IBKR_SOURCE,
) -> BrokerPosition:
    """Convert one IBKR position/portfolio item.

    Accepts either shape; fields absent from the given shape stay ``None``.
    """
    contract = getattr(item, "contract", None)
    if contract is None:
        raise ValueError("IBKR position has no contract")

    security_type = to_security_type(getattr(contract, "secType", None))
    quantity = to_decimal(getattr(item, "position", None))
    if quantity is None:
        raise ValueError("IBKR position has no quantity")

    account = account_id or getattr(item, "account", None) or ""
    if not account:
        raise ValueError("IBKR position has no account")

    # `position`/`PortfolioItem` differ: only the latter carries valuation.
    return BrokerPosition(
        account_id=str(account),
        symbol=str(getattr(contract, "symbol", "") or "UNKNOWN"),
        security_type=security_type,
        as_of=as_of,
        source=source,
        contract_id=_contract_id(contract),
        local_symbol=_optional_str(getattr(contract, "localSymbol", None)),
        currency=_optional_str(getattr(contract, "currency", None)),
        multiplier=_multiplier(contract),
        quantity=quantity,
        # IBKR's avgCost for an option is the per-contract cost *including* the
        # multiplier (a 5.95 option shows 595.0), not the option's price.
        #
        # PortfolioItem calls it averageCost, Position calls it avgCost. Checked
        # in order rather than with `or`, so a genuine zero is kept instead of
        # falling through to the other field.
        average_cost=_first_present(item, "averageCost", "avgCost"),
        expiration=to_date(getattr(contract, "lastTradeDateOrContractMonth", None)),
        strike=_positive(to_decimal(getattr(contract, "strike", None))),
        right=to_option_right(getattr(contract, "right", None)),
        market_price=to_decimal(getattr(item, "marketPrice", None)),
        market_value=to_decimal(getattr(item, "marketValue", None)),
        unrealized_pnl=to_decimal(getattr(item, "unrealizedPNL", None)),
        realized_pnl=to_decimal(getattr(item, "realizedPNL", None)),
    )


def to_broker_positions(
    items: list[Any],
    as_of: datetime,
    *,
    account_id: str | None = None,
    source: str = IBKR_SOURCE,
) -> list[BrokerPosition]:
    """Convert many, skipping fully closed (zero quantity) rows.

    IBKR keeps reporting a contract with quantity 0 after a position is closed.
    Carrying those forward would inflate the position count and produce phantom
    reconciliation mismatches.
    """
    positions = [
        to_broker_position(item, as_of, account_id=account_id, source=source) for item in items
    ]
    return [p for p in positions if p.quantity != 0]


def _first_present(item: Any, *names: str) -> Any:
    """Return the first attribute that is actually present and convertible.

    Distinguishes "field absent" from "field is zero": a zero average cost is
    real data and must not fall through to the next candidate name.
    """
    for name in names:
        if hasattr(item, name):
            converted = to_decimal(getattr(item, name))
            if converted is not None:
                return converted
    return None


def _contract_id(contract: Any) -> int | None:
    value = getattr(contract, "conId", None)
    if isinstance(value, int) and value > 0:
        return value
    return None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _positive(value: Any) -> Any:
    """IBKR reports strike 0.0 for non-option contracts; the model wants ``None``."""
    if value is None or value <= 0:
        return None
    return value


def option_positions(positions: list[BrokerPosition]) -> list[BrokerPosition]:
    """Only the option positions, for callers that manage option strategies."""
    return [p for p in positions if p.security_type is SecurityType.OPTION]
