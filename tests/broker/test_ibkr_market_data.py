"""Translating IBKR quotes and option chains.

The invariant under test throughout: **no invented prices**. IBKR returns NaN
liberally, and every one of those paths must end in an explicit "unavailable",
never in a zero or a stale substitute.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_system.broker.ibkr.market_data import (
    market_data_origin,
    to_market_data_snapshot,
    to_option_chain_snapshot,
    unavailable_snapshot,
)
from trading_system.domain.enums import DataQuality, MarketDataOrigin, OptionRight

from .conftest import BROKER_NOW, NAN, make_option_chain_row, make_ticker


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_quote_is_translated() -> None:
    snapshot = to_market_data_snapshot(make_ticker(), BROKER_NOW)

    assert snapshot.symbol == "NVDA"
    assert snapshot.bid == Decimal("6.2")
    assert snapshot.ask == Decimal("6.3")
    assert snapshot.last == Decimal("6.25")
    assert snapshot.close == Decimal("6.1")
    assert snapshot.data_quality is DataQuality.OK
    assert snapshot.has_quote


@pytest.mark.unit
def test_all_nan_becomes_unavailable_not_zero() -> None:
    ticker = make_ticker(bid=NAN, ask=NAN, last=NAN, close=NAN, volume=NAN)
    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.origin is MarketDataOrigin.UNAVAILABLE
    assert snapshot.data_quality is DataQuality.UNUSABLE
    assert snapshot.bid is None
    assert snapshot.ask is None
    assert snapshot.last is None
    assert snapshot.close is None
    assert not snapshot.has_quote


@pytest.mark.unit
def test_close_only_quote_is_marked_stale() -> None:
    """Outside market hours only the previous close survives; say so."""
    ticker = make_ticker(bid=NAN, ask=NAN, last=NAN, close=6.10)
    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.data_quality is DataQuality.STALE
    assert snapshot.close == Decimal("6.1")
    assert snapshot.bid is None


@pytest.mark.unit
def test_missing_one_side_of_the_book_is_degraded() -> None:
    snapshot = to_market_data_snapshot(make_ticker(bid=NAN), BROKER_NOW)
    assert snapshot.data_quality is DataQuality.DEGRADED


@pytest.mark.unit
@pytest.mark.parametrize("bad_price", [0.0, -1.0])
def test_non_positive_prices_are_treated_as_absent(bad_price: float) -> None:
    """IBKR uses -1 for "no data" on some fields, and 0 is not a quote."""
    snapshot = to_market_data_snapshot(make_ticker(bid=bad_price), BROKER_NOW)
    assert snapshot.bid is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1, MarketDataOrigin.BROKER_REALTIME),
        (2, MarketDataOrigin.BROKER_FROZEN),
        (3, MarketDataOrigin.BROKER_DELAYED),
        (4, MarketDataOrigin.BROKER_FROZEN),
    ],
)
def test_market_data_type_maps_to_origin(code: int, expected: MarketDataOrigin) -> None:
    assert market_data_origin(code) is expected


@pytest.mark.unit
def test_unknown_market_data_type_is_not_claimed_to_be_live() -> None:
    """If we cannot prove data is real-time, we must not label it real-time."""
    assert market_data_origin(None) is not MarketDataOrigin.BROKER_REALTIME
    assert market_data_origin(99) is not MarketDataOrigin.BROKER_REALTIME


@pytest.mark.unit
def test_delayed_data_is_labelled_delayed() -> None:
    snapshot = to_market_data_snapshot(make_ticker(market_data_type=3), BROKER_NOW)
    assert snapshot.origin is MarketDataOrigin.BROKER_DELAYED


@pytest.mark.unit
def test_unavailable_snapshot_helper_carries_no_prices() -> None:
    snapshot = unavailable_snapshot("SPY", BROKER_NOW)
    assert snapshot.origin is MarketDataOrigin.UNAVAILABLE
    assert not snapshot.has_quote


@pytest.mark.unit
def test_model_refuses_an_unavailable_snapshot_carrying_a_price() -> None:
    """Belt and braces: the model itself rejects the contradiction."""
    from pydantic import ValidationError

    from trading_system.domain.models import MarketDataSnapshot

    with pytest.raises(ValidationError, match="must carry no prices"):
        MarketDataSnapshot(
            symbol="SPY",
            security_type="STOCK",
            as_of=BROKER_NOW,
            source="IBKR",
            origin=MarketDataOrigin.UNAVAILABLE,
            bid=Decimal("1.00"),
        )


# ---------------------------------------------------------------------------
# Option chains
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_chain_is_normalised_sorted_and_deduplicated() -> None:
    row = make_option_chain_row(
        expirations=["20260918", "20260821", "20260918"],
        strikes=[180.0, 175.0, 180.0],
    )
    chain = to_option_chain_snapshot([row], "SPY", BROKER_NOW)

    assert chain.expirations == [date(2026, 8, 21), date(2026, 9, 18)]
    assert chain.strikes == [Decimal("175.0"), Decimal("180.0")]
    assert chain.rights == [OptionRight.CALL, OptionRight.PUT]
    assert chain.multiplier == 100


@pytest.mark.unit
def test_zero_strikes_are_dropped() -> None:
    chain = to_option_chain_snapshot(
        [make_option_chain_row(strikes=[0.0, 180.0])], "SPY", BROKER_NOW
    )
    assert chain.strikes == [Decimal("180.0")]


@pytest.mark.unit
def test_unparseable_expiration_is_dropped_not_guessed() -> None:
    """A YYYYMM value has no day; inventing one would fabricate an expiry."""
    chain = to_option_chain_snapshot(
        [make_option_chain_row(expirations=["202609", "20260918"])], "SPY", BROKER_NOW
    )
    assert chain.expirations == [date(2026, 9, 18)]


@pytest.mark.unit
def test_preferred_exchange_wins() -> None:
    rows = [
        make_option_chain_row(exchange="CBOE", strikes=[100.0]),
        make_option_chain_row(exchange="SMART", strikes=[200.0]),
    ]
    chain = to_option_chain_snapshot(rows, "SPY", BROKER_NOW)
    assert chain.exchange == "SMART"
    assert chain.strikes == [Decimal("200.0")]


@pytest.mark.unit
def test_selection_is_deterministic_without_the_preferred_exchange() -> None:
    rows = [
        make_option_chain_row(exchange="NASDAQOM", strikes=[100.0]),
        make_option_chain_row(exchange="CBOE", strikes=[200.0]),
    ]
    first = to_option_chain_snapshot(rows, "SPY", BROKER_NOW)
    second = to_option_chain_snapshot(list(reversed(rows)), "SPY", BROKER_NOW)
    assert first.exchange == second.exchange == "CBOE"


@pytest.mark.unit
def test_empty_chain_is_rejected() -> None:
    with pytest.raises(ValueError, match="no option chain"):
        to_option_chain_snapshot([], "SPY", BROKER_NOW)
