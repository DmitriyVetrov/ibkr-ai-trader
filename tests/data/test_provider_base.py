"""Provider abstractions: what a provider is allowed to be.

The boundary matters more than the mechanics. A provider retrieves; it does not
rank, classify, size or trade. These tests pin that down structurally so a
later provider cannot quietly grow a decision-making method.
"""

from __future__ import annotations

import inspect

import pytest

from trading_system.data.models import MarketQuote
from trading_system.data.providers import base as provider_base
from trading_system.data.providers.base import (
    DataProvider,
    ProviderAvailability,
    ProviderCost,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
    failed_result,
    successful_result,
)
from trading_system.data.providers.market import (
    MarketDataProvider,
    SimulatedMarketDataProvider,
)
from trading_system.data.providers.news import NewsProvider
from trading_system.data.providers.options import OptionsDataProvider
from trading_system.domain.enums import (
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    SourceTier,
)

pytestmark = pytest.mark.unit

#: Every provider interface the specification requires to exist.
PROVIDER_INTERFACES = [
    MarketDataProvider,
    OptionsDataProvider,
    NewsProvider,
]


# ---------------------------------------------------------------------------
# Identity and metadata
# ---------------------------------------------------------------------------
def test_a_provider_declares_identity_tier_and_cost(data_clock) -> None:
    description = SimulatedMarketDataProvider(clock=data_clock).describe()

    assert description.provider_id == "SIMULATOR"
    assert isinstance(description.tier, SourceTier)
    assert isinstance(description.cost, ProviderCost)
    assert isinstance(description.availability, ProviderAvailability)
    assert DataType.MARKET_QUOTE in description.data_types


def test_a_provider_stamps_itself_on_the_records_it_produces(data_clock, data_now) -> None:
    provider = SimulatedMarketDataProvider(clock=data_clock)
    metadata = provider.metadata(retrieved_at=data_now)

    assert metadata.provider == "SIMULATOR"
    assert metadata.source_tier is provider.tier
    assert metadata.origin is MarketDataOrigin.SIMULATED
    assert metadata.retrieved_at == data_now


def test_a_fallback_records_who_was_asked_first(data_clock, data_now) -> None:
    """``provider`` always names who answered. Never who we hoped would."""
    provider = SimulatedMarketDataProvider(clock=data_clock)
    metadata = provider.metadata(retrieved_at=data_now, requested_provider="IBKR")

    assert metadata.provider == "SIMULATOR"
    assert metadata.requested_provider == "IBKR"
    assert metadata.used_fallback


# ---------------------------------------------------------------------------
# Bounded requests
# ---------------------------------------------------------------------------
def test_a_provider_must_have_a_positive_timeout() -> None:
    """There is no "wait forever" setting, because that is how a runtime hangs."""
    with pytest.raises(ValueError, match="unbounded requests are refused"):
        SimulatedMarketDataProvider(timeout_seconds=0)

    with pytest.raises(ValueError, match="unbounded"):
        SimulatedMarketDataProvider(timeout_seconds=-1)


def test_the_timeout_is_exposed(data_clock) -> None:
    assert SimulatedMarketDataProvider(clock=data_clock, timeout_seconds=7.5).timeout_seconds == 7.5


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def test_a_result_with_records_is_a_success(make_quote) -> None:
    result = successful_result(
        provider_id="SIMULATOR",
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        records=[make_quote()],
    )
    assert result.outcome is CollectionOutcome.SUCCESS
    assert result.succeeded
    assert result.record_count == 1


def test_an_empty_result_is_no_data_not_success() -> None:
    """ "Nothing to report" and "we got what we asked for" are different facts."""
    result: ProviderResult[MarketQuote] = successful_result(
        provider_id="SIMULATOR",
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        records=[],
    )
    assert result.outcome is CollectionOutcome.NO_DATA
    assert not result.succeeded


def test_a_partial_result_is_marked_partial(make_quote) -> None:
    result = successful_result(
        provider_id="FIXTURE_NEWS",
        data_type=DataType.NEWS_ARTICLE,
        key="NVDA",
        records=[make_quote()],
        partial=True,
        notes=["one entry was unusable"],
    )
    assert result.outcome is CollectionOutcome.PARTIAL_SUCCESS
    assert result.succeeded
    assert result.notes == ("one entry was unusable",)


@pytest.mark.parametrize(
    "outcome",
    [
        CollectionOutcome.PROVIDER_UNAVAILABLE,
        CollectionOutcome.TIMEOUT,
        CollectionOutcome.INVALID_DATA,
        CollectionOutcome.NO_DATA,
    ],
)
def test_a_failed_result_carries_no_records(outcome: CollectionOutcome) -> None:
    """A failure never smuggles a partial or substituted record through."""
    result: ProviderResult[MarketQuote] = failed_result(
        provider_id="IBKR",
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        outcome=outcome,
        error="something went wrong",
    )
    assert result.records == ()
    assert not result.succeeded
    assert result.error


def test_every_failure_mode_has_its_own_error_type() -> None:
    """Callers fail safe on the specific thing, not on message text."""
    assert issubclass(ProviderTimeoutError, ProviderUnavailableError)
    assert issubclass(ProviderRateLimitError, ProviderUnavailableError)
    assert issubclass(ProviderUnavailableError, ProviderError)
    assert issubclass(ProviderResponseError, ProviderError)
    assert not issubclass(ProviderResponseError, ProviderUnavailableError)


# ---------------------------------------------------------------------------
# The boundary: providers retrieve, they do not decide
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("interface", PROVIDER_INTERFACES, ids=lambda i: i.__name__)
def test_no_provider_interface_exposes_a_decision_method(interface: type) -> None:
    forbidden = (
        "rank",
        "score",
        "select",
        "recommend",
        "classify",
        "allocate",
        "size",
        "place_order",
        "submit",
        "buy",
        "sell",
    )
    methods = [name for name, _ in inspect.getmembers(interface, inspect.isfunction)]
    offenders = [name for name in methods if any(name.startswith(word) for word in forbidden)]
    assert offenders == [], f"{interface.__name__} exposes decision methods: {offenders}"


@pytest.mark.parametrize("interface", PROVIDER_INTERFACES, ids=lambda i: i.__name__)
def test_every_provider_interface_declares_its_data_types(interface: type) -> None:
    assert "data_types" in dir(interface)
    assert issubclass(interface, DataProvider)


def test_the_provider_module_does_not_import_the_broker_library() -> None:
    """Broker specifics stay inside broker/ibkr/, including here."""
    source = inspect.getsource(provider_base)
    assert "ib_async" not in source


def test_no_paid_cost_is_used_by_any_shipped_provider(data_clock) -> None:
    from trading_system.data.providers.news import FixtureNewsProvider
    from trading_system.data.providers.options import SimulatedOptionsDataProvider

    providers = [
        SimulatedMarketDataProvider(clock=data_clock),
        SimulatedOptionsDataProvider(clock=data_clock),
        FixtureNewsProvider("/nonexistent", clock=data_clock),
    ]
    assert all(p.cost is not ProviderCost.PAID for p in providers)
