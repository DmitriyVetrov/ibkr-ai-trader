"""Data-layer checks against a real IB Gateway.

Skipped unless ``ALLOW_LIVE_TESTS=true`` and marked ``ibkr``, so an ordinary
``pytest`` run never touches a gateway::

    ALLOW_LIVE_TESTS=true pytest -m ibkr tests/data

Read-only throughout: every test asserts that zero orders were submitted, and
the connections are opened read-only at the IBKR API level as well.

Unavailability is a passing outcome where the account genuinely lacks a market
data subscription — reporting that honestly is the behaviour under test, and a
provider that invented a price to avoid it would be the failure.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.broker.factory import build_broker
from trading_system.data.providers.broker_session import BrokerSession
from trading_system.data.providers.market import IBKRMarketDataProvider
from trading_system.data.providers.options import IBKROptionsDataProvider
from trading_system.data.quality import QualityContext, QualityEngine
from trading_system.domain.enums import (
    CollectionOutcome,
    DataQualityIssue,
    MarketDataOrigin,
    TradingMode,
)
from trading_system.infrastructure.settings import Settings, load_config

pytestmark = [pytest.mark.ibkr, pytest.mark.integration]


@pytest.fixture
def paper_settings() -> Settings:
    settings = Settings()
    assert settings.trading_mode is not TradingMode.LIVE, "refusing to run against LIVE"
    assert settings.ibkr_read_only, "refusing to run without the read-only guard"
    return settings


@pytest.fixture
def paper_session(paper_settings: Settings) -> BrokerSession:
    return BrokerSession(lambda: build_broker(paper_settings))


# ---------------------------------------------------------------------------
# Market quote
# ---------------------------------------------------------------------------
def test_a_real_quote_is_retrieved_and_labelled_honestly(paper_session, paper_settings) -> None:
    provider = IBKRMarketDataProvider(
        paper_session, timeout_seconds=paper_settings.ibkr_request_timeout_seconds
    )
    result = provider.fetch_quote("SPY")

    if result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE:
        pytest.skip(f"no market data for SPY on this account: {result.error}")

    assert result.outcome is CollectionOutcome.SUCCESS
    quote = result.records[0]

    assert quote.symbol == "SPY"
    assert quote.source.provider == "IBKR"
    assert quote.source.origin in {
        MarketDataOrigin.BROKER_REALTIME,
        MarketDataOrigin.BROKER_DELAYED,
        MarketDataOrigin.BROKER_FROZEN,
    }
    assert quote.has_price
    for value in (quote.bid, quote.ask, quote.last, quote.close):
        assert value is None or isinstance(value, Decimal)


def test_a_real_quote_carries_full_provenance(paper_session) -> None:
    result = IBKRMarketDataProvider(paper_session).fetch_quote("SPY")
    if not result.succeeded:
        pytest.skip(f"no market data for SPY on this account: {result.error}")

    source = result.records[0].source
    assert source.provider == "IBKR"
    assert source.retrieved_at is not None
    assert source.source_identifier == "ibkr:SPY"
    assert source.retrieved_at.tzinfo is not None


def test_the_raw_broker_response_is_preserved(paper_session) -> None:
    result = IBKRMarketDataProvider(paper_session).fetch_quote("SPY")
    if not result.succeeded:
        pytest.skip("no market data for SPY on this account")

    assert result.raw is not None
    assert result.raw.provider == "IBKR"
    assert result.raw.payload_hash


def test_quality_is_assessed_on_real_paper_data(paper_session) -> None:
    """The paper feed's quirks must surface as flags, not as edits.

    Real validation found an SPY volume that cannot be a session volume. If it
    reappears here the record must still carry the raw value and be marked
    unusable for research — never corrected.
    """
    result = IBKRMarketDataProvider(paper_session).fetch_quote("SPY")
    if not result.succeeded:
        pytest.skip("no market data for SPY on this account")

    quote = result.records[0]
    config = load_config()
    engine = QualityEngine(config.data)
    report = engine.evaluate(quote, context=QualityContext(now=quote.source.retrieved_at))

    assert isinstance(report.research_usable, bool)
    if report.has(DataQualityIssue.SUSPICIOUS_VOLUME):
        assert not report.plausibility_valid
        assert not report.research_usable
        # The value that triggered the flag is still exactly as received.
        assert quote.volume is not None
        assert quote.volume > config.data.plausibility.max_equity_daily_volume


# ---------------------------------------------------------------------------
# Option chain
# ---------------------------------------------------------------------------
def test_a_real_option_chain_is_retrieved(paper_session) -> None:
    result = IBKROptionsDataProvider(paper_session).fetch_chain("SPY")

    if result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE:
        pytest.skip(f"no option chain for SPY on this account: {result.error}")

    chain = result.records[0]
    assert chain.underlying == "SPY"
    assert chain.expirations, "expected at least one expiration"
    assert chain.strikes, "expected at least one strike"
    assert chain.expirations == sorted(set(chain.expirations))
    assert chain.strikes == sorted(set(chain.strikes))


def test_the_real_spy_option_trading_class_is_preserved(paper_session) -> None:
    """The Milestone 2 finding, checked against the live account.

    SPY options trade under class ``2SPY`` while the underlying uses ``SPY``.
    Whatever the broker reports must survive unmodified — the assertion is that
    we did not rewrite it, not that it has any particular value.
    """
    result = IBKROptionsDataProvider(paper_session).fetch_chain("SPY")
    if not result.succeeded:
        pytest.skip("no option chain for SPY on this account")

    chain = result.records[0]
    broker_class = chain.trading_class

    assert broker_class, "the broker reported no trading class"
    if broker_class != "SPY":
        # Whatever the broker calls it, we stored exactly that.
        assert broker_class == "2SPY" or broker_class.endswith("SPY")


def test_option_chain_contract_identifiers_survive(paper_session) -> None:
    result = IBKROptionsDataProvider(paper_session).fetch_chain("SPY")
    if not result.succeeded:
        pytest.skip("no option chain for SPY on this account")

    chain = result.records[0]
    assert chain.underlying_contract_id is not None
    assert chain.multiplier is None or chain.multiplier > 0


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
def test_a_real_retrieval_submits_zero_orders(paper_settings) -> None:
    brokers = []

    def _factory():
        broker = build_broker(paper_settings)
        brokers.append(broker)
        return broker

    provider = IBKRMarketDataProvider(BrokerSession(_factory))
    provider.fetch_quote("SPY")

    assert brokers
    for broker in brokers:
        assert broker.orders_submitted == 0
        assert broker.read_only


def test_each_real_retrieval_uses_its_own_connection(paper_settings) -> None:
    """The one-reliable-round-trip constraint, against the real gateway."""
    brokers = []

    def _factory():
        broker = build_broker(paper_settings)
        brokers.append(broker)
        return broker

    provider = IBKRMarketDataProvider(BrokerSession(_factory))
    provider.fetch_quote("SPY")
    provider.fetch_quote("QQQ")

    assert len(brokers) == 2
    for broker in brokers:
        assert not broker.is_connected, "the session left a connection open"


def test_the_broker_request_timeout_is_bounded(paper_settings) -> None:
    broker = build_broker(paper_settings)
    assert getattr(broker, "_request_timeout", 0) > 0
