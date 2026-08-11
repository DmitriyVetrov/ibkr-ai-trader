"""Deterministic simulated market data.

Every value here is derived from the symbol and the injected clock, so the same
inputs always produce the same output — no randomness, no network, no hidden
state. That is what makes simulator-backed tests reproducible.

Everything produced here is stamped ``MarketDataOrigin.SIMULATED``. It must
never be presented as, or silently substituted for, a real broker quote.
"""

from __future__ import annotations

import hashlib
from calendar import FRIDAY, monthcalendar
from datetime import date
from decimal import Decimal

from trading_system.domain.enums import (
    DataQuality,
    MarketDataOrigin,
    OptionRight,
    SecurityType,
)
from trading_system.domain.models import MarketDataSnapshot, OptionChainSnapshot
from trading_system.infrastructure.clock import Clock

__all__ = [
    "SIMULATED_SOURCE",
    "monthly_expirations",
    "simulated_option_chain",
    "simulated_quote",
    "simulated_reference_price",
    "simulated_strikes",
]

#: Recorded as ``source`` on everything the simulator produces.
SIMULATED_SOURCE = "SIMULATOR"

_PRICE_FLOOR = Decimal("20")
_PRICE_RANGE = Decimal("480")
_SPREAD_FRACTION = Decimal("0.001")
_CENT = Decimal("0.01")


def simulated_reference_price(symbol: str) -> Decimal:
    """A stable, plausible reference price for a symbol.

    Derived from a SHA-256 digest of the symbol rather than ``hash()``, whose
    value varies between interpreter runs and would make tests flaky.
    """
    digest = hashlib.sha256(symbol.upper().encode("utf-8")).digest()
    offset = Decimal(int.from_bytes(digest[:4], "big")) / Decimal(2**32)
    return (_PRICE_FLOOR + offset * _PRICE_RANGE).quantize(_CENT)


def simulated_quote(
    symbol: str,
    clock: Clock,
    security_type: SecurityType = SecurityType.STOCK,
    *,
    contract_id: int | None = None,
    currency: str = "USD",
) -> MarketDataSnapshot:
    """Build a deterministic quote snapshot around the reference price."""
    last = simulated_reference_price(symbol)
    half_spread = (last * _SPREAD_FRACTION).quantize(_CENT)
    return MarketDataSnapshot(
        symbol=symbol.upper(),
        security_type=security_type,
        as_of=clock.now(),
        source=SIMULATED_SOURCE,
        origin=MarketDataOrigin.SIMULATED,
        data_quality=DataQuality.OK,
        contract_id=contract_id,
        currency=currency,
        bid=last - half_spread,
        ask=last + half_spread,
        last=last,
        close=last,
        volume=Decimal("1000000"),
    )


def monthly_expirations(reference: date, count: int = 6) -> list[date]:
    """The next ``count`` third-Friday expirations strictly after ``reference``.

    Standard US equity options expire on the third Friday of the month, so the
    simulated chain matches the shape of a real one.
    """
    expirations: list[date] = []
    year, month = reference.year, reference.month
    while len(expirations) < count:
        fridays = [week[FRIDAY] for week in monthcalendar(year, month) if week[FRIDAY] != 0]
        third_friday = date(year, month, fridays[2])
        if third_friday > reference:
            expirations.append(third_friday)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return expirations


def simulated_strikes(reference_price: Decimal, *, step: Decimal = Decimal("5")) -> list[Decimal]:
    """A symmetric strike ladder around the reference price, snapped to ``step``."""
    centre = (reference_price / step).to_integral_value() * step
    lowest = max(step, centre - step * 8)
    return [lowest + step * i for i in range(17)]


def simulated_option_chain(
    underlying: str,
    clock: Clock,
    *,
    underlying_contract_id: int | None = None,
) -> OptionChainSnapshot:
    """Build a deterministic option chain for an underlying."""
    now = clock.now()
    reference = simulated_reference_price(underlying)
    return OptionChainSnapshot(
        underlying=underlying.upper(),
        as_of=now,
        source=SIMULATED_SOURCE,
        origin=MarketDataOrigin.SIMULATED,
        underlying_contract_id=underlying_contract_id,
        exchange="SMART",
        trading_class=underlying.upper(),
        multiplier=100,
        expirations=monthly_expirations(now.date()),
        strikes=simulated_strikes(reference),
        rights=[OptionRight.CALL, OptionRight.PUT],
    )
