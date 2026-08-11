"""News grouping: one event is one piece of evidence, however often it is told.

A wire story about an earnings beat is reprinted, syndicated and rewritten. Ten
copies of it are ten *reports* of one fact — corroboration, which is worth
something — and they are emphatically not ten independent catalysts. An agent
shown all ten will weigh the story ten times, and the resulting confidence will
be a fact about the news industry rather than about the market.

So articles are grouped before they reach the agent, and each group arrives as
one :class:`~trading_system.research.models.EvidenceItem` carrying how many
further reports were folded in and where they came from.

The grouping is deliberately shallow (Milestone 5 brief section 31): normalised
headline similarity inside a publication window. No embeddings, no model call,
no semantic clustering. The objective is to stop one story counting ten times,
not to build a topic model — and a grouping nobody can predict is worse than a
crude one everybody can.

Three properties the implementation holds:

* **Deterministic.** Same articles in, same groups out, same representative
  chosen, in the same order. Reproducibility depends on it.
* **Conservative.** When in doubt the articles stay separate. Merging two
  genuinely different stories destroys evidence; leaving two copies apart
  merely overweights one, which the corroboration count then makes visible.
* **Lossless.** Nothing is discarded. Every folded-in article's source is named
  on the group, so "which outlets reported this" stays answerable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading_system.data.models import NewsArticle
from trading_system.infrastructure.settings import DeduplicationConfig
from trading_system.research.models import tier_rank
from trading_system.research.sources import SourceTrustPolicy

__all__ = ["ArticleGroup", "group_articles", "normalise_headline", "similarity"]

_NON_WORD = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ArticleGroup:
    """One story, and every article that reported it.

    ``representative`` is the article whose text and provenance the evidence
    item is built from; ``duplicates`` are the rest, retained so the group can
    say who else carried it.
    """

    representative: NewsArticle
    duplicates: tuple[NewsArticle, ...] = field(default_factory=tuple)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)

    @property
    def size(self) -> int:
        return 1 + len(self.duplicates)

    def source_names(self) -> list[str]:
        """Distinct outlets that carried the duplicates, in a stable order."""
        names = {
            article.source.source_name or article.source.provider for article in self.duplicates
        }
        return sorted(names)


def normalise_headline(headline: str, stopwords: frozenset[str]) -> tuple[str, ...]:
    """Reduce a headline to a comparable token tuple.

    Lower-cased, punctuation removed, configured stopwords dropped, duplicates
    removed and the remainder sorted — so word order and house style do not
    make two tellings of one story look different.
    """
    tokens = [token for token in _NON_WORD.split(headline.lower()) if token]
    meaningful = sorted({token for token in tokens if token not in stopwords})
    return tuple(meaningful)


def similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """Jaccard similarity of two normalised headlines, in ``[0, 1]``.

    Two empty headlines are treated as *dissimilar* rather than identical:
    a headline that normalises to nothing carries no evidence that it is the
    same story, and merging on an absence would be the one destructive mistake
    this module must not make.
    """
    if not left or not right:
        return 0.0
    a, b = set(left), set(right)
    return len(a & b) / len(a | b)


def group_articles(
    articles: list[NewsArticle],
    *,
    config: DeduplicationConfig,
    policy: SourceTrustPolicy | None = None,
) -> list[ArticleGroup]:
    """Group articles that report the same story.

    Two articles join a group when their normalised headlines reach the
    configured similarity **and** they were published within the configured
    window of each other. Both conditions are required: last year's results and
    this year's produce near-identical headlines, and only the window separates
    them.

    The representative is the most trusted source, then the earliest
    publication, then the lexicographically smallest article id. Preferring the
    earliest is deliberate — the first telling is the one closest to the event,
    and the later copies are what the corroboration count is for.

    Disabled by configuration, every article becomes its own group; the shape
    of the output does not change, so no caller has to care.
    """
    if not articles:
        return []

    ordered = sorted(articles, key=_ordering_key)
    if not config.enabled:
        return [ArticleGroup(representative=article) for article in ordered]

    stopwords = frozenset(word.lower() for word in config.stopwords)
    window = timedelta(hours=config.publication_window_hours)
    threshold = config.headline_similarity

    buckets: list[list[NewsArticle]] = []
    signatures: list[tuple[str, ...]] = []

    for article in ordered:
        signature = normalise_headline(article.headline, stopwords)
        placed = False
        for index, existing in enumerate(signatures):
            if similarity(signature, existing) < threshold:
                continue
            if not _within_window(buckets[index], article, window):
                continue
            buckets[index].append(article)
            placed = True
            break
        if not placed:
            buckets.append([article])
            signatures.append(signature)

    groups: list[ArticleGroup] = []
    for bucket in buckets:
        ranked = sorted(bucket, key=lambda a: _representative_key(a, policy))
        groups.append(ArticleGroup(representative=ranked[0], duplicates=tuple(ranked[1:])))
    return groups


def _within_window(bucket: list[NewsArticle], article: NewsArticle, window: timedelta) -> bool:
    """Whether ``article`` was published close enough to everything in ``bucket``.

    Compared against every member rather than only the first, so a chain of
    slightly-overlapping articles cannot quietly stretch a 48-hour window into
    a week.
    """
    published = _published(article)
    for member in bucket:
        other = _published(member)
        if published is None or other is None:
            # A publication time is what places a story in the news cycle.
            # Without one there is nothing to say the two are the same event.
            return False
        if abs(published - other) > window:
            return False
    return True


def _published(article: NewsArticle) -> datetime | None:
    return article.source.published_at or article.source.source_timestamp


def _ordering_key(article: NewsArticle) -> tuple[str, str]:
    """Stable input order: earliest publication, then article id."""
    published = _published(article)
    return (published.isoformat() if published else "", article.article_id)


def _representative_key(
    article: NewsArticle, policy: SourceTrustPolicy | None
) -> tuple[int, str, str]:
    """Most trusted, then earliest, then lexicographic. Fully deterministic."""
    declared = article.source.source_tier
    tier = (
        policy.resolve(
            declared=declared,
            identifier=article.source.source_identifier,
            name=article.source.source_name,
        )
        if policy is not None
        else declared
    )
    published = _published(article)
    return (tier_rank(tier), published.isoformat() if published else "9999", article.article_id)
