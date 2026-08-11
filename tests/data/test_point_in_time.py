"""Look-ahead bias protection.

The most important suite in the data layer, and arguably in the project. Every
historical claim the system will ever make — a backtest, a forward-test score,
an evaluation of why a trade worked — is worthless if a record can be seen
before it was known.

The rule under test: a record is visible at time T only if *every* clock it
carries has passed T, and above all only if we had actually retrieved it by
then. An article published in 2019 that we downloaded this morning did not
inform a decision made last week.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.data.models import CorporateEvent, FundamentalSnapshot, NewsArticle
from trading_system.data.point_in_time import (
    LookAheadError,
    assert_no_look_ahead,
    latest_as_of,
    visible_at,
)
from trading_system.data.repository import build_snapshot
from trading_system.domain.enums import (
    CorporateEventType,
    DataType,
    MarketDataOrigin,
    SourceTier,
)

pytestmark = pytest.mark.unit


def _snapshot(records, *, key="SPY", data_type=DataType.MARKET_QUOTE, quality):
    first = records[0]
    return build_snapshot(
        data_type=data_type,
        key=key,
        records=records,
        provider=first.source.provider,
        source_tier=first.source.source_tier,
        origin=first.source.origin,
        as_of=first.as_of,
        retrieved_at=first.source.retrieved_at,
        source_timestamp=first.source.source_timestamp,
        quality=quality,
    )


# ---------------------------------------------------------------------------
# 1. Future news must not appear in an earlier snapshot
# ---------------------------------------------------------------------------
def test_news_published_after_t_is_invisible_at_t(make_source, data_now) -> None:
    later = data_now + timedelta(hours=6)
    article = NewsArticle(
        as_of=later,
        source=make_source(
            provider="FIXTURE_NEWS",
            tier=SourceTier.TIER_2,
            origin=MarketDataOrigin.HISTORICAL,
            retrieved_at=later,
            source_timestamp=later,
            published_at=later,
            source_identifier="https://example.com/a",
        ),
        article_id="a-1",
        headline="Something that has not happened yet",
        symbols=["NVDA"],
    )

    assert not article.known_at(data_now)
    assert visible_at([article], data_now) == []
    assert visible_at([article], later) == [article]


def test_an_old_article_retrieved_today_is_invisible_last_week(make_source, data_now) -> None:
    """Retrieval binds, not publication.

    This is the subtle case that a naive ``published_at <= T`` filter gets
    wrong, and it is exactly how a backtest ends up quietly cheating.
    """
    long_ago = data_now - timedelta(days=400)
    last_week = data_now - timedelta(days=7)
    article = NewsArticle(
        as_of=long_ago,
        source=make_source(
            provider="FIXTURE_NEWS",
            tier=SourceTier.TIER_2,
            origin=MarketDataOrigin.HISTORICAL,
            retrieved_at=data_now,
            source_timestamp=long_ago,
            published_at=long_ago,
            source_identifier="https://example.com/old",
        ),
        article_id="a-old",
        headline="Published long ago, downloaded only today",
        symbols=["NVDA"],
    )

    assert article.source.published_at is not None
    assert article.source.published_at < last_week
    assert not article.known_at(last_week)
    assert article.known_at(data_now)


# ---------------------------------------------------------------------------
# 2. Future market and option data
# ---------------------------------------------------------------------------
def test_a_quote_observed_after_t_is_invisible_at_t(make_quote, data_now) -> None:
    later = data_now + timedelta(minutes=30)
    quote = make_quote(as_of=later, retrieved_at=later, source_timestamp=later)

    assert not quote.known_at(data_now)
    assert quote.known_at(later)


def test_a_chain_retrieved_after_t_is_invisible_at_t(make_chain, data_now) -> None:
    later = data_now + timedelta(hours=2)
    chain = make_chain(as_of=later, retrieved_at=later, source_timestamp=later)

    assert not chain.known_at(data_now)


def test_get_as_of_never_returns_a_snapshot_retrieved_later(
    repository, make_quote, quality_engine, data_now
) -> None:
    early = data_now - timedelta(hours=2)
    late = data_now + timedelta(hours=2)

    early_quote = make_quote(
        as_of=early, retrieved_at=early, source_timestamp=early, last=Decimal("490")
    )
    late_quote = make_quote(
        as_of=late, retrieved_at=late, source_timestamp=late, last=Decimal("510")
    )
    for quote in (early_quote, late_quote):
        repository.save_snapshot(_snapshot([quote], quality=quality_engine.evaluate(quote)))

    seen = repository.get_as_of(DataType.MARKET_QUOTE, "SPY", data_now)
    assert seen is not None
    assert seen.as_of == early
    assert seen.records[0]["last"] == "490"


# ---------------------------------------------------------------------------
# 3. Revised fundamentals keep their original timestamps
# ---------------------------------------------------------------------------
def test_a_restatement_does_not_rewrite_the_original(
    repository, make_source, quality_engine, data_now
) -> None:
    """A revision is a new record with a later publication time.

    The original keeps its own ``published_at`` and stays the correct answer
    for any instant before the revision was filed.
    """
    period_end_at = data_now - timedelta(days=40)
    first_filed = data_now - timedelta(days=30)
    revised_filed = data_now - timedelta(days=2)

    def _snapshot_for(revenue, filed):
        return FundamentalSnapshot(
            as_of=filed,
            source=make_source(
                provider="SEC_XBRL",
                tier=SourceTier.TIER_1,
                origin=MarketDataOrigin.PROVIDER_REALTIME,
                retrieved_at=filed,
                source_timestamp=filed,
                published_at=filed,
                effective_at=period_end_at,
                source_identifier="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
            ),
            symbol="AAPL",
            revenue=revenue,
            filing_accession_number="0000320193-26-000081",
        )

    original = _snapshot_for(Decimal("94036000000"), first_filed)
    revision = _snapshot_for(Decimal("93500000000"), revised_filed)
    for record in (original, revision):
        repository.save_snapshot(
            _snapshot(
                [record],
                key="AAPL",
                data_type=DataType.FUNDAMENTAL_SNAPSHOT,
                quality=quality_engine.evaluate(record),
            )
        )

    before = repository.get_as_of(
        DataType.FUNDAMENTAL_SNAPSHOT, "AAPL", revised_filed - timedelta(days=1)
    )
    after = repository.get_as_of(DataType.FUNDAMENTAL_SNAPSHOT, "AAPL", data_now)

    assert before is not None and after is not None
    assert before.records[0]["revenue"] == "94036000000"
    assert after.records[0]["revenue"] == "93500000000"
    # The original's own effective time survives the restatement.
    assert before.records[0]["source"]["effective_at"].startswith("2026-07-01T14:30:00")


# ---------------------------------------------------------------------------
# 4. Corporate events: future dates are fine, unknown announcements are not
# ---------------------------------------------------------------------------
def test_a_future_event_is_visible_once_announced(make_source, data_now) -> None:
    """A calendar is about things that have not happened. That is allowed."""
    announced = data_now - timedelta(days=4)
    event = CorporateEvent(
        as_of=announced,
        source=make_source(
            provider="FIXTURE_EVENTS",
            origin=MarketDataOrigin.HISTORICAL,
            retrieved_at=announced,
            source_timestamp=announced,
            published_at=announced,
        ),
        event_id="e-1",
        event_type=CorporateEventType.EARNINGS,
        symbol="NVDA",
        event_time=data_now + timedelta(days=17),
        announced_at=announced,
        confirmed=True,
    )

    assert event.event_time > data_now
    assert event.known_at(data_now)


def test_an_event_announced_after_t_is_invisible_at_t(make_source, data_now) -> None:
    announced = data_now + timedelta(days=1)
    event = CorporateEvent(
        as_of=announced,
        source=make_source(
            provider="FIXTURE_EVENTS",
            origin=MarketDataOrigin.HISTORICAL,
            retrieved_at=announced,
            source_timestamp=announced,
            published_at=announced,
        ),
        event_id="e-2",
        event_type=CorporateEventType.INVESTOR_DAY,
        symbol="NVDA",
        event_time=data_now + timedelta(days=60),
        announced_at=announced,
    )

    assert not event.known_at(data_now)


# ---------------------------------------------------------------------------
# 5. get_as_of returns the latest thing that was genuinely available
# ---------------------------------------------------------------------------
def test_get_as_of_returns_the_latest_available_not_merely_the_first(
    repository, make_quote, quality_engine, data_now
) -> None:
    instants = [data_now - timedelta(hours=n) for n in (5, 3, 1)]
    for index, instant in enumerate(instants):
        quote = make_quote(
            as_of=instant,
            retrieved_at=instant,
            source_timestamp=instant,
            last=Decimal(f"50{index}"),
        )
        repository.save_snapshot(_snapshot([quote], quality=quality_engine.evaluate(quote)))

    middle = repository.get_as_of(DataType.MARKET_QUOTE, "SPY", data_now - timedelta(hours=2))
    newest = repository.get_as_of(DataType.MARKET_QUOTE, "SPY", data_now)

    assert middle is not None and middle.as_of == instants[1]
    assert newest is not None and newest.as_of == instants[2]


def test_get_as_of_before_any_collection_returns_nothing(
    repository, make_quote, quality_engine, data_now
) -> None:
    quote = make_quote()
    repository.save_snapshot(_snapshot([quote], quality=quality_engine.evaluate(quote)))

    assert repository.get_as_of(DataType.MARKET_QUOTE, "SPY", data_now - timedelta(days=1)) is None


# ---------------------------------------------------------------------------
# Helpers refuse to be used unsafely
# ---------------------------------------------------------------------------
def test_assert_no_look_ahead_raises_rather_than_filtering(make_quote, data_now) -> None:
    """A caller that believes it filtered correctly gets that belief checked."""
    future = make_quote(as_of=data_now + timedelta(hours=1))

    with pytest.raises(LookAheadError, match="not knowable"):
        assert_no_look_ahead([future], data_now)


def test_a_naive_instant_is_refused(make_quote, data_now) -> None:
    from datetime import datetime

    with pytest.raises(LookAheadError, match="timezone-aware"):
        visible_at([make_quote()], datetime(2026, 8, 10, 14, 30))


def test_latest_as_of_prefers_the_later_observation(make_quote, data_now) -> None:
    two_hours_ago = data_now - timedelta(hours=2)
    five_minutes_ago = data_now - timedelta(minutes=5)
    older = make_quote(
        as_of=two_hours_ago, retrieved_at=two_hours_ago, source_timestamp=two_hours_ago
    )
    newer = make_quote(
        as_of=five_minutes_ago,
        retrieved_at=five_minutes_ago,
        source_timestamp=five_minutes_ago,
    )

    pair = [older, newer]

    assert latest_as_of(pair, data_now) is newer
    assert latest_as_of(pair, data_now - timedelta(hours=1)) is older
    assert latest_as_of(pair, data_now - timedelta(days=1)) is None
