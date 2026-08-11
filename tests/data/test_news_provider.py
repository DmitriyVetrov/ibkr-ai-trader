"""News and corporate-event providers.

No live news provider exists in Milestone 3 — free structured news from Tier 1
or Tier 2 sources is not available, and inventing one is explicitly not the
answer. What is tested here is the interface plus the fixture replay that keeps
the whole downstream path exercised.

The behaviour that matters most: an article that cannot be attributed or placed
in time is rejected rather than stored, because it could never be cited and
could never be filtered point-in-time.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_system.data.providers.news import (
    CorporateEventProvider,
    FixtureCorporateEventProvider,
    FixtureNewsProvider,
    NewsProvider,
)
from trading_system.domain.enums import (
    CollectionOutcome,
    CorporateEventType,
    DataType,
    MarketDataOrigin,
    SourceTier,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Valid response
# ---------------------------------------------------------------------------
def test_recorded_articles_become_canonical_records(news_fixture_dir, data_clock) -> None:
    result = FixtureNewsProvider(news_fixture_dir, clock=data_clock).fetch_news("NVDA")

    assert result.succeeded
    assert result.record_count == 2
    article = result.records[0]
    assert article.headline
    assert article.symbols == ["NVDA"]
    assert article.source.published_at is not None
    assert (article.source.source_identifier or "").startswith("https://")


def test_each_article_keeps_its_publishers_tier_not_the_replayers(
    news_fixture_dir, data_clock
) -> None:
    """The replay mechanism is Tier 4; Reuters is not."""
    provider = FixtureNewsProvider(news_fixture_dir, clock=data_clock)
    assert provider.tier is SourceTier.TIER_4

    article = provider.fetch_news("NVDA").records[0]
    assert article.source.source_tier is SourceTier.TIER_2
    assert article.source.source_name == "Reuters"


def test_the_replay_is_labelled_historical_never_realtime(news_fixture_dir, data_clock) -> None:
    article = FixtureNewsProvider(news_fixture_dir, clock=data_clock).fetch_news("NVDA").records[0]

    assert article.source.origin is MarketDataOrigin.HISTORICAL
    assert not article.source.is_live_origin


# ---------------------------------------------------------------------------
# Malformed entries
# ---------------------------------------------------------------------------
def test_an_article_without_a_publication_time_is_rejected(news_fixture_dir, data_clock) -> None:
    """An article with no timestamp cannot be placed on a timeline."""
    result = FixtureNewsProvider(news_fixture_dir, clock=data_clock).fetch_news("NVDA")

    assert result.outcome is CollectionOutcome.PARTIAL_SUCCESS
    assert any("publication time" in note for note in result.notes)
    assert all(a.source.published_at is not None for a in result.records)


def test_a_naive_publication_time_is_rejected(tmp_path, data_clock) -> None:
    """ "2026-08-09 13:05" has no defined position on the timeline."""
    import json

    path = tmp_path / "news"
    path.mkdir()
    (path / "ACME.json").write_text(
        json.dumps(
            [
                {
                    "headline": "Naive timestamp",
                    "url": "https://example.com/a",
                    "published_at": "2026-08-09T13:05:00",
                    "source_name": "Example",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = FixtureNewsProvider(path, clock=data_clock).fetch_news("ACME")
    assert result.outcome is CollectionOutcome.INVALID_DATA
    assert result.records == ()


def test_a_corrupt_fixture_is_reported_not_silently_skipped(tmp_path, data_clock) -> None:
    path = tmp_path / "news"
    path.mkdir()
    (path / "ACME.json").write_text("{not json", encoding="utf-8")

    result = FixtureNewsProvider(path, clock=data_clock).fetch_news("ACME")
    assert result.outcome is CollectionOutcome.INVALID_DATA
    assert "corrupt" in (result.error or "")


def test_a_missing_fixture_is_no_data_not_an_error(news_fixture_dir, data_clock) -> None:
    result = FixtureNewsProvider(news_fixture_dir, clock=data_clock).fetch_news("UNKNOWN")

    assert result.outcome is CollectionOutcome.NO_DATA
    assert result.records == ()


def test_a_missing_directory_makes_the_provider_unavailable(data_clock) -> None:
    provider = FixtureNewsProvider("/nonexistent/path", clock=data_clock)
    assert provider.availability().value == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def test_since_filters_by_publication_time(news_fixture_dir, data_clock) -> None:
    cutoff = datetime(2026, 8, 10, tzinfo=UTC)
    result = FixtureNewsProvider(news_fixture_dir, clock=data_clock).fetch_news(
        "NVDA", since=cutoff
    )

    assert result.record_count == 1
    published_at = result.records[0].source.published_at
    assert published_at is not None and published_at >= cutoff


def test_limit_is_respected(news_fixture_dir, data_clock) -> None:
    result = FixtureNewsProvider(news_fixture_dir, clock=data_clock).fetch_news("NVDA", limit=1)
    assert result.record_count == 1


# ---------------------------------------------------------------------------
# No interpretation
# ---------------------------------------------------------------------------
def test_the_provider_attaches_no_sentiment_or_classification(news_fixture_dir, data_clock) -> None:
    """Reading meaning into an article is the research agent's job."""
    article = FixtureNewsProvider(news_fixture_dir, clock=data_clock).fetch_news("NVDA").records[0]
    fields = set(article.model_dump())

    for forbidden in ("sentiment", "hypothesis", "direction", "score", "recommendation"):
        assert forbidden not in fields


def test_relevance_is_only_carried_when_the_source_supplied_it(tmp_path, data_clock) -> None:
    import json

    path = tmp_path / "news"
    path.mkdir()
    (path / "ACME.json").write_text(
        json.dumps(
            [
                {
                    "headline": "No relevance supplied",
                    "url": "https://example.com/a",
                    "published_at": "2026-08-09T13:05:00+00:00",
                    "source_name": "Example",
                }
            ]
        ),
        encoding="utf-8",
    )

    article = FixtureNewsProvider(path, clock=data_clock).fetch_news("ACME").records[0]
    assert article.relevance is None


# ---------------------------------------------------------------------------
# Corporate events
# ---------------------------------------------------------------------------
def test_recorded_events_become_canonical_records(events_fixture_dir, data_clock) -> None:
    result = FixtureCorporateEventProvider(events_fixture_dir, clock=data_clock).fetch_events(
        "NVDA"
    )

    assert result.succeeded
    assert result.record_count == 2
    earnings = next(e for e in result.records if e.event_type is CorporateEventType.EARNINGS)
    assert earnings.confirmed
    assert earnings.announced_at is not None
    assert earnings.event_time > earnings.announced_at


def test_an_event_without_an_announcement_time_is_rejected(tmp_path, data_clock) -> None:
    """Without ``announced_at`` the event would leak into earlier snapshots."""
    import json

    path = tmp_path / "events"
    path.mkdir()
    (path / "ACME.json").write_text(
        json.dumps(
            [{"event_id": "x", "event_type": "EARNINGS", "event_time": "2026-09-01T20:00:00+00:00"}]
        ),
        encoding="utf-8",
    )

    result = FixtureCorporateEventProvider(path, clock=data_clock).fetch_events("ACME")
    assert result.records == ()
    assert any("announced_at" in note for note in result.notes)


def test_an_unknown_event_type_falls_back_to_other(tmp_path, data_clock) -> None:
    import json

    path = tmp_path / "events"
    path.mkdir()
    (path / "ACME.json").write_text(
        json.dumps(
            [
                {
                    "event_id": "x",
                    "event_type": "SOMETHING_NEW",
                    "event_time": "2026-09-01T20:00:00+00:00",
                    "announced_at": "2026-08-01T20:00:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )

    event = FixtureCorporateEventProvider(path, clock=data_clock).fetch_events("ACME").records[0]
    assert event.event_type is CorporateEventType.OTHER


# ---------------------------------------------------------------------------
# The interfaces exist for a real provider to implement later
# ---------------------------------------------------------------------------
def test_the_interfaces_declare_their_data_types() -> None:
    assert DataType.NEWS_ARTICLE in FixtureNewsProvider("/tmp").data_types
    assert DataType.CORPORATE_EVENT in FixtureCorporateEventProvider("/tmp").data_types
    assert issubclass(FixtureNewsProvider, NewsProvider)
    assert issubclass(FixtureCorporateEventProvider, CorporateEventProvider)


def test_the_deferral_is_documented_on_the_provider() -> None:
    """A gap that is not written down becomes a gap nobody remembers."""
    assert "no live news provider" in FixtureNewsProvider.notes.lower()
    from trading_system.data.providers import news as news_module

    assert "deliberate" in (news_module.__doc__ or "").lower()
