"""What a rate is, and what one attempted conversion produced.

Two artifacts, and the split between them is the point:

:class:`FxRate`
    An *observation*. Somebody quoted this pair at this instant, and the record
    says who and when. It is never derived from a default and there is no
    constructor for a rate nobody supplied.
:class:`FxConversion`
    A *result*. It carries the four-way outcome, and only ``VALID`` carries a
    converted amount. A caller that wants the number has to look at the status
    to get it, which is what stops "we could not convert" from being read as
    zero, as one, or as the original figure.

Nothing here reaches a broker, a repository, a clock or a network. A rate
arrives as captured state, exactly as an account balance does, so a stored
authorisation can be re-derived years later and reach the same answer.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.domain.enums import FxRateOrigin, FxStatus
from trading_system.domain.models import Identifier, ImmutableModel, Money, UtcDatetime

__all__ = ["FxConversion", "FxRate", "FxRateTable"]


class FxRate(ImmutableModel):
    """One quoted rate: ``1 base = rate quote``.

    The direction is stated in the field names rather than in a pair string,
    because ``EURUSD`` reads as a rate in both directions depending on who is
    saying it, and a conversion applied the wrong way round is wrong by the
    square of the rate without ever looking implausible.
    """

    base_currency: str = Field(min_length=3, max_length=8)
    quote_currency: str = Field(min_length=3, max_length=8)
    #: How many units of ``quote_currency`` one unit of ``base_currency`` buys.
    rate: Money = Field(gt=0)
    as_of: UtcDatetime
    origin: FxRateOrigin
    #: Who reported it - a broker name, a provider, a fixture. Never a default.
    source: Identifier

    @model_validator(mode="after")
    def _a_rate_between_one_currency_and_itself_is_not_an_observation(self) -> FxRate:
        if self.base_currency.upper() == self.quote_currency.upper():
            raise ValueError(
                f"{self.base_currency} to {self.quote_currency} is not a rate anyone quotes. "
                f"A same-currency conversion is the identity and needs no rate; storing one "
                f"would put a factor in the record that could later be edited to something "
                f"other than 1"
            )
        return self

    @property
    def pair(self) -> tuple[str, str]:
        return self.base_currency.upper(), self.quote_currency.upper()

    def inverted(self) -> FxRate:
        """The same observation, quoted the other way round.

        Marked :attr:`FxRateOrigin.INVERTED` so the record never implies the
        source quoted this direction. The reciprocal is exact in ``Decimal``
        only for a few rates, so it is computed at high precision and the
        original is what stays on file.
        """
        return FxRate(
            base_currency=self.quote_currency,
            quote_currency=self.base_currency,
            rate=Decimal(1) / self.rate,
            as_of=self.as_of,
            origin=FxRateOrigin.INVERTED,
            source=self.source,
        )


class FxRateTable(ImmutableModel):
    """Every rate captured in one observation, addressable by pair.

    Deliberately not a ``dict[str, Decimal]``. A bare mapping loses the instant
    and the source, and those are the two things that decide whether a rate may
    be used at all.
    """

    rates: tuple[FxRate, ...] = ()

    @model_validator(mode="after")
    def _one_rate_per_direction(self) -> FxRateTable:
        seen: set[tuple[str, str]] = set()
        for rate in self.rates:
            if rate.pair in seen:
                raise ValueError(
                    f"two rates for {rate.base_currency}/{rate.quote_currency} in one table; "
                    f"which one applied would depend on iteration order"
                )
            seen.add(rate.pair)
        return self

    def find(self, base: str, quote: str) -> FxRate | None:
        """The rate for this direction, inverting a reverse quote if needed.

        Direct before inverted, and nothing else: no triangulation through a
        third currency. Chaining two rates compounds two staleness windows and
        two spreads into a figure that carries neither, and the error is
        invisible in the result.
        """
        base, quote = base.upper(), quote.upper()
        for rate in self.rates:
            if rate.pair == (base, quote):
                return rate
        for rate in self.rates:
            if rate.pair == (quote, base):
                return rate.inverted()
        return None


class FxConversion(ImmutableModel):
    """One conversion attempt, successful or not.

    ``converted_amount`` exists only on a ``VALID`` result. That is enforced by
    a validator rather than left to callers, because the failure this shape
    exists to prevent is a caller reading a defaulted zero - or, worse, the
    unconverted original - out of a conversion that did not happen.
    """

    status: FxStatus
    amount: Money
    from_currency: str = Field(min_length=3, max_length=8)
    to_currency: str = Field(min_length=3, max_length=8)

    converted_amount: Money | None = None
    rate: Money | None = None
    rate_as_of: UtcDatetime | None = None
    rate_origin: FxRateOrigin | None = None
    rate_source: Identifier | None = None
    rate_age_seconds: float | None = None
    #: Why it failed, in words, for the check that will carry it.
    detail: str = ""

    @model_validator(mode="after")
    def _only_a_valid_conversion_carries_a_figure(self) -> FxConversion:
        if self.status is FxStatus.VALID:
            missing = [
                name
                for name, value in (
                    ("converted_amount", self.converted_amount),
                    ("rate", self.rate),
                    ("rate_origin", self.rate_origin),
                    ("rate_source", self.rate_source),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"a VALID conversion must name its arithmetic; missing {', '.join(missing)}"
                )
            return self
        if self.converted_amount is not None or self.rate is not None:
            raise ValueError(
                f"a {self.status.value} conversion carries no figure and no rate. A number "
                f"beside a failed conversion is how an unconverted amount gets spent"
            )
        if not self.detail:
            raise ValueError(f"a {self.status.value} conversion must say what went wrong")
        return self

    @property
    def ok(self) -> bool:
        return self.status is FxStatus.VALID

    @property
    def value(self) -> Decimal:
        """The converted figure. Raises unless the conversion succeeded.

        Deliberately not an ``Optional`` accessor with a fallback: every caller
        of this is about to compare money against a limit, and the honest
        behaviour when there is no figure is to stop, not to substitute one.
        """
        if self.converted_amount is None:
            raise ValueError(
                f"no converted amount: {self.from_currency} to {self.to_currency} ended "
                f"{self.status.value} ({self.detail})"
            )
        return self.converted_amount

    def describe(self) -> str:
        """One line naming the arithmetic, for a check's detail field."""
        if not self.ok:
            return f"{self.from_currency}->{self.to_currency} {self.status.value}: {self.detail}"
        return (
            f"{self.amount} {self.from_currency} x {self.rate} = {self.converted_amount} "
            f"{self.to_currency} (rate from {self.rate_source} at "
            f"{self.rate_as_of.isoformat() if self.rate_as_of else 'unstated'})"
        )
