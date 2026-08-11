"""Provider registry and explicit fallback.

The registry exists so no consumer hard-codes a provider. Its selection has to
be deterministic — same registry, same environment, same choice — and its
fallback has to be loud about itself, because a silent fallback is
indistinguishable from a lie about where data came from.
"""

from __future__ import annotations

import pytest

from trading_system.data.providers.base import (
    ProviderAvailability,
    ProviderError,
    ProviderUnavailableError,
)
from trading_system.data.providers.market import SimulatedMarketDataProvider
from trading_system.data.providers.news import FixtureNewsProvider
from trading_system.data.providers.options import SimulatedOptionsDataProvider
from trading_system.data.registry import ProviderRegistry
from trading_system.domain.enums import CollectionOutcome, DataType

pytestmark = pytest.mark.unit


class _NamedProvider(SimulatedMarketDataProvider):
    def __init__(self, provider_id: str, available: bool = True, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.provider_id = provider_id
        self._available = available
        self.calls = 0

    def availability(self) -> ProviderAvailability:
        return (
            ProviderAvailability.AVAILABLE if self._available else ProviderAvailability.UNAVAILABLE
        )

    def fetch_quote(self, symbol, security_type=None):
        self.calls += 1
        return super().fetch_quote(symbol)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_providers_can_be_registered_and_retrieved(data_clock) -> None:
    registry = ProviderRegistry()
    market = registry.register(SimulatedMarketDataProvider(clock=data_clock))

    assert registry.get("SIMULATOR") is market
    assert len(registry) == 1
    assert list(registry) == [market]


def test_several_providers_can_serve_one_data_type(data_clock) -> None:
    registry = ProviderRegistry()
    registry.register(_NamedProvider("A", clock=data_clock))
    registry.register(_NamedProvider("B", clock=data_clock))

    assert [p.provider_id for p in registry.for_data_type(DataType.MARKET_QUOTE)] == ["A", "B"]


def test_a_duplicate_provider_id_for_the_same_type_is_refused(data_clock) -> None:
    """Two objects answering to one name make stored provenance ambiguous."""
    registry = ProviderRegistry()
    registry.register(_NamedProvider("A", clock=data_clock))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_NamedProvider("A", clock=data_clock))


def test_the_same_id_may_serve_different_data_types(data_clock) -> None:
    """IBKR is one source of both quotes and chains, and that is fine."""
    registry = ProviderRegistry()
    registry.register(SimulatedMarketDataProvider(clock=data_clock))
    registry.register(SimulatedOptionsDataProvider(clock=data_clock))

    assert len(registry) == 2
    assert registry.for_data_type(DataType.OPTION_CHAIN)


def test_an_unregistered_id_raises(data_clock) -> None:
    with pytest.raises(KeyError):
        ProviderRegistry().get("NOPE")


def test_a_provider_without_an_id_is_refused(data_clock) -> None:
    provider = SimulatedMarketDataProvider(clock=data_clock)
    provider.provider_id = ""

    with pytest.raises(ValueError, match="provider_id"):
        ProviderRegistry().register(provider)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def test_selection_is_deterministic(data_clock) -> None:
    registry = ProviderRegistry()
    registry.register(_NamedProvider("A", clock=data_clock))
    registry.register(_NamedProvider("B", clock=data_clock))

    chosen = {registry.select(DataType.MARKET_QUOTE).provider.provider_id for _ in range(5)}
    assert chosen == {"A"}


def test_selection_skips_unavailable_providers(data_clock) -> None:
    registry = ProviderRegistry()
    registry.register(_NamedProvider("A", available=False, clock=data_clock))
    registry.register(_NamedProvider("B", clock=data_clock))

    selection = registry.select(DataType.MARKET_QUOTE)

    assert selection.provider.provider_id == "B"
    assert [s.provider_id for s in selection.skipped] == ["A"]


def test_a_preferred_provider_wins(data_clock) -> None:
    registry = ProviderRegistry()
    registry.register(_NamedProvider("A", clock=data_clock))
    registry.register(_NamedProvider("B", clock=data_clock))

    assert registry.select(DataType.MARKET_QUOTE, preferred="B").provider.provider_id == "B"


def test_no_provider_for_a_data_type_is_an_error_not_a_none(data_clock) -> None:
    """ "No provider" and "no data" must not be confused for one another."""
    with pytest.raises(ProviderUnavailableError, match="no provider is registered"):
        ProviderRegistry().select(DataType.OPTION_CHAIN)


def test_all_providers_unavailable_is_an_error(data_clock) -> None:
    registry = ProviderRegistry()
    registry.register(_NamedProvider("A", available=False, clock=data_clock))

    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        registry.select(DataType.MARKET_QUOTE)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------
def test_fallback_uses_the_next_provider(data_clock) -> None:
    registry = ProviderRegistry()
    first = _NamedProvider("A", available=False, clock=data_clock)
    second = _NamedProvider("B", clock=data_clock)
    registry.register(first)
    registry.register(second)

    result = registry.fetch_with_fallback(
        DataType.MARKET_QUOTE,
        lambda provider: provider.fetch_quote("SPY"),  # type: ignore[attr-defined]
        key="SPY",
    )

    assert result.succeeded
    assert first.calls == 0
    assert second.calls == 1
    assert result.records[0].source.requested_provider == "A"


def test_fallback_records_why_each_provider_was_skipped(data_clock) -> None:
    registry = ProviderRegistry()
    registry.register(_NamedProvider("A", available=False, clock=data_clock))
    registry.register(_NamedProvider("B", clock=data_clock))

    result = registry.fetch_with_fallback(
        DataType.MARKET_QUOTE,
        lambda provider: provider.fetch_quote("SPY"),  # type: ignore[attr-defined]
        key="SPY",
    )
    assert any("A" in note for note in result.notes)


def test_a_raising_provider_does_not_stop_the_fallback(data_clock) -> None:
    class _Raiser(_NamedProvider):
        def fetch_quote(self, symbol, security_type=None):
            raise ProviderError("boom")

    registry = ProviderRegistry()
    registry.register(_Raiser("A", clock=data_clock))
    registry.register(_NamedProvider("B", clock=data_clock))

    result = registry.fetch_with_fallback(
        DataType.MARKET_QUOTE,
        lambda provider: provider.fetch_quote("SPY"),  # type: ignore[attr-defined]
        key="SPY",
    )
    assert result.succeeded
    assert result.provider_id == "B"


def test_every_provider_failing_yields_a_failure_result(data_clock) -> None:
    registry = ProviderRegistry()
    registry.register(_NamedProvider("A", available=False, clock=data_clock))
    registry.register(_NamedProvider("B", available=False, clock=data_clock))

    result = registry.fetch_with_fallback(
        DataType.MARKET_QUOTE,
        lambda provider: provider.fetch_quote("SPY"),  # type: ignore[attr-defined]
        key="SPY",
    )

    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert result.records == ()
    assert "A" in (result.error or "") and "B" in (result.error or "")


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------
def test_describe_reports_every_provider(data_clock, tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register(SimulatedMarketDataProvider(clock=data_clock))
    registry.register(FixtureNewsProvider(tmp_path, clock=data_clock))

    descriptions = registry.describe()
    assert {d.provider_id for d in descriptions} == {"SIMULATOR", "FIXTURE_NEWS"}
    assert all(d.cost.value != "PAID" for d in descriptions)
