"""IBKR per-contract option quotes, and the sentinels hiding in them.

Everything here was captured against IB Gateway 10.45 with ``ib_async`` 2.1.0
on 2026-08-20, market closed, paper account, ``IBKR_MARKET_DATA_TYPE=3``::

    tick  66 -> -1        delayed bid        "no value"
    tick  67 -> -1        delayed ask        "no value"
    tick  68 -> 10.9      delayed last
    tick  74 -> -1        delayed volume     "no value"
    tick  75 -> 14.73     delayed close

and, on the same contract with ``marketDataType=4``::

    tick  66 -> 10.37     delayed-frozen bid
    tick  67 -> 11.14     delayed-frozen ask

``ib_async`` copies those onto ``ticker.bid`` / ``ticker.ask`` unchanged, so
``-1`` reaches the translation as a plain float. It is neither ``NaN`` nor the
``DBL_MAX`` sentinel, which means ``to_decimal`` cannot drop it and must not
try — a legitimate ``-1`` exists elsewhere in the API (unrealised P&L, a put's
delta). The rejection has to happen where the field's meaning is known.

The failure this suite exists to prevent is quiet and expensive: a ``-1`` ask
reaching the allocator, where ``price_source: ASK_DEBIT`` would read it as the
cost of a contract.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_system.broker.ibkr.market_data import to_option_quote_snapshot
from trading_system.domain.enums import DataQuality, MarketDataOrigin, OptionRight

from .conftest import (
    BROKER_NOW,
    NAN,
    NO_COMPUTATION,
    NO_VALUE,
    make_option_computation,
    make_option_contract,
    make_option_quote_ticker,
    make_stock_contract,
)


# ---------------------------------------------------------------------------
# The -1 sentinel
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_the_minus_one_bid_and_ask_sentinel_becomes_unavailable() -> None:
    """The exact delayed, market-closed capture: -1 is not a price."""
    ticker = make_option_quote_ticker(bid=NO_VALUE, ask=NO_VALUE, last=10.9, close=14.73)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.bid is None
    assert quote.ask is None
    assert quote.last == Decimal("10.9")
    assert quote.close == Decimal("14.73")


@pytest.mark.unit
def test_a_minus_one_ask_never_reaches_a_cost() -> None:
    """The specific disaster: ASK_DEBIT reading -1 as what a contract costs."""
    ticker = make_option_quote_ticker(bid=NO_VALUE, ask=NO_VALUE)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.ask is None
    assert not quote.has_two_sided_quote
    # And nothing substituted for it from the side that did report.
    assert quote.ask != quote.last
    assert quote.ask != quote.close


@pytest.mark.unit
def test_minus_one_volume_is_absent_not_negative() -> None:
    """Tick 74 arrived as -1 on the same capture."""
    ticker = make_option_quote_ticker(volume=NO_VALUE)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.volume is None


@pytest.mark.unit
def test_zero_is_not_a_quote_either() -> None:
    """0.00 is IBKR's other way of saying nothing was quoted."""
    ticker = make_option_quote_ticker(bid=0.0, ask=0.0)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.bid is None
    assert quote.ask is None


@pytest.mark.unit
def test_nan_still_becomes_none() -> None:
    """The older sentinel has not stopped mattering."""
    ticker = make_option_quote_ticker(bid=NAN, ask=NAN, last=NAN, close=14.73)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.bid is None
    assert quote.ask is None
    assert quote.last is None
    assert quote.close == Decimal("14.73")


# ---------------------------------------------------------------------------
# Frozen data is where a closed market keeps its quote
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_delayed_frozen_two_sided_quote_survives_intact() -> None:
    """marketDataType=4 on the same contract: real bid and ask."""
    ticker = make_option_quote_ticker(
        bid=10.37, ask=11.14, last=10.9, close=14.73, market_data_type=4
    )

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.bid == Decimal("10.37")
    assert quote.ask == Decimal("11.14")
    assert quote.has_two_sided_quote
    assert quote.data_quality is DataQuality.OK


@pytest.mark.unit
def test_frozen_and_delayed_are_different_origins() -> None:
    """A consumer must always be able to tell which feed answered."""
    delayed = to_option_quote_snapshot(make_option_quote_ticker(market_data_type=3), BROKER_NOW)
    frozen = to_option_quote_snapshot(make_option_quote_ticker(market_data_type=4), BROKER_NOW)

    assert delayed.origin is MarketDataOrigin.BROKER_DELAYED
    assert frozen.origin is MarketDataOrigin.BROKER_FROZEN


# ---------------------------------------------------------------------------
# Greeks
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_model_greeks_are_carried_across_exactly() -> None:
    ticker = make_option_quote_ticker(
        model_greeks=make_option_computation(
            implied_vol=0.12853707234503078,
            delta=0.5605018766549641,
            gamma=0.016401697671914614,
            vega=0.7378027531531508,
            theta=-0.26983587485386806,
            und_price=762.340576171875,
        )
    )

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.implied_volatility == Decimal("0.12853707234503078")
    assert quote.delta == Decimal("0.5605018766549641")
    assert quote.gamma == Decimal("0.016401697671914614")
    assert quote.vega == Decimal("0.7378027531531508")
    assert quote.theta == Decimal("-0.26983587485386806")
    assert quote.underlying_price == Decimal("762.340576171875")


@pytest.mark.unit
def test_a_negative_delta_is_preserved_for_a_put() -> None:
    """The sign is information. A put's delta is negative and stays negative."""
    ticker = make_option_quote_ticker(
        contract=make_option_contract(right="P"),
        model_greeks=make_option_computation(delta=-0.4500196558183862),
    )

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.delta == Decimal("-0.4500196558183862")


@pytest.mark.unit
def test_bid_and_ask_greeks_are_never_read() -> None:
    """They carried vega=-2.0, theta=-2.0 next to delta=None on the real feed.

    A theta of -2.0 is perfectly possible for a real contract, so no filter
    separates the sentinel from the value honestly. The resolution is to read
    ``modelGreeks`` and nothing else — asserted here by giving the bid and ask
    computations values that would be unmistakable if they leaked.
    """
    ticker = make_option_quote_ticker(
        model_greeks=make_option_computation(vega=0.7378, theta=-0.2698),
        bid_greeks=make_option_computation(vega=NO_COMPUTATION, theta=NO_COMPUTATION),
        ask_greeks=make_option_computation(vega=NO_COMPUTATION, theta=NO_COMPUTATION),
    )

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.vega == Decimal("0.7378")
    assert quote.theta == Decimal("-0.2698")
    assert quote.vega != Decimal(str(NO_COMPUTATION))
    assert quote.theta != Decimal(str(NO_COMPUTATION))


@pytest.mark.unit
def test_absent_greeks_are_none_not_zero() -> None:
    """One contract in the real capture reported no modelGreeks at all."""
    ticker = make_option_quote_ticker(model_greeks=None)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.implied_volatility is None
    assert quote.delta is None
    assert quote.gamma is None
    assert quote.theta is None
    assert quote.vega is None
    assert quote.underlying_price is None
    # ...and the quote is still a quote: a missing Greek is not a missing price.
    assert quote.has_quote


@pytest.mark.unit
def test_negative_implied_volatility_is_dropped() -> None:
    """A volatility below zero is not a measurement, whatever the tick said."""
    ticker = make_option_quote_ticker(model_greeks=make_option_computation(implied_vol=NO_VALUE))

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.implied_volatility is None


@pytest.mark.unit
def test_an_implausible_delta_is_preserved_for_the_quality_engine() -> None:
    """Plausibility is not judged here, deliberately.

    ``config/data.yaml`` states ``max_abs_delta`` and the quality engine
    applies it. A second, silent bound in the broker adapter would destroy the
    evidence that the feed misbehaved before anything could record it — the
    same reasoning that preserves the corrupted tick 74 verbatim.
    """
    ticker = make_option_quote_ticker(model_greeks=make_option_computation(delta=4.2))

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.delta == Decimal("4.2")


# ---------------------------------------------------------------------------
# Open interest belongs to a side
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_open_interest_is_read_from_the_matching_side() -> None:
    """Reading the counterpart's depth would float a thin contract past a floor."""
    call = to_option_quote_snapshot(
        make_option_quote_ticker(
            contract=make_option_contract(right="C"),
            call_open_interest=1500.0,
            put_open_interest=7.0,
        ),
        BROKER_NOW,
    )
    put = to_option_quote_snapshot(
        make_option_quote_ticker(
            contract=make_option_contract(right="P"),
            call_open_interest=1500.0,
            put_open_interest=7.0,
        ),
        BROKER_NOW,
    )

    assert call.open_interest == Decimal("1500")
    assert put.open_interest == Decimal("7")


@pytest.mark.unit
def test_unreported_open_interest_is_none_not_zero() -> None:
    """Generic tick 101 is not always served. Absent is not empty."""
    ticker = make_option_quote_ticker(call_open_interest=NAN, put_open_interest=NAN)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.open_interest is None


# ---------------------------------------------------------------------------
# Contract identity
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_the_contract_identifies_itself_completely() -> None:
    ticker = make_option_quote_ticker(
        contract=make_option_contract(
            symbol="SPY", con_id=907156215, expiry="20260911", strike=762.0, right="C"
        )
    )

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.contract.contract_id == 907156215
    assert quote.contract.symbol == "SPY"
    assert quote.contract.expiration == date(2026, 9, 11)
    assert quote.contract.strike == Decimal("762")
    assert quote.contract.right is OptionRight.CALL
    assert quote.contract.multiplier == 100


@pytest.mark.unit
def test_a_stock_ticker_is_refused() -> None:
    """An option quote for something that is not an option is not repaired."""
    ticker = make_option_quote_ticker(contract=make_stock_contract())

    with pytest.raises(ValueError, match="OPTION contract"):
        to_option_quote_snapshot(ticker, BROKER_NOW)


@pytest.mark.unit
def test_a_contract_with_no_strike_is_refused_not_guessed() -> None:
    ticker = make_option_quote_ticker(contract=make_option_contract(strike=0.0))

    with pytest.raises(ValueError, match="strike"):
        to_option_quote_snapshot(ticker, BROKER_NOW)


@pytest.mark.unit
def test_a_ticker_with_no_contract_is_refused() -> None:
    ticker = make_option_quote_ticker()
    ticker.contract = None

    with pytest.raises(ValueError, match="no contract"):
        to_option_quote_snapshot(ticker, BROKER_NOW)


# ---------------------------------------------------------------------------
# Whole-quote verdicts
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_no_price_at_all_is_unavailable_and_carries_no_greeks() -> None:
    """An implied volatility for a contract nobody quoted is a model output."""
    ticker = make_option_quote_ticker(bid=NO_VALUE, ask=NO_VALUE, last=NAN, close=NAN)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.origin is MarketDataOrigin.UNAVAILABLE
    assert quote.data_quality is DataQuality.UNUSABLE
    assert not quote.has_quote
    assert quote.implied_volatility is None
    assert quote.delta is None
    # The contract is still identified: we know what we could not price.
    assert quote.contract.strike == Decimal("180")


@pytest.mark.unit
def test_a_one_sided_quote_is_degraded_not_ok() -> None:
    """A cost comes from the ask and a spread needs both sides."""
    ticker = make_option_quote_ticker(bid=NO_VALUE, ask=NO_VALUE, last=10.9)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.has_quote
    assert not quote.has_two_sided_quote
    assert quote.data_quality is DataQuality.DEGRADED


@pytest.mark.unit
def test_only_a_previous_close_is_stale() -> None:
    ticker = make_option_quote_ticker(bid=NO_VALUE, ask=NO_VALUE, last=NAN, close=14.73)

    quote = to_option_quote_snapshot(ticker, BROKER_NOW)

    assert quote.data_quality is DataQuality.STALE
    assert quote.close == Decimal("14.73")
