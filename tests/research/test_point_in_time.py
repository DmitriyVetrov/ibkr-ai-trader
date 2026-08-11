"""Point-in-time protection (brief sections 7 and 47). Mandatory.

``research(as_of=T)`` may only see what the system had actually retrieved by T.
Everything else in this milestone rests on it: an outlook that quietly read
tomorrow's news would evaluate beautifully and mean nothing, and the failure
would be invisible in the stored report — the citation would look perfectly
ordinary.

The distinction that has to survive is between *content* and *knowledge*. A
future-dated earnings date is legitimate evidence, because a calendar is
supposed to point forward. The same event is invisible before it was announced,
because until then we did not know it. These tests check both halves.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.data.point_in_time import LookAheadError
from trading_system.domain.enums import ResearchStatus

from .conftest import RESEARCH_NOW

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Nothing retrieved after T may be seen
# ---------------------------------------------------------------------------
def test_news_published_after_the_instant_is_invisible(
    store_quote, store_chain, store_news, build_input
) -> None:
    store_quote("NVDA")
    store_chain("NVDA")
    store_news("NVDA", article_id="known", published_at=RESEARCH_NOW - timedelta(days=1))
    store_news(
        "NVDA",
        article_id="tomorrow",
        headline="Something that has not happened yet",
        published_at=RESEARCH_NOW + timedelta(days=1),
        retrieved_at=RESEARCH_NOW + timedelta(days=1),
    )

    research_input = build_input("NVDA", as_of=RESEARCH_NOW)

    headlines = " ".join(item.summary for item in research_input.news)
    assert "has not happened yet" not in headlines
    assert len(research_input.news) == 1


def test_a_filing_downloaded_later_is_invisible_however_old_its_content(
    store_quote, store_chain, store_filing, build_input
) -> None:
    """Retrieval binds. A 2019 filing fetched today did not inform last week."""
    store_quote("NVDA")
    store_chain("NVDA")
    store_filing(
        "NVDA",
        event_id="old-filing-fetched-today",
        filed_at=RESEARCH_NOW - timedelta(days=30),
        retrieved_at=RESEARCH_NOW + timedelta(days=2),
    )

    research_input = build_input("NVDA", as_of=RESEARCH_NOW)

    assert research_input.regulatory_events == []


def test_a_price_observed_after_the_instant_is_invisible(
    store_quote, store_chain, build_input
) -> None:
    store_quote("NVDA", as_of=RESEARCH_NOW - timedelta(hours=1), last=Decimal("180.00"))
    store_chain("NVDA")
    store_quote(
        "NVDA",
        as_of=RESEARCH_NOW + timedelta(days=1),
        retrieved_at=RESEARCH_NOW + timedelta(days=1),
        last=Decimal("999.00"),
    )

    snapshot = build_input("NVDA", as_of=RESEARCH_NOW).market_snapshot

    assert snapshot is not None
    assert snapshot.last == Decimal("180.00"), "tomorrow's price is not today's evidence"


def test_an_event_announced_after_the_instant_is_invisible(
    store_quote, store_chain, store_event, build_input
) -> None:
    """The event exists in the database today. It did not exist to us then."""
    store_quote("NVDA")
    store_chain("NVDA")
    store_event(
        "NVDA",
        event_id="announced-later",
        event_time=RESEARCH_NOW + timedelta(days=20),
        announced_at=RESEARCH_NOW + timedelta(days=2),
        retrieved_at=RESEARCH_NOW + timedelta(days=2),
    )

    research_input = build_input("NVDA", as_of=RESEARCH_NOW)

    assert research_input.events == []


def test_fundamentals_retrieved_later_are_invisible(
    store_quote, store_chain, store_fundamentals, build_input
) -> None:
    store_quote("NVDA")
    store_chain("NVDA")
    store_fundamentals("NVDA", retrieved_at=RESEARCH_NOW + timedelta(days=3))

    assert build_input("NVDA", as_of=RESEARCH_NOW).fundamentals == []


# ---------------------------------------------------------------------------
# The essential distinction: future content is fine, future knowledge is not
# ---------------------------------------------------------------------------
def test_a_future_event_announced_before_the_instant_is_visible(
    store_quote, store_chain, store_event, build_input
) -> None:
    """This is the whole point of a calendar and must not be filtered out."""
    store_quote("NVDA")
    store_chain("NVDA")
    store_event(
        "NVDA",
        event_id="earnings-in-17-days",
        event_time=RESEARCH_NOW + timedelta(days=17),
        announced_at=RESEARCH_NOW - timedelta(days=4),
    )

    events = build_input("NVDA", as_of=RESEARCH_NOW).events

    assert len(events) == 1
    assert events[0].event_id == "earnings-in-17-days"
    assert events[0].expected_event_time > RESEARCH_NOW
    assert events[0].within_horizon is True


def test_an_event_beyond_the_horizon_is_visible_but_flagged_outside_it(
    store_quote, store_chain, store_event, build_input
) -> None:
    """Deliberately wider than the horizon: an event just past it is a reason
    *not* to expect the move inside it, which is information."""
    store_quote("NVDA")
    store_chain("NVDA")
    store_event(
        "NVDA",
        event_id="earnings-in-40-days",
        event_time=RESEARCH_NOW + timedelta(days=40),
        announced_at=RESEARCH_NOW - timedelta(days=4),
    )

    events = build_input("NVDA", as_of=RESEARCH_NOW).events

    assert len(events) == 1
    assert events[0].within_horizon is False


# ---------------------------------------------------------------------------
# Replaying a past instant
# ---------------------------------------------------------------------------
def test_research_can_be_reconstructed_for_a_past_instant(
    store_quote, store_chain, store_news, build_input
) -> None:
    """What the system knew last week, reconstructed today, excludes this week."""
    last_week = RESEARCH_NOW - timedelta(days=7)
    store_quote("NVDA", as_of=last_week, retrieved_at=last_week, last=Decimal("160.00"))
    store_chain("NVDA", as_of=last_week)
    store_news("NVDA", article_id="old", published_at=last_week - timedelta(hours=2))
    store_news("NVDA", article_id="new", published_at=RESEARCH_NOW - timedelta(hours=2))

    then = build_input("NVDA", as_of=last_week)
    now = build_input("NVDA", as_of=RESEARCH_NOW)

    assert then.market_snapshot is not None and then.market_snapshot.last == Decimal("160.00")
    assert len(then.news) == 1
    assert len(now.news) == 2, "and today, both are visible"


def test_the_snapshot_list_only_names_snapshots_that_were_visible(
    store_quote, store_chain, store_news, build_input
) -> None:
    store_quote("NVDA")
    store_chain("NVDA")
    store_news(
        "NVDA",
        article_id="future",
        published_at=RESEARCH_NOW + timedelta(days=1),
        retrieved_at=RESEARCH_NOW + timedelta(days=1),
    )

    research_input = build_input("NVDA", as_of=RESEARCH_NOW)

    for item in research_input.all_evidence:
        assert item.source.retrieved_at <= RESEARCH_NOW


# ---------------------------------------------------------------------------
# A leak is a hard failure, never a quietly shorter list
# ---------------------------------------------------------------------------
def test_a_look_ahead_leak_raises_rather_than_being_filtered(
    data_repo, store_quote, monkeypatch, make_research_config
) -> None:
    """A shortened list would look like an ordinary quiet week."""
    from trading_system.research.context import ResearchInputBuilder
    from trading_system.research.sources import SourceTrustPolicy

    store_quote("NVDA")
    config = make_research_config()
    builder = ResearchInputBuilder(
        data_repo, config=config.research, source_policy=SourceTrustPolicy(config.sources)
    )

    # Simulate a storage bug: the ledger says the snapshot was visible, but a
    # record inside it was not. Nothing may paper over that.
    original = type(data_repo).get_as_of

    def leaking_get_as_of(self, data_type, key, as_of, *, provider=None):
        return original(self, data_type, key, as_of + timedelta(days=30), provider=provider)

    store_quote(
        "NVDA",
        as_of=RESEARCH_NOW + timedelta(days=10),
        retrieved_at=RESEARCH_NOW + timedelta(days=10),
    )
    monkeypatch.setattr(type(data_repo), "get_as_of", leaking_get_as_of)

    with pytest.raises(LookAheadError):
        builder.build("NVDA", RESEARCH_NOW, run_id="leak-test")


def test_the_service_reports_a_leak_as_point_in_time_error_without_an_outlook(
    make_service, store_universe, store_quote, monkeypatch, data_repo
) -> None:
    """A correctness bug in storage is never turned into a market view."""
    store_universe(["NVDA"])
    store_quote("NVDA")
    store_quote(
        "NVDA",
        as_of=RESEARCH_NOW + timedelta(days=10),
        retrieved_at=RESEARCH_NOW + timedelta(days=10),
    )

    original = type(data_repo).get_as_of

    def leaking_get_as_of(self, data_type, key, as_of, *, provider=None):
        return original(self, data_type, key, as_of + timedelta(days=30), provider=provider)

    monkeypatch.setattr(type(data_repo), "get_as_of", leaking_get_as_of)
    service = make_service()

    run = service.run(as_of=RESEARCH_NOW, dry_run=True)

    report = run.result.report("NVDA")
    assert report is not None
    assert report.status is ResearchStatus.POINT_IN_TIME_ERROR
    assert report.hypothesis is None
    assert report.confidence is None
    assert "not knowable" in (report.status_detail or "")
