"""Cache: disposable, honest about expiry, never mistaken for history.

The failure this suite guards against is the cache quietly becoming a data
source — a stale price served as current during an outage, or a cached entry
treated as the historical record. Both are worse than having no cache at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_system.data.cache import DataCache, as_cached
from trading_system.domain.enums import MarketDataOrigin

pytestmark = pytest.mark.unit


@pytest.fixture
def cache(tmp_path, data_clock) -> DataCache:
    return DataCache(tmp_path / "data" / "cache", clock=data_clock, default_ttl_seconds=300)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------
def test_a_stored_entry_is_returned(cache) -> None:
    cache.put("quotes", "SPY", {"bid": "500.10"})
    entry = cache.get("quotes", "SPY")

    assert entry is not None
    assert entry.payload == {"bid": "500.10"}


def test_a_miss_is_none(cache) -> None:
    assert cache.get("quotes", "NOPE") is None


def test_an_expired_entry_is_a_miss_not_a_stale_hit(cache, data_clock) -> None:
    """During an outage the cache must say "nothing", not "yesterday"."""
    cache.put("quotes", "SPY", {"bid": "500.10"}, ttl_seconds=60)
    assert cache.get("quotes", "SPY") is not None

    data_clock.advance(seconds=61)
    assert cache.get("quotes", "SPY") is None


def test_a_corrupt_entry_is_a_miss(cache, tmp_path) -> None:
    cache.put("quotes", "SPY", {"bid": "1"})
    stored = next((tmp_path / "data" / "cache").rglob("*.json"))
    stored.write_text("{not json", encoding="utf-8")

    assert cache.get("quotes", "SPY") is None


def test_a_disabled_cache_stores_and_returns_nothing(tmp_path, data_clock) -> None:
    disabled = DataCache(tmp_path / "cache", clock=data_clock, enabled=False)

    assert disabled.put("quotes", "SPY", {"bid": "1"}) is None
    assert disabled.get("quotes", "SPY") is None


# ---------------------------------------------------------------------------
# Disposability
# ---------------------------------------------------------------------------
def test_the_cache_can_be_cleared_at_any_time(cache) -> None:
    cache.put("quotes", "SPY", {"bid": "1"})
    cache.put("chains", "SPY", {"strikes": []})

    assert cache.clear() == 2
    assert cache.get("quotes", "SPY") is None


def test_clearing_one_namespace_leaves_the_others(cache) -> None:
    cache.put("quotes", "SPY", {"bid": "1"})
    cache.put("chains", "SPY", {"strikes": []})

    assert cache.clear("quotes") == 1
    assert cache.get("chains", "SPY") is not None


def test_an_entry_can_be_invalidated(cache) -> None:
    cache.put("quotes", "SPY", {"bid": "1"})
    cache.invalidate("quotes", "SPY")

    assert cache.get("quotes", "SPY") is None


# ---------------------------------------------------------------------------
# Cache is not history
# ---------------------------------------------------------------------------
def test_the_cache_lives_outside_the_snapshot_and_historical_areas(
    cache, repository, tmp_path
) -> None:
    """Cache, snapshots and history must never share a directory."""
    cache.put("quotes", "SPY", {"bid": "1"})

    cached_files = list((tmp_path / "data" / "cache").rglob("*.json"))
    assert cached_files
    for path in cached_files:
        assert "snapshots" not in path.parts
        assert "historical" not in path.parts
        assert "raw" not in path.parts


def test_replayed_data_is_relabelled_cached(make_quote) -> None:
    """A realtime quote replayed from cache is not a realtime quote."""
    live = make_quote(origin=MarketDataOrigin.BROKER_REALTIME)
    replayed = as_cached(live)

    assert replayed.source.origin is MarketDataOrigin.CACHED
    assert not replayed.source.is_live_origin


def test_relabelling_does_not_disguise_the_age(make_quote, data_now) -> None:
    """Cached data is exactly as old as it was; the label makes that visible."""
    live = make_quote(origin=MarketDataOrigin.BROKER_REALTIME)
    replayed = as_cached(live)

    assert replayed.source.retrieved_at == live.source.retrieved_at
    assert replayed.source.source_timestamp == live.source.source_timestamp
    assert replayed.as_of == live.as_of
    assert replayed.source.age_seconds(data_now + timedelta(minutes=10)) == pytest.approx(
        live.source.age_seconds(data_now + timedelta(minutes=10))
    )


def test_relabelling_leaves_the_original_alone(make_quote) -> None:
    live = make_quote(origin=MarketDataOrigin.BROKER_REALTIME)
    as_cached(live)

    assert live.source.origin is MarketDataOrigin.BROKER_REALTIME


def test_cached_data_is_flagged_stale_once_the_window_passes(
    quality_engine, make_quote, data_now
) -> None:
    """Relabelling as cached does not exempt a record from freshness checks."""
    from trading_system.data.quality import QualityContext
    from trading_system.domain.enums import DataQualityIssue

    old = data_now - timedelta(hours=4)
    replayed = as_cached(make_quote(as_of=old, retrieved_at=old, source_timestamp=old))
    report = quality_engine.evaluate(replayed, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.STALE_DATA)
