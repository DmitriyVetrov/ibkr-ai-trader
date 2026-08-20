"""Normalisation: shape changes, substance does not.

The regression that gives this file its reason to exist: real IBKR validation
showed SPY *options* trading under class ``2SPY`` while the SPY *underlying*
uses ``SPY``. The obvious-looking simplification — deriving the trading class
from the symbol — produces a contract the broker does not recognise, and the
failure would only surface at order time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.data.normalizers import (
    market_quote_from_broker,
    option_chain_from_broker,
    option_contract_from_broker,
)
from trading_system.domain.enums import (
    DataQuality,
    MarketDataOrigin,
    OptionRight,
    SecurityType,
    SourceTier,
)
from trading_system.domain.models import (
    BrokerContract,
    MarketDataSnapshot,
    OptionChainSnapshot,
)

pytestmark = pytest.mark.unit

BROKER_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


def _broker_quote(**overrides: object) -> MarketDataSnapshot:
    fields: dict[str, object] = {
        "symbol": "SPY",
        "security_type": SecurityType.STOCK,
        "as_of": BROKER_NOW,
        "source": "IBKR",
        "origin": MarketDataOrigin.BROKER_DELAYED,
        "data_quality": DataQuality.OK,
        "contract_id": 756733,
        "currency": "USD",
        "bid": Decimal("500.10"),
        "ask": Decimal("500.20"),
        "last": Decimal("500.15"),
        "close": Decimal("499.80"),
        "volume": Decimal("75000000"),
        "average_daily_volume": Decimal("52014430"),
    }
    fields.update(overrides)
    return MarketDataSnapshot(**fields)


def _broker_option_contract(**overrides: object) -> BrokerContract:
    fields: dict[str, object] = {
        "symbol": "SPY",
        "security_type": SecurityType.OPTION,
        "as_of": BROKER_NOW,
        "source": "IBKR",
        "contract_id": 778899,
        "exchange": "SMART",
        "primary_exchange": "",
        "currency": "USD",
        "local_symbol": "SPY   260918C00500000",
        # The real value IBKR returns for SPY options.
        "trading_class": "2SPY",
        "multiplier": 100,
        "expiration": date(2026, 9, 18),
        "strike": Decimal("500"),
        "right": OptionRight.CALL,
    }
    fields.update(overrides)
    return BrokerContract(**fields)


# ---------------------------------------------------------------------------
# The 2SPY regression
# ---------------------------------------------------------------------------
def test_spy_option_trading_class_stays_2spy(make_source) -> None:
    """``2SPY`` must survive normalisation, unaltered and un-derived."""
    contract = option_contract_from_broker(_broker_option_contract(), underlying="SPY")

    assert contract.trading_class == "2SPY"
    assert contract.trading_class != contract.underlying
    assert contract.trading_class != contract.symbol


def test_the_chain_keeps_the_option_trading_class(make_source) -> None:
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        as_of=BROKER_NOW,
        source="IBKR",
        origin=MarketDataOrigin.BROKER_REALTIME,
        underlying_contract_id=756733,
        exchange="SMART",
        trading_class="2SPY",
        multiplier=100,
        expirations=[date(2026, 8, 21), date(2026, 9, 18)],
        strikes=[Decimal("495"), Decimal("500")],
        rights=[OptionRight.CALL, OptionRight.PUT],
    )
    chain = option_chain_from_broker(snapshot, source=make_source())

    assert chain.trading_class == "2SPY"
    assert chain.underlying == "SPY"


def test_trading_class_survives_serialisation(make_source) -> None:
    """It has to be intact on the way back out of storage too."""
    contract = option_contract_from_broker(_broker_option_contract(), underlying="SPY")
    payload = contract.model_dump(mode="json")

    assert payload["trading_class"] == "2SPY"
    from trading_system.data.models import OptionContract

    assert OptionContract.model_validate(payload).trading_class == "2SPY"


def test_every_broker_identifier_is_preserved(make_source) -> None:
    """The identifier set the broker needs to find the contract again."""
    contract = option_contract_from_broker(
        _broker_option_contract(primary_exchange="ARCA"), underlying="SPY"
    )

    assert contract.contract_id == 778899
    assert contract.symbol == "SPY"
    assert contract.security_type is SecurityType.OPTION
    assert contract.exchange == "SMART"
    assert contract.primary_exchange == "ARCA"
    assert contract.currency == "USD"
    assert contract.expiration == date(2026, 9, 18)
    assert contract.strike == Decimal("500")
    assert contract.right is OptionRight.CALL
    assert contract.multiplier == 100
    assert contract.trading_class == "2SPY"
    assert contract.local_symbol == "SPY   260918C00500000"


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def test_the_exchange_timestamp_is_carried_through_not_replaced(make_source) -> None:
    """Our clock is not the market's clock."""
    exchange_time = datetime(2026, 8, 10, 13, 59, 58, tzinfo=UTC)
    quote = market_quote_from_broker(
        _broker_quote(as_of=exchange_time),
        source=make_source(retrieved_at=BROKER_NOW, source_timestamp=exchange_time),
    )

    assert quote.as_of == exchange_time
    assert quote.source.retrieved_at == BROKER_NOW
    assert quote.source.source_timestamp == exchange_time


def test_timestamps_are_timezone_aware_and_utc(make_source) -> None:
    quote = market_quote_from_broker(_broker_quote(), source=make_source())

    assert quote.as_of.tzinfo is not None
    assert quote.as_of.utcoffset() == BROKER_NOW.utcoffset()


def test_a_naive_timestamp_is_rejected(make_source) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="naive datetime"):
        _broker_quote(as_of=datetime(2026, 8, 10, 14, 30))


# ---------------------------------------------------------------------------
# None is not zero
# ---------------------------------------------------------------------------
def test_missing_prices_stay_none(make_source) -> None:
    """ "No bid" and "a bid of zero" are different facts about a market."""
    quote = market_quote_from_broker(
        _broker_quote(bid=None, ask=None, volume=None), source=make_source()
    )

    assert quote.bid is None
    assert quote.ask is None
    assert quote.volume is None
    assert quote.bid != 0


def test_a_quote_with_no_two_sided_market_has_no_midpoint(make_source) -> None:
    """A midpoint from one side is not a midpoint, and is not invented."""
    quote = market_quote_from_broker(_broker_quote(bid=None), source=make_source())

    assert quote.mid is None
    assert quote.spread is None
    assert quote.last is not None


def test_a_two_sided_quote_has_an_exact_decimal_midpoint(make_source) -> None:
    quote = market_quote_from_broker(_broker_quote(), source=make_source())

    assert quote.mid == Decimal("500.15")
    assert isinstance(quote.mid, Decimal)


# ---------------------------------------------------------------------------
# Money and numeric exactness
# ---------------------------------------------------------------------------
def test_prices_are_decimals_not_floats(make_source) -> None:
    quote = market_quote_from_broker(_broker_quote(), source=make_source())

    for value in (quote.bid, quote.ask, quote.last, quote.close, quote.volume):
        assert isinstance(value, Decimal)


def test_a_binary_float_price_is_rejected(make_source, make_quote) -> None:
    """A float would import representation error into an accounting decision."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="binary floating point"):
        make_quote(bid=500.1)


def test_exact_decimals_survive_normalisation(make_source) -> None:
    quote = market_quote_from_broker(
        _broker_quote(bid=Decimal("500.10"), ask=Decimal("500.20")), source=make_source()
    )

    assert str(quote.bid) == "500.10"
    assert str(quote.ask) == "500.20"


# ---------------------------------------------------------------------------
# Enum and security-type mapping
# ---------------------------------------------------------------------------
def test_security_type_is_mapped_not_guessed(make_source) -> None:
    quote = market_quote_from_broker(
        _broker_quote(security_type=SecurityType.OPTION), source=make_source()
    )
    assert quote.security_type is SecurityType.OPTION


def test_option_right_is_preserved(make_source) -> None:
    put = option_contract_from_broker(
        _broker_option_contract(right=OptionRight.PUT), underlying="SPY"
    )
    assert put.right is OptionRight.PUT


def test_an_unparseable_expiry_keeps_the_raw_broker_string() -> None:
    """``YYYYMM`` has no day. Inventing one would fabricate an expiry."""
    from trading_system.data.models import OptionContract

    contract = OptionContract(
        underlying="ES",
        symbol="ES",
        security_type=SecurityType.FUTURE_OPTION,
        expiration=None,
        raw_last_trade_date="202609",
        strike=Decimal("5000"),
        right=OptionRight.CALL,
    )
    assert contract.expiration is None
    assert contract.raw_last_trade_date == "202609"
    assert not contract.is_fully_identified


# ---------------------------------------------------------------------------
# Provenance and origin
# ---------------------------------------------------------------------------
def test_the_broker_origin_is_carried_not_assumed(make_source) -> None:
    """Delayed data must never be relabelled realtime on the way through."""
    quote = market_quote_from_broker(
        _broker_quote(origin=MarketDataOrigin.BROKER_DELAYED),
        source=make_source(origin=MarketDataOrigin.BROKER_DELAYED),
    )

    assert quote.source.origin is MarketDataOrigin.BROKER_DELAYED
    assert not quote.source.is_live_origin


def test_provider_metadata_is_attached(make_source) -> None:
    quote = market_quote_from_broker(
        _broker_quote(),
        source=make_source(provider="IBKR", tier=SourceTier.TIER_1, source_identifier="ibkr:SPY"),
    )

    assert quote.source.provider == "IBKR"
    assert quote.source.source_tier is SourceTier.TIER_1
    assert quote.source.source_identifier == "ibkr:SPY"


def test_normalisation_produces_a_new_object_and_leaves_the_input_alone(make_source) -> None:
    broker_snapshot = _broker_quote()
    quote = market_quote_from_broker(broker_snapshot, source=make_source())

    assert id(quote) != id(broker_snapshot)
    assert broker_snapshot.bid == Decimal("500.10")
    assert broker_snapshot.source == "IBKR"


# ---------------------------------------------------------------------------
# Both volume fields survive normalisation, separately
# ---------------------------------------------------------------------------
def test_both_volume_fields_cross_the_normalisation_boundary(make_source) -> None:
    """Normalisation renames and attaches provenance. It does not do arithmetic.

    The pair here is the real SPY capture from 2026-08-15: a corrupted tick 74
    beside a clean tick 21. Both must arrive on the canonical record exactly as
    the broker sent them, still distinguishable.
    """
    quote = market_quote_from_broker(
        _broker_quote(
            volume=Decimal("31367915626456"),
            average_daily_volume=Decimal("52014430"),
        ),
        source=make_source(),
    )

    assert quote.volume == Decimal("31367915626456")
    assert quote.average_daily_volume == Decimal("52014430")


def test_a_missing_average_daily_volume_stays_missing(make_source) -> None:
    """`None` crosses as `None`; it is never filled in from the session volume."""
    quote = market_quote_from_broker(
        _broker_quote(volume=Decimal("75000000"), average_daily_volume=None),
        source=make_source(),
    )

    assert quote.average_daily_volume is None
    assert quote.volume == Decimal("75000000")


def test_normalisation_of_the_same_snapshot_is_deterministic(make_source) -> None:
    """Requirement I, at the canonical boundary."""
    source = make_source()
    snapshot = _broker_quote(
        volume=Decimal("31367915626456"), average_daily_volume=Decimal("52014430")
    )

    first = market_quote_from_broker(snapshot, source=source)
    second = market_quote_from_broker(snapshot, source=source)

    assert first.model_dump() == second.model_dump()
