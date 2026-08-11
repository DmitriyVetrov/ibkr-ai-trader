"""Translate IBKR market data and option chains into domain snapshots.

The rule this module exists to enforce: **no invented prices**. IBKR signals a
missing quote with ``NaN``, and returns ``NaN`` liberally — outside market
hours, without a market-data subscription, or for an unqualified contract. A
snapshot with no usable field is reported as ``UNAVAILABLE``, never as zero and
never filled in from the previous close of something else.

Pure functions over duck-typed inputs; nothing here imports ``ib_async``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_system.broker.ibkr.conversion import (
    to_date,
    to_decimal,
    to_security_type,
    to_utc,
)
from trading_system.domain.enums import (
    DataQuality,
    MarketDataOrigin,
    OptionRight,
    SecurityType,
)
from trading_system.domain.models import MarketDataSnapshot, OptionChainSnapshot

__all__ = [
    "IBKR_SOURCE",
    "market_data_origin",
    "to_market_data_snapshot",
    "to_option_chain_snapshot",
    "unavailable_snapshot",
]

IBKR_SOURCE = "IBKR"

#: IBKR ``marketDataType`` codes as returned on a Ticker.
_ORIGINS = {
    1: MarketDataOrigin.BROKER_REALTIME,
    2: MarketDataOrigin.BROKER_FROZEN,
    3: MarketDataOrigin.BROKER_DELAYED,
    4: MarketDataOrigin.BROKER_FROZEN,
}


def market_data_origin(market_data_type: Any) -> MarketDataOrigin:
    """Map IBKR's market-data type code.

    An unrecognised code is reported as ``BROKER_DELAYED`` rather than
    ``BROKER_REALTIME``: if we cannot prove data is live, we must not claim it
    is.
    """
    if isinstance(market_data_type, int) and market_data_type in _ORIGINS:
        return _ORIGINS[market_data_type]
    return MarketDataOrigin.BROKER_DELAYED


def unavailable_snapshot(
    symbol: str,
    as_of: datetime,
    security_type: SecurityType = SecurityType.STOCK,
    *,
    source: str = IBKR_SOURCE,
    contract_id: int | None = None,
) -> MarketDataSnapshot:
    """A snapshot that explicitly carries no prices."""
    return MarketDataSnapshot(
        symbol=symbol,
        security_type=security_type,
        as_of=as_of,
        source=source,
        origin=MarketDataOrigin.UNAVAILABLE,
        data_quality=DataQuality.UNUSABLE,
        contract_id=contract_id,
    )


def to_market_data_snapshot(
    ticker: Any,
    as_of: datetime,
    *,
    source: str = IBKR_SOURCE,
    fallback_symbol: str | None = None,
) -> MarketDataSnapshot:
    """Convert an IBKR ``Ticker``.

    Returns an ``UNAVAILABLE`` snapshot when every price field is missing,
    rather than a snapshot of zeros.
    """
    contract = getattr(ticker, "contract", None)
    symbol = str(getattr(contract, "symbol", "") or fallback_symbol or "UNKNOWN")
    security_type = to_security_type(getattr(contract, "secType", None))
    contract_id = getattr(contract, "conId", None)
    contract_id = contract_id if isinstance(contract_id, int) and contract_id > 0 else None

    bid = _tradeable(to_decimal(getattr(ticker, "bid", None)))
    ask = _tradeable(to_decimal(getattr(ticker, "ask", None)))
    last = _tradeable(to_decimal(getattr(ticker, "last", None)))
    close = _tradeable(to_decimal(getattr(ticker, "close", None)))
    volume = to_decimal(getattr(ticker, "volume", None))

    if bid is None and ask is None and last is None and close is None:
        return unavailable_snapshot(
            symbol, as_of, security_type, source=source, contract_id=contract_id
        )

    quote_time = to_utc(getattr(ticker, "time", None)) or as_of
    origin = market_data_origin(getattr(ticker, "marketDataType", None))

    # A quote with no live bid/ask is stale — only the previous close survived.
    quality = DataQuality.OK if (bid is not None and ask is not None) else DataQuality.DEGRADED
    if bid is None and ask is None and last is None:
        quality = DataQuality.STALE

    return MarketDataSnapshot(
        symbol=symbol,
        security_type=security_type,
        as_of=quote_time,
        source=source,
        origin=origin,
        data_quality=quality,
        contract_id=contract_id,
        currency=_optional_str(getattr(contract, "currency", None)),
        bid=bid,
        ask=ask,
        last=last,
        close=close,
        volume=volume if volume is None or volume >= 0 else None,
    )


def to_option_chain_snapshot(
    chains: list[Any],
    underlying: str,
    as_of: datetime,
    *,
    source: str = IBKR_SOURCE,
    underlying_contract_id: int | None = None,
    preferred_exchange: str = "SMART",
) -> OptionChainSnapshot:
    """Merge IBKR ``OptionChain`` rows into one normalised snapshot.

    IBKR returns one row per exchange/trading-class combination. The preferred
    exchange is used when present; otherwise the first row is taken, so the
    result is deterministic rather than dependent on IBKR's ordering.
    """
    if not chains:
        raise ValueError(f"no option chain rows returned for {underlying}")

    chosen = next(
        (c for c in chains if getattr(c, "exchange", None) == preferred_exchange),
        None,
    ) or min(chains, key=lambda c: str(getattr(c, "exchange", "")))

    expirations = sorted(
        {
            expiry
            for raw in getattr(chosen, "expirations", []) or []
            if (expiry := to_date(raw)) is not None
        }
    )
    strikes = sorted(
        {
            strike
            for raw in getattr(chosen, "strikes", []) or []
            if (strike := to_decimal(raw)) is not None and strike > 0
        }
    )

    multiplier = to_decimal(getattr(chosen, "multiplier", None))

    return OptionChainSnapshot(
        underlying=underlying,
        as_of=as_of,
        source=source,
        origin=MarketDataOrigin.BROKER_REALTIME,
        underlying_contract_id=underlying_contract_id
        or _positive_int(getattr(chosen, "underlyingConId", None)),
        exchange=_optional_str(getattr(chosen, "exchange", None)),
        trading_class=_optional_str(getattr(chosen, "tradingClass", None)),
        multiplier=int(multiplier) if multiplier and multiplier > 0 else None,
        expirations=expirations,
        strikes=strikes,
        rights=[OptionRight.CALL, OptionRight.PUT],
    )


def _tradeable(value: Decimal | None) -> Decimal | None:
    """IBKR uses -1 for "no data" on some price fields, and 0 is not a quote."""
    if value is None or value <= 0:
        return None
    return value


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None
