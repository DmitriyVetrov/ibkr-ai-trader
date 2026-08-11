"""Provenance survives normalisation, storage and retrieval.

A number without a source is not evidence. The rule the specification states
plainly — never claim a source that was not actually retrieved — only holds if
provenance is carried end to end and cannot be rewritten by anything
downstream.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_system.data.models import MarketQuote
from trading_system.data.providers.base import ProviderAvailability
from trading_system.data.providers.market import SimulatedMarketDataProvider
from trading_system.data.registry import ProviderRegistry
from trading_system.data.repository import build_snapshot, records_of
from trading_system.domain.enums import DataType, MarketDataOrigin, SourceTier

pytestmark = pytest.mark.unit


def _store(repository, record, quality_engine, *, data_type=DataType.MARKET_QUOTE, key="SPY"):
    snapshot = build_snapshot(
        data_type=data_type,
        key=key,
        records=[record],
        provider=record.source.provider,
        source_tier=record.source.source_tier,
        origin=record.source.origin,
        as_of=record.as_of,
        retrieved_at=record.source.retrieved_at,
        source_timestamp=record.source.source_timestamp,
        quality=quality_engine.evaluate(record),
    )
    repository.save_snapshot(snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# Every field survives the round trip
# ---------------------------------------------------------------------------
def test_provenance_survives_storage_and_retrieval(
    repository, make_quote, quality_engine, data_now
) -> None:
    published = data_now - timedelta(hours=3)
    quote = make_quote(
        provider="IBKR",
        tier=SourceTier.TIER_1,
        source_identifier="ibkr:SPY",
        published_at=published,
        source_timestamp=data_now,
    )
    _store(repository, quote, quality_engine)

    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None
    restored = records_of(stored, MarketQuote)[0]

    assert restored.source.provider == "IBKR"
    assert restored.source.source_tier is SourceTier.TIER_1
    assert restored.source.source_identifier == "ibkr:SPY"
    assert restored.source.published_at == published
    assert restored.source.retrieved_at == quote.source.retrieved_at
    assert restored.source.origin is quote.source.origin


def test_the_snapshot_itself_carries_provider_and_tier(
    repository, make_quote, quality_engine
) -> None:
    quote = make_quote(provider="SEC_EDGAR", tier=SourceTier.TIER_1)
    snapshot = _store(repository, quote, quality_engine)

    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None
    assert stored.provider == "SEC_EDGAR"
    assert stored.source_tier is SourceTier.TIER_1
    assert stored.data_origin is snapshot.data_origin


@pytest.mark.parametrize(
    "tier",
    [SourceTier.TIER_1, SourceTier.TIER_2, SourceTier.TIER_3, SourceTier.TIER_4],
)
def test_every_tier_round_trips(repository, make_quote, quality_engine, tier) -> None:
    """Tier is a stored attribute, not a runtime guess."""
    quote = make_quote(tier=tier)
    _store(repository, quote, quality_engine, key=f"SPY-{tier.value}")

    stored = repository.get_latest(DataType.MARKET_QUOTE, f"SPY-{tier.value}")
    assert stored is not None
    assert stored.source_tier is tier


def test_a_lower_tier_is_stored_not_rejected(repository, make_quote, quality_engine) -> None:
    """Tier is a trust ranking, not a truth test — Tier 4 data still stores."""
    quote = make_quote(tier=SourceTier.TIER_4)
    _store(repository, quote, quality_engine)

    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None
    assert stored.source_tier is SourceTier.TIER_4
    assert stored.data_quality.research_usable


# ---------------------------------------------------------------------------
# Fallback attribution
# ---------------------------------------------------------------------------
class _AlwaysUnavailable(SimulatedMarketDataProvider):
    provider_id = "PROVIDER_A"
    display_name = "Provider A"

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability.UNAVAILABLE


def test_a_fallback_names_the_provider_that_actually_answered(data_clock) -> None:
    """Provider B's data must never be filed under provider A's name."""
    registry = ProviderRegistry()
    registry.register(_AlwaysUnavailable(clock=data_clock))
    registry.register(SimulatedMarketDataProvider(clock=data_clock))

    result = registry.fetch_with_fallback(
        DataType.MARKET_QUOTE,
        lambda provider: provider.fetch_quote("SPY"),  # type: ignore[attr-defined]
        key="SPY",
    )

    assert result.succeeded
    quote = result.records[0]
    assert quote.source.provider == "SIMULATOR"
    assert quote.source.requested_provider == "PROVIDER_A"
    assert quote.source.used_fallback


def test_a_fallback_is_recorded_in_storage_too(repository, data_clock, quality_engine) -> None:
    registry = ProviderRegistry()
    registry.register(_AlwaysUnavailable(clock=data_clock))
    registry.register(SimulatedMarketDataProvider(clock=data_clock))
    result = registry.fetch_with_fallback(
        DataType.MARKET_QUOTE,
        lambda provider: provider.fetch_quote("SPY"),  # type: ignore[attr-defined]
        key="SPY",
    )
    _store(repository, result.records[0], quality_engine)

    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None
    restored = records_of(stored, MarketQuote)[0]

    assert stored.provider == "SIMULATOR"
    assert restored.source.requested_provider == "PROVIDER_A"


def test_no_fallback_means_no_requested_provider(data_clock) -> None:
    registry = ProviderRegistry()
    registry.register(SimulatedMarketDataProvider(clock=data_clock))

    result = registry.fetch_with_fallback(
        DataType.MARKET_QUOTE,
        lambda provider: provider.fetch_quote("SPY"),  # type: ignore[attr-defined]
        key="SPY",
    )
    assert result.records[0].source.requested_provider is None
    assert not result.records[0].source.used_fallback


# ---------------------------------------------------------------------------
# Field-level provenance for merged records
# ---------------------------------------------------------------------------
def test_field_level_provenance_round_trips(repository, make_quote, quality_engine) -> None:
    """Merged records must say which source produced which field."""
    quote = make_quote(field_provenance={"volume": "PROVIDER_X", "bid": "IBKR"})
    _store(repository, quote, quality_engine)

    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None
    restored = records_of(stored, MarketQuote)[0]

    assert restored.source.field_provenance == {"volume": "PROVIDER_X", "bid": "IBKR"}


def test_an_unmerged_record_carries_no_field_provenance(make_quote) -> None:
    """Empty means "everything came from ``provider``", which is the norm."""
    assert make_quote().source.field_provenance == {}


# ---------------------------------------------------------------------------
# Origin honesty
# ---------------------------------------------------------------------------
def test_cached_data_is_relabelled_on_the_way_out(make_quote) -> None:
    from trading_system.data.cache import as_cached

    live = make_quote(origin=MarketDataOrigin.BROKER_REALTIME)
    replayed = as_cached(live)

    assert replayed.source.origin is MarketDataOrigin.CACHED
    assert not replayed.source.is_live_origin
    # The original is untouched, and the ages still match.
    assert live.source.origin is MarketDataOrigin.BROKER_REALTIME
    assert replayed.source.retrieved_at == live.source.retrieved_at


def test_simulated_data_can_never_be_read_as_a_broker_quote(data_clock) -> None:
    quote = SimulatedMarketDataProvider(clock=data_clock).fetch_quote("SPY").records[0]

    assert quote.source.origin is MarketDataOrigin.SIMULATED
    assert quote.source.provider == "SIMULATOR"
    assert (quote.source.source_identifier or "").startswith("simulator:")
