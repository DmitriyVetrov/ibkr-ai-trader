"""The conversion layer itself: four outcomes, and never a fifth.

Everything downstream — the risk engine, the allocator, position sizing, order
validation — compares figures that came out of :func:`convert`. So the
properties asserted here are the ones every one of those depends on, and the
one asserted hardest is the negative: **there is no input to this function that
makes two different currencies convert at 1.0.**

That is worth a suite of its own rather than a line in the risk tests, because
the failure it prevents is silent. A converted figure that is wrong by an
exchange rate looks exactly like a correct one, passes every accounting
identity, and produces a position sized wrongly by an amount nobody recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system.domain.enums import FxRateOrigin, FxStatus
from trading_system.fx import FxConversion, FxRate, FxRateTable, convert

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 22, 14, 30, tzinfo=UTC)


def rate(
    base: str = "EUR",
    quote: str = "USD",
    value: str = "1.10",
    as_of: datetime = NOW,
    origin: FxRateOrigin = FxRateOrigin.BROKER_ACCOUNT_LEDGER,
) -> FxRate:
    return FxRate(
        base_currency=base,
        quote_currency=quote,
        rate=Decimal(value),
        as_of=as_of,
        origin=origin,
        source="TEST",
    )


def table(*rates: FxRate) -> FxRateTable:
    return FxRateTable(rates=rates)


def _convert(
    amount: str = "5000",
    *,
    frm: str = "EUR",
    to: str = "USD",
    rates: FxRateTable | None = None,
    as_of: datetime | None = NOW,
    max_age: float | None = 86400.0,
) -> FxConversion:
    return convert(
        Decimal(amount),
        from_currency=frm,
        to_currency=to,
        rates=rates if rates is not None else table(rate()),
        as_of=as_of,
        max_age_seconds=max_age,
    )


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------
def test_a_valid_rate_converts_and_records_how() -> None:
    """Case 3: EUR 5,000 at 1.10 is USD 5,500, and the record says why."""
    result = _convert()

    assert result.status is FxStatus.VALID
    assert result.converted_amount == Decimal("5500.00")
    assert result.rate == Decimal("1.10")
    assert result.from_currency == "EUR"
    assert result.to_currency == "USD"
    assert result.rate_source == "TEST"
    assert result.rate_as_of == NOW
    assert result.rate_origin is FxRateOrigin.BROKER_ACCOUNT_LEDGER


def test_the_original_amount_survives_the_conversion() -> None:
    """The source figure is never converted in place.

    A record that replaced 5,000 with 5,500 could not answer how much of the
    operator's own money is committed, which is the question an operator asks
    about their own account.
    """
    result = _convert()

    assert result.amount == Decimal("5000")
    assert result.from_currency == "EUR"


def test_a_reverse_quote_is_inverted_and_marked_as_derived() -> None:
    """IBKR quotes each currency into the base, so half of these are inverted.

    The origin changes to ``INVERTED`` so a stored artifact never implies the
    source quoted this direction — it quoted the other one, and the reciprocal
    is this system's arithmetic rather than the broker's statement.
    """
    result = _convert(rates=table(rate(base="USD", quote="EUR", value="0.90")))

    assert result.status is FxStatus.VALID
    assert result.rate_origin is FxRateOrigin.INVERTED
    assert result.converted_amount == Decimal("5555.56")


def test_rounding_is_half_even_to_the_cent() -> None:
    """Not rounded down the way a limit price is, and the difference matters.

    A limit price is rounded down because it is a number this system *offers*
    and must never exceed what was authorised. This figure is a measurement of
    capital, and biasing every measurement one way would accumulate across a
    campaign's whole ledger.
    """
    result = _convert("100", rates=table(rate(value="1.005")))

    assert result.converted_amount == Decimal("100.50")


# ---------------------------------------------------------------------------
# The identity, and only the identity
# ---------------------------------------------------------------------------
def test_the_same_currency_converts_at_one_and_says_so() -> None:
    """The only circumstance in which a factor of exactly 1 is honest."""
    result = _convert(frm="USD", to="USD", rates=FxRateTable())

    assert result.status is FxStatus.VALID
    assert result.converted_amount == Decimal("5000")
    assert result.rate == Decimal(1)
    assert result.rate_origin is FxRateOrigin.IDENTITY


def test_two_different_currencies_never_convert_at_one() -> None:
    """The negative claim this whole layer exists to make.

    Every way of failing to find a rate is tried: an empty table, a table with
    the wrong pair, a stale rate, and no decision instant. None of them yields
    a figure, and none of them yields a rate of 1.
    """
    attempts = [
        _convert(rates=FxRateTable()),
        _convert(rates=table(rate(base="GBP", quote="CHF"))),
        _convert(rates=table(rate(as_of=NOW - timedelta(days=30)))),
        _convert(as_of=None),
    ]

    for result in attempts:
        assert result.status is not FxStatus.VALID
        assert result.converted_amount is None
        assert result.rate is None
        assert result.detail, "a failure must say what went wrong"


def test_a_rate_between_a_currency_and_itself_cannot_be_constructed() -> None:
    """An identity stored as a factor is a factor someone could later edit."""
    with pytest.raises(ValidationError, match="not a rate anyone quotes"):
        rate(base="USD", quote="USD", value="1.00")


# ---------------------------------------------------------------------------
# The four outcomes are four different facts
# ---------------------------------------------------------------------------
def test_an_absent_pair_is_unavailable() -> None:
    result = _convert(rates=FxRateTable())

    assert result.status is FxStatus.UNAVAILABLE
    assert "EUR/USD" in result.detail


def test_a_rate_older_than_policy_is_stale_not_unavailable() -> None:
    """Different facts, different fixes: one needs a capture, one needs a feed."""
    result = _convert(rates=table(rate(as_of=NOW - timedelta(seconds=86401))))

    assert result.status is FxStatus.STALE
    assert result.rate_age_seconds == pytest.approx(86401.0)
    # The rate's provenance is still recorded even though it was not used —
    # "which stale rate" is the first question an operator asks.
    assert result.rate_source == "TEST"
    assert result.rate is None, "recorded provenance is not a usable figure"


def test_freshness_is_checked_before_the_arithmetic() -> None:
    """A stale pass annotated afterwards is exactly the artifact to avoid."""
    result = _convert(rates=table(rate(as_of=NOW - timedelta(days=1000))))

    assert result.status is FxStatus.STALE
    assert result.converted_amount is None


def test_no_decision_instant_means_no_conversion() -> None:
    """An unaged rate is not a fresh one, and 'now' is not this module's to take.

    Reading a clock here would make the function impure *and* let a replayed
    decision convert at today's freshness rather than the one it actually had.
    """
    result = _convert(as_of=None)

    assert result.status is FxStatus.UNAVAILABLE
    assert "decision instant" in result.detail


def test_an_unbounded_window_still_requires_a_rate() -> None:
    """``max_age_seconds=None`` relaxes freshness, never existence."""
    assert _convert(max_age=None).status is FxStatus.VALID
    assert _convert(rates=FxRateTable(), max_age=None).status is FxStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# The shape refuses to lie
# ---------------------------------------------------------------------------
def test_a_failed_conversion_cannot_carry_a_figure() -> None:
    """The defect this model exists to prevent, made unconstructable.

    A caller reading a defaulted zero — or, worse, the unconverted original —
    out of a conversion that did not happen is how money moves at the wrong
    rate. So the model refuses the shape rather than trusting the caller.
    """
    with pytest.raises(ValidationError, match="carries no figure"):
        FxConversion(
            status=FxStatus.UNAVAILABLE,
            amount=Decimal("5000"),
            from_currency="EUR",
            to_currency="USD",
            converted_amount=Decimal("5000"),
            detail="no rate",
        )


def test_a_valid_conversion_must_name_its_arithmetic() -> None:
    with pytest.raises(ValidationError, match="must name its arithmetic"):
        FxConversion(
            status=FxStatus.VALID,
            amount=Decimal("5000"),
            from_currency="EUR",
            to_currency="USD",
        )


def test_reading_the_figure_off_a_failed_conversion_raises() -> None:
    """Deliberately not an optional accessor with a fallback.

    Every caller of ``.value`` is about to compare money against a limit. The
    honest behaviour when there is no figure is to stop.
    """
    result = _convert(rates=FxRateTable())

    with pytest.raises(ValueError, match="no converted amount"):
        _ = result.value


def test_two_rates_for_one_direction_are_refused() -> None:
    """Which one applied would otherwise depend on iteration order."""
    with pytest.raises(ValidationError, match="two rates for"):
        table(rate(value="1.10"), rate(value="1.20"))


# ---------------------------------------------------------------------------
# What the layer deliberately will not do
# ---------------------------------------------------------------------------
def test_there_is_no_triangulation_through_a_third_currency() -> None:
    """EUR->USD and USD->CZK do not make a EUR->CZK rate.

    Chaining compounds two staleness windows and two spreads into a figure
    carrying neither, and the error is invisible in the result. A pair nobody
    quoted directly is a pair this system has no rate for.
    """
    rates = table(rate(base="EUR", quote="USD"), rate(base="USD", quote="CZK", value="23.0"))

    result = _convert(to="CZK", rates=rates)

    assert result.status is FxStatus.UNAVAILABLE


def test_the_layer_generalises_beyond_eur_usd() -> None:
    """Nothing here is hard-coded for one pair.

    The same mechanism has to carry a EUR budget into CZK for a campaign
    trading Czech instruments, and the only thing that changes is the rate.
    """
    result = _convert("5000", to="CZK", rates=table(rate(quote="CZK", value="25.30")))

    assert result.status is FxStatus.VALID
    assert result.converted_amount == Decimal("126500.00")
    assert result.to_currency == "CZK"
