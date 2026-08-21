"""Normalise broker domain models into canonical data records.

Milestone 2 already translated IBKR's wire objects into domain models and
handled the traps there — ``NaN`` and ``DBL_MAX`` sentinels becoming ``None``,
prices becoming exact decimals, ``YYYYMMDD`` becoming dates. This module does
the second, smaller step: attaching provenance and quality-bearing structure so
the record can be stored, queried point-in-time and consumed by a future agent.

What normalisation may do: rename fields, map enums, convert timestamps,
attach provenance.

What it may not do, and does not do here:

* invent a value that the broker did not send — every ``None`` stays ``None``;
* repair a suspicious number — that is the quality engine's job, and its job is
  to flag, not to fix;
* overwrite or reconstruct a broker identifier.

The last one has teeth. IBKR reports SPY *options* under trading class ``2SPY``
while the SPY *underlying* uses ``SPY``. Deriving ``trading_class`` from the
symbol — the obvious-looking simplification — produces a contract the broker
does not recognise. Every identifier is copied across verbatim.
"""

from __future__ import annotations

from datetime import datetime

from trading_system.data.models import (
    DataSourceMetadata,
    MarketQuote,
    OptionChain,
    OptionContract,
    OptionQuote,
)
from trading_system.domain.models import (
    BrokerContract,
    MarketDataSnapshot,
    OptionChainSnapshot,
    OptionQuoteSnapshot,
)

__all__ = [
    "market_quote_from_broker",
    "option_chain_from_broker",
    "option_contract_from_broker",
    "option_quote_from_broker",
]


def market_quote_from_broker(
    snapshot: MarketDataSnapshot,
    *,
    source: DataSourceMetadata,
) -> MarketQuote:
    """Convert a broker quote snapshot into a canonical :class:`MarketQuote`.

    The broker's ``as_of`` is the exchange-side quote time where IBKR supplied
    one; it is carried through unchanged rather than replaced by our own clock,
    so the record still says when the market produced it.
    """
    return MarketQuote(
        as_of=snapshot.as_of,
        source=source,
        symbol=snapshot.symbol,
        security_type=snapshot.security_type,
        currency=snapshot.currency,
        contract_id=snapshot.contract_id,
        bid=snapshot.bid,
        ask=snapshot.ask,
        last=snapshot.last,
        close=snapshot.close,
        volume=snapshot.volume,
        average_daily_volume=snapshot.average_daily_volume,
    )


def option_contract_from_broker(
    contract: BrokerContract,
    *,
    underlying: str,
) -> OptionContract:
    """Convert a broker contract, preserving every identifier exactly.

    ``trading_class`` and ``local_symbol`` come straight from the broker. They
    are never derived from ``underlying``, which is passed in only to record
    which underlying this contract belongs to.
    """
    return OptionContract(
        underlying=underlying,
        symbol=contract.symbol,
        security_type=contract.security_type,
        expiration=contract.expiration,
        strike=contract.strike,
        right=contract.right,
        contract_id=contract.contract_id,
        exchange=contract.exchange,
        primary_exchange=contract.primary_exchange,
        currency=contract.currency,
        multiplier=contract.multiplier,
        trading_class=contract.trading_class,
        local_symbol=contract.local_symbol,
    )


def option_chain_from_broker(
    snapshot: OptionChainSnapshot,
    *,
    source: DataSourceMetadata,
    as_of: datetime | None = None,
    contracts: list[OptionContract] | None = None,
) -> OptionChain:
    """Convert broker chain metadata into a canonical :class:`OptionChain`.

    ``trading_class`` is carried across as the broker reported it — for SPY
    options that is ``2SPY``, not ``SPY``.
    """
    return OptionChain(
        as_of=as_of or snapshot.as_of,
        source=source,
        underlying=snapshot.underlying,
        underlying_contract_id=snapshot.underlying_contract_id,
        exchange=snapshot.exchange,
        trading_class=snapshot.trading_class,
        multiplier=snapshot.multiplier,
        expirations=list(snapshot.expirations),
        strikes=list(snapshot.strikes),
        rights=list(snapshot.rights),
        contracts=list(contracts or []),
    )


def option_quote_from_broker(
    snapshot: OptionQuoteSnapshot,
    *,
    source: DataSourceMetadata,
    underlying: str,
    as_of: datetime | None = None,
) -> OptionQuote:
    """Convert one broker option quote into a canonical :class:`OptionQuote`.

    Every ``None`` stays ``None``. The broker adapter has already rejected
    IBKR's ``-1`` "no value" marker on the price ticks, so an absent bid here
    means the market did not quote one — not that it quoted a negative number
    and nobody noticed.
    """
    return OptionQuote(
        as_of=as_of or snapshot.as_of,
        source=source,
        contract=option_contract_from_broker(snapshot.contract, underlying=underlying),
        bid=snapshot.bid,
        ask=snapshot.ask,
        last=snapshot.last,
        close=snapshot.close,
        volume=snapshot.volume,
        open_interest=snapshot.open_interest,
        implied_volatility=snapshot.implied_volatility,
        delta=snapshot.delta,
        gamma=snapshot.gamma,
        theta=snapshot.theta,
        vega=snapshot.vega,
        underlying_price=snapshot.underlying_price,
    )
