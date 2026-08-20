"""Translate IBKR market data and option chains into domain snapshots.

The rule this module exists to enforce: **no invented prices**. IBKR signals a
missing quote with ``NaN``, and returns ``NaN`` liberally — outside market
hours, without a market-data subscription, or for an unqualified contract. A
snapshot with no usable field is reported as ``UNAVAILABLE``, never as zero and
never filled in from the previous close of something else.

**Two volume fields, and they are not interchangeable.**

``volume`` is the current session's cumulative share volume — IBKR tick 8
(``VOLUME``) live, tick 74 (``DELAYED_VOLUME``) delayed. Against a delayed
paper feed, **tick 74 arrives corrupted**: a raw-wire capture on
2026-08-15 (gateway 10.45, ``ib_async`` 2.1.0, ``marketDataType=3``) recorded

    <<< 2,6,3,74,31367915626456        SPY DELAYED_VOLUME

for an SPY session whose true volume was some 31.4 million shares. The
inflation is *approximately* one million on most symbols and demonstrably not
that on all of them, so there is no correction to apply:

* dividing by a fixed factor would be wrong by 10x on at least one sampled
  symbol, silently, in the direction that *passes* a liquidity floor;
* the value is not IBKR's ``DBL_MAX`` unset sentinel, so
  :func:`~trading_system.broker.ibkr.conversion.to_decimal` cannot and must not
  drop it;
* it is a real observation of a misbehaving feed, and destroying it destroys
  the evidence.

So the raw value is carried through byte for byte and the quality engine flags
``SUSPICIOUS_VOLUME``. Nothing here rescales it. Nothing anywhere should read
it as a liquidity measure.

``average_daily_volume`` is IBKR tick 21 (``avVolume``), the trailing 90-day
average daily share volume, requested through generic tick 165. The same
capture recorded it clean and unscaled on the same connection — SPY
``52014430``, NVDA ``146001516`` — and it is the field a liquidity floor should
read. It is an *independent* observation, never derived from ``volume`` and
never a repaired version of it.

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
    # Tick 21, via generic tick 165. Copied across with no arithmetic at all —
    # not scaled, not clamped, not reconciled against `volume`.
    average_daily_volume = to_decimal(getattr(ticker, "avVolume", None))

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
        average_daily_volume=(
            average_daily_volume
            if average_daily_volume is None or average_daily_volume >= 0
            else None
        ),
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

    IBKR returns one row per exchange **and trading class**, and a single
    exchange routinely appears more than once. Real paper validation on SPY
    returned 39 rows including *two* on SMART: ``SMART/SPY`` with 35
    expirations and 491 strikes, and ``SMART/2SPY`` with 3 and 3. Taking the
    first SMART row IBKR happened to send would have stored 3 of 491 strikes —
    a silent 99% loss, and fatal to the point of accumulating an option-chain
    history.

    So among the rows for the preferred exchange, the one with the widest
    coverage wins. Ties break on trading class and then exchange name — an
    arbitrary rule, but a fixed one, which is what keeps the result independent
    of IBKR's response ordering.

    The winning row's ``tradingClass`` is carried through exactly as reported.
    ``2SPY`` is a real, separate class that coexists with ``SPY``; the lesson
    from validation is that the trading class cannot be *derived* from the
    symbol, not that SPY options are always ``2SPY``.
    """
    if not chains:
        raise ValueError(f"no option chain rows returned for {underlying}")

    preferred = [c for c in chains if getattr(c, "exchange", None) == preferred_exchange]
    chosen = max(preferred or chains, key=_coverage)

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


def _coverage(row: Any) -> tuple[int, int, str, str]:
    """Rank a chain row: widest coverage first, then deterministically.

    The exchange and trading class are included as tie-breakers so that two
    rows describing the same number of contracts still order identically on
    every run, whatever order IBKR sent them in.
    """
    strikes = getattr(row, "strikes", None) or ()
    expirations = getattr(row, "expirations", None) or ()
    return (
        len(strikes),
        len(expirations),
        str(getattr(row, "tradingClass", "") or ""),
        str(getattr(row, "exchange", "") or ""),
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
