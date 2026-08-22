"""The one place a figure changes currency.

A pure function of its arguments. No clock, no broker, no repository, no
network and no configuration lookup: the decision instant, the rate table and
the freshness window are all passed in, so a stored conversion can be replayed
and must reach the same answer.

Three rules, each with tests that fail loudly:

* **Never 1.0 for two different currencies.** There is no default, no fallback
  and no parity assumption anywhere in this module. Two currencies with no rate
  between them produce :attr:`~trading_system.domain.enums.FxStatus.UNAVAILABLE`
  and no figure at all.
* **Same currency is the identity, and says so.** Converting USD to USD returns
  the amount unchanged with origin ``IDENTITY``. That is the only circumstance
  in which a factor of exactly 1 is an honest description of what happened.
* **Freshness is checked before the arithmetic.** A rate too old for policy is
  ``STALE``, never a converted number that is later annotated as doubtful - the
  same ordering the readiness gate applies to evidence, for the same reason.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from trading_system.domain.enums import FxRateOrigin, FxStatus
from trading_system.fx.models import FxConversion, FxRateTable

__all__ = ["CENT", "convert"]

#: Converted money is quantised to the cent. The unquantised product would
#: carry a rate's full precision into a figure that is then compared against a
#: limit, and two limits derived from the same rate would disagree in the last
#: digit depending on which order they were multiplied in.
CENT = Decimal("0.01")


def convert(
    amount: Decimal,
    *,
    from_currency: str,
    to_currency: str,
    rates: FxRateTable,
    as_of: datetime | None,
    max_age_seconds: float | None,
) -> FxConversion:
    """Convert ``amount`` and record exactly how, or why it could not be done.

    ``as_of`` of ``None`` means there is no decision instant to measure a rate
    against. Same-currency conversion still succeeds, because the identity
    needs no rate and therefore no freshness. Anything else is ``UNAVAILABLE``:
    a rate whose age cannot be established is not a rate this system will use,
    and taking "now" from a clock here would make the module impure *and* let a
    replayed decision convert at today's freshness rather than the one it had.

    ``max_age_seconds`` of ``None`` means freshness is not policed here - the
    caller has already bound the rate to the instant some other way, which is
    the case when the rate and the balance it converts came from one broker
    read. It never means "any age is fine by default": every production caller
    passes a window from configuration.

    Rounding is ``ROUND_HALF_EVEN`` to the cent. It is deliberately *not*
    rounded down the way a limit price is: this figure is not an amount anyone
    bids, it is a measurement of capital, and biasing every measurement in one
    direction would accumulate across a campaign's whole ledger.
    """
    source, target = from_currency.upper(), to_currency.upper()

    if source == target:
        return FxConversion(
            status=FxStatus.VALID,
            amount=amount,
            from_currency=source,
            to_currency=target,
            converted_amount=amount,
            rate=Decimal(1),
            rate_as_of=as_of,
            rate_origin=FxRateOrigin.IDENTITY,
            rate_source="IDENTITY",
            rate_age_seconds=0.0,
            detail="same currency; no conversion was required and none was applied",
        )

    if as_of is None:
        return FxConversion(
            status=FxStatus.UNAVAILABLE,
            amount=amount,
            from_currency=source,
            to_currency=target,
            detail=(
                f"no decision instant was supplied, so the age of any {source}/{target} rate "
                f"cannot be established. An unaged rate is not a fresh one"
            ),
        )

    rate = rates.find(source, target)
    if rate is None:
        return FxConversion(
            status=FxStatus.UNAVAILABLE,
            amount=amount,
            from_currency=source,
            to_currency=target,
            detail=(
                f"no {source}/{target} rate was captured, in either direction. Converting "
                f"at an assumed rate would misstate this figure by an amount nobody recorded"
            ),
        )

    age = (as_of - rate.as_of).total_seconds()
    if max_age_seconds is not None and age > max_age_seconds:
        return FxConversion(
            status=FxStatus.STALE,
            amount=amount,
            from_currency=source,
            to_currency=target,
            rate_as_of=rate.as_of,
            rate_origin=rate.origin,
            rate_source=rate.source,
            rate_age_seconds=age,
            detail=(
                f"the {source}/{target} rate was already {age:.0f}s old at the decision "
                f"instant and policy permits {max_age_seconds:.0f}s. A stale rate is a rate "
                f"from a different market"
            ),
        )

    if rate.rate <= 0:
        return FxConversion(  # pragma: no cover - FxRate refuses a non-positive rate
            status=FxStatus.INVALID,
            amount=amount,
            from_currency=source,
            to_currency=target,
            rate_as_of=rate.as_of,
            rate_origin=rate.origin,
            rate_source=rate.source,
            rate_age_seconds=age,
            detail=f"the {source}/{target} rate is {rate.rate}, which is not a usable rate",
        )

    converted = (amount * rate.rate).quantize(CENT, rounding=ROUND_HALF_EVEN)
    return FxConversion(
        status=FxStatus.VALID,
        amount=amount,
        from_currency=source,
        to_currency=target,
        converted_amount=converted,
        rate=rate.rate,
        rate_as_of=rate.as_of,
        rate_origin=rate.origin,
        rate_source=rate.source,
        rate_age_seconds=age,
        detail=f"converted at {rate.rate} reported by {rate.source}",
    )
