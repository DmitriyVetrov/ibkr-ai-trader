"""News grouping (brief section 31).

One objective: ten syndicated copies of one Reuters story must not become ten
independent catalysts. Everything here checks that, and checks the two ways the
grouping could do harm instead — merging genuinely different stories, or
becoming unpredictable.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_system.domain.enums import SourceTier
from trading_system.infrastructure.settings import DeduplicationConfig
from trading_system.research.evidence import group_articles, normalise_headline, similarity
from trading_system.research.sources import SourceTrustPolicy

from .conftest import RESEARCH_NOW

pytestmark = pytest.mark.unit

STOPWORDS = frozenset({"a", "an", "and", "the", "of", "to", "in", "on", "for"})


@pytest.fixture
def config() -> DeduplicationConfig:
    return DeduplicationConfig(
        enabled=True,
        headline_similarity=0.8,
        publication_window_hours=48,
        stopwords=sorted(STOPWORDS),
    )


@pytest.fixture
def policy(system_config) -> SourceTrustPolicy:
    return SourceTrustPolicy(system_config.sources)


def _article(store_news, **kwargs):
    return store_news("NVDA", **kwargs)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def test_normalisation_ignores_case_punctuation_and_word_order() -> None:
    left = normalise_headline("Nvidia's data-centre revenue accelerates!", STOPWORDS)
    right = normalise_headline("Revenue accelerates at Nvidia's data centre", STOPWORDS)

    assert similarity(left, right) >= 0.8


def test_two_empty_headlines_are_not_treated_as_the_same_story() -> None:
    """Merging on an absence is the one destructive mistake available here."""
    assert similarity((), ()) == 0.0


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def test_syndicated_copies_of_one_story_become_one_item(store_news, config, policy) -> None:
    articles = [
        _article(
            store_news,
            article_id=f"copy-{index}",
            headline="Nvidia data-centre revenue accelerates, company says",
            source_name=name,
            url=url,
            tier=tier,
            published_at=RESEARCH_NOW - timedelta(hours=index + 1),
        )
        for index, (name, url, tier) in enumerate(
            [
                ("Reuters", "https://www.reuters.com/a", SourceTier.TIER_2),
                ("MarketWatch", "https://www.marketwatch.com/b", SourceTier.TIER_3),
                ("Some Aggregator", "https://aggregator.test/c", SourceTier.TIER_4),
            ]
        )
    ]

    groups = group_articles(articles, config=config, policy=policy)

    assert len(groups) == 1, "one event, one piece of evidence"
    assert groups[0].size == 3
    assert groups[0].duplicate_count == 2


def test_the_most_trusted_source_becomes_the_representative(store_news, config, policy) -> None:
    articles = [
        _article(
            store_news,
            article_id="tier4",
            headline="Nvidia data-centre revenue accelerates, company says",
            source_name="Some Aggregator",
            url="https://aggregator.test/c",
            tier=SourceTier.TIER_4,
            published_at=RESEARCH_NOW - timedelta(hours=3),
        ),
        _article(
            store_news,
            article_id="tier2",
            headline="Nvidia data-centre revenue accelerates, company says",
            source_name="Reuters",
            url="https://www.reuters.com/a",
            tier=SourceTier.TIER_2,
            published_at=RESEARCH_NOW - timedelta(hours=1),
        ),
    ]

    groups = group_articles(articles, config=config, policy=policy)

    assert groups[0].representative.article_id == "tier2"
    assert groups[0].source_names() == ["Some Aggregator"]


def test_distinct_stories_are_not_merged(store_news, config, policy) -> None:
    articles = [
        _article(
            store_news,
            article_id="revenue",
            headline="Nvidia data-centre revenue accelerates, company says",
            published_at=RESEARCH_NOW - timedelta(hours=2),
        ),
        _article(
            store_news,
            article_id="lawsuit",
            headline="Regulator opens antitrust review into chip licensing",
            published_at=RESEARCH_NOW - timedelta(hours=3),
        ),
    ]

    groups = group_articles(articles, config=config, policy=policy)

    assert len(groups) == 2


def test_identical_headlines_far_apart_in_time_are_separate_events(
    store_news, config, policy
) -> None:
    """Last year's results and this year's produce near-identical headlines."""
    articles = [
        _article(
            store_news,
            article_id="this-year",
            headline="Company reports second-quarter results",
            published_at=RESEARCH_NOW - timedelta(hours=2),
        ),
        _article(
            store_news,
            article_id="last-quarter",
            headline="Company reports second-quarter results",
            published_at=RESEARCH_NOW - timedelta(days=8),
        ),
    ]

    groups = group_articles(articles, config=config, policy=policy)

    assert len(groups) == 2


def test_an_article_with_no_publication_time_is_never_merged(store_news, config, policy) -> None:
    """Without a publication time there is nothing placing it in the news cycle."""
    dated = _article(
        store_news,
        article_id="dated",
        headline="Nvidia data-centre revenue accelerates, company says",
        published_at=RESEARCH_NOW - timedelta(hours=2),
    )
    undated = dated.model_copy(
        update={
            "article_id": "undated",
            "source": dated.source.model_copy(
                update={"published_at": None, "source_timestamp": None}
            ),
        }
    )

    groups = group_articles([dated, undated], config=config, policy=policy)

    assert len(groups) == 2


def test_grouping_is_deterministic(store_news, config, policy) -> None:
    """Same articles in, same groups out. Reproducibility depends on it."""
    articles = [
        _article(
            store_news,
            article_id=f"copy-{index}",
            headline="Nvidia data-centre revenue accelerates, company says",
            published_at=RESEARCH_NOW - timedelta(hours=index + 1),
        )
        for index in range(4)
    ]

    first = group_articles(articles, config=config, policy=policy)
    second = group_articles(list(reversed(articles)), config=config, policy=policy)

    assert [g.representative.article_id for g in first] == [
        g.representative.article_id for g in second
    ]


def test_disabled_deduplication_keeps_every_article_separate(store_news, config, policy) -> None:
    articles = [
        _article(
            store_news,
            article_id=f"copy-{index}",
            headline="Nvidia data-centre revenue accelerates, company says",
            published_at=RESEARCH_NOW - timedelta(hours=index + 1),
        )
        for index in range(3)
    ]

    groups = group_articles(
        articles, config=config.model_copy(update={"enabled": False}), policy=policy
    )

    assert len(groups) == 3
    assert all(group.duplicate_count == 0 for group in groups)


def test_exact_match_only_when_the_threshold_is_one(store_news, config, policy) -> None:
    articles = [
        _article(
            store_news,
            article_id="original",
            headline="Nvidia data-centre revenue accelerates, company says",
            published_at=RESEARCH_NOW - timedelta(hours=1),
        ),
        _article(
            store_news,
            article_id="rewrite",
            headline="Nvidia data-centre revenue accelerates sharply, company says",
            published_at=RESEARCH_NOW - timedelta(hours=2),
        ),
    ]

    grouped = group_articles(
        articles, config=config.model_copy(update={"headline_similarity": 1.0}), policy=policy
    )

    assert len(grouped) == 2


# ---------------------------------------------------------------------------
# Through the input builder
# ---------------------------------------------------------------------------
def test_the_input_shows_one_item_with_a_corroboration_count(
    store_quote, store_chain, store_news, build_input
) -> None:
    """The agent sees one story, and sees that three outlets carried it."""
    store_quote("NVDA")
    store_chain("NVDA")
    for index, (name, url) in enumerate(
        [
            ("Reuters", "https://www.reuters.com/a"),
            ("MarketWatch", "https://www.marketwatch.com/b"),
            ("Barrons", "https://www.barrons.com/c"),
        ]
    ):
        store_news(
            "NVDA",
            article_id=f"copy-{index}",
            headline="Nvidia data-centre revenue accelerates, company says",
            source_name=name,
            url=url,
            published_at=RESEARCH_NOW - timedelta(hours=index + 1),
        )

    research_input = build_input("NVDA")

    assert len(research_input.news) == 1
    item = research_input.news[0]
    assert item.duplicate_count == 2
    assert item.is_corroborated
    assert set(item.duplicate_source_names) == {"MarketWatch", "Barrons"}


def test_ten_copies_are_not_ten_evidence_items(
    store_quote, store_chain, store_news, build_input
) -> None:
    store_quote("NVDA")
    store_chain("NVDA")
    for index in range(10):
        store_news(
            "NVDA",
            article_id=f"copy-{index}",
            headline="Nvidia data-centre revenue accelerates, company says",
            source_name=f"Outlet {index}",
            url=f"https://outlet-{index}.test/story",
            published_at=RESEARCH_NOW - timedelta(hours=index + 1),
        )

    news = build_input("NVDA").news

    assert len(news) == 1, "one event, one catalyst"
    assert news[0].duplicate_count == 9
