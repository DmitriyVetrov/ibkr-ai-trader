"""Market data provider behaviour, without a gateway.

The IBKR provider is exercised against fake brokers rather than a live
connection: the point under test is what the *provider* does with a broker
response, not whether IBKR is reachable. The gateway-backed checks live in
``test_ibkr_paper.py``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.broker.base import (
    BrokerConnectionError,
    BrokerTimeoutError,
)
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.data.providers.broker_session import BrokerSession
from trading_system.data.providers.market import (
    IBKRMarketDataProvider,
    SimulatedMarketDataProvider,
)
from trading_system.domain.enums import (
    BarInterval,
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    SecurityType,
)

pytestmark = pytest.mark.unit


class _FailingBroker(SimulatedBroker):
    """A simulator that fails the way a real broker fails."""

    def __init__(self, error: Exception, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._error = error

    def get_market_data(self, symbol: str, security_type=SecurityType.STOCK):
        raise self._error


def _session(broker_factory) -> BrokerSession:
    return BrokerSession(broker_factory)


# ---------------------------------------------------------------------------
# Valid response
# ---------------------------------------------------------------------------
def test_a_valid_broker_quote_becomes_a_canonical_record(data_clock) -> None:
    provider = IBKRMarketDataProvider(
        _session(lambda: SimulatedBroker(clock=data_clock)), clock=data_clock
    )
    result = provider.fetch_quote("SPY")

    assert result.outcome is CollectionOutcome.SUCCESS
    assert result.record_count == 1
    quote = result.records[0]
    assert quote.symbol == "SPY"
    assert isinstance(quote.bid, Decimal)
    assert quote.source.provider == "IBKR"
    assert quote.source.retrieved_at == data_clock.now()


def test_the_raw_response_is_preserved_alongside_the_record(data_clock) -> None:
    provider = IBKRMarketDataProvider(
        _session(lambda: SimulatedBroker(clock=data_clock)), clock=data_clock
    )
    result = provider.fetch_quote("SPY")

    assert result.raw is not None
    assert result.raw.provider == "IBKR"
    assert result.raw.data_type is DataType.MARKET_QUOTE
    assert result.raw.payload_hash
    assert "bid" in result.raw.payload


def test_the_origin_comes_from_the_response_not_from_the_provider(data_clock) -> None:
    """A provider defaulting to "delayed" must still report simulated as simulated."""
    provider = IBKRMarketDataProvider(
        _session(lambda: SimulatedBroker(clock=data_clock)), clock=data_clock
    )
    assert provider.origin is MarketDataOrigin.BROKER_DELAYED

    quote = provider.fetch_quote("SPY").records[0]
    assert quote.source.origin is MarketDataOrigin.SIMULATED


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_unavailable_market_data_yields_no_record_and_no_invented_price(data_clock) -> None:
    broker = SimulatedBroker(SimulatedBrokerState(unquotable_symbols={"ZZZZ"}), clock=data_clock)
    provider = IBKRMarketDataProvider(_session(lambda: broker), clock=data_clock)

    result = provider.fetch_quote("ZZZZ")

    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert result.records == ()
    assert "MARKET_DATA_UNAVAILABLE" in (result.error or "")


def test_a_broker_timeout_is_reported_as_provider_unavailable(data_clock) -> None:
    broker = _FailingBroker(BrokerTimeoutError("no answer"), clock=data_clock)
    provider = IBKRMarketDataProvider(_session(lambda: broker), clock=data_clock)

    result = provider.fetch_quote("SPY")

    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert "timed out" in (result.error or "")
    assert result.records == ()


def test_a_disconnected_broker_is_reported_not_raised(data_clock) -> None:
    broker = SimulatedBroker(clock=data_clock, fail_to_connect=True)
    provider = IBKRMarketDataProvider(_session(lambda: broker), clock=data_clock)

    result = provider.fetch_quote("SPY")

    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert result.records == ()


def test_an_unexpected_broker_error_is_reported_as_invalid_data(data_clock) -> None:
    broker = _FailingBroker(BrokerConnectionError("socket closed"), clock=data_clock)
    provider = IBKRMarketDataProvider(_session(lambda: broker), clock=data_clock)

    result = provider.fetch_quote("SPY")
    assert not result.succeeded
    assert result.records == ()


def test_a_provider_that_cannot_build_a_broker_is_unavailable(data_clock) -> None:
    from trading_system.broker.base import BrokerConfigurationError

    def _refuse():
        raise BrokerConfigurationError("no gateway configured")

    provider = IBKRMarketDataProvider(_session(_refuse), clock=data_clock)

    assert provider.availability().value == "UNAVAILABLE"
    assert provider.fetch_quote("SPY").outcome is CollectionOutcome.PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Bars are honestly unsupported
# ---------------------------------------------------------------------------
def test_historical_bars_report_no_data_rather_than_being_synthesised(data_clock) -> None:
    provider = IBKRMarketDataProvider(
        _session(lambda: SimulatedBroker(clock=data_clock)), clock=data_clock
    )
    result = provider.fetch_bars("SPY", BarInterval.DAY_1)

    assert result.outcome is CollectionOutcome.NO_DATA
    assert result.records == ()
    assert "does not supply historical bars" in (result.error or "")


# ---------------------------------------------------------------------------
# The simulated provider
# ---------------------------------------------------------------------------
def test_the_simulator_labels_everything_it_produces(data_clock) -> None:
    """Synthetic data must be unmistakable at every layer."""
    result = SimulatedMarketDataProvider(clock=data_clock).fetch_quote("SPY")
    quote = result.records[0]

    assert quote.source.provider == "SIMULATOR"
    assert quote.source.origin is MarketDataOrigin.SIMULATED
    assert quote.source.origin not in {
        MarketDataOrigin.BROKER_REALTIME,
        MarketDataOrigin.BROKER_DELAYED,
        MarketDataOrigin.PROVIDER_REALTIME,
    }
    assert result.raw is not None
    assert "SIMULATED" in result.raw.notes[0]


def test_the_simulator_is_deterministic(data_clock) -> None:
    provider = SimulatedMarketDataProvider(clock=data_clock)

    first = provider.fetch_quote("SPY").records[0]
    second = provider.fetch_quote("SPY").records[0]

    assert first.bid == second.bid
    assert first.last == second.last


def test_the_simulator_needs_no_broker_and_no_network(data_clock) -> None:
    provider = SimulatedMarketDataProvider(clock=data_clock)

    assert not provider.requires_broker
    assert not provider.requires_network
    assert provider.availability().value == "AVAILABLE"


# ---------------------------------------------------------------------------
# Timestamp handling
# ---------------------------------------------------------------------------
def test_retrieval_and_observation_times_are_recorded_separately(data_clock) -> None:
    result = SimulatedMarketDataProvider(clock=data_clock).fetch_quote("SPY")
    source = result.records[0].source

    assert source.retrieved_at is not None
    assert source.source_timestamp is not None
    assert source.observed_at is not None


def test_the_record_reports_its_own_age(data_clock, data_now) -> None:
    quote = SimulatedMarketDataProvider(clock=data_clock).fetch_quote("SPY").records[0]

    assert quote.source.age_seconds(data_now) == pytest.approx(0.0, abs=1.0)
    assert quote.source.age_seconds(data_now + timedelta(minutes=5)) == pytest.approx(300.0, abs=1)
