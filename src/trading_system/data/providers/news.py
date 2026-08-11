"""News and corporate event providers.

**No live news provider ships in Milestone 3, and that is a deliberate,
documented gap.** The Tier 1 and Tier 2 sources this system is supposed to
prefer — SEC releases aside — do not offer free structured news retrieval:
Reuters, Bloomberg, the FT and the WSJ all require paid licensing, and the
free aggregators that remain are Tier 4 sources of uncertain provenance. The
brief is explicit that where reliable free news retrieval is unavailable, the
interface and fixtures are the correct deliverable and inventing a provider is
not.

So what exists here is:

* :class:`NewsProvider` — the interface a real provider will implement;
* :class:`FixtureNewsProvider` — replays recorded articles from disk, which
  makes the whole downstream path (normalisation, quality, point-in-time
  storage, look-ahead filtering) testable and exercised today.

The nearest free Tier 1 substitute for corporate announcements is already
implemented as :mod:`trading_system.data.providers.regulatory`: 8-K filings are
the announcement itself rather than a report of it.

No provider here classifies an article. Sentiment, relevance to a thesis and
market interpretation belong to the research agent in Milestone 5.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_system.data.hashing import payload_hash, stable_hash
from trading_system.data.models import CorporateEvent, NewsArticle, RawRecord
from trading_system.data.providers.base import (
    DataProvider,
    ProviderAvailability,
    ProviderCost,
    ProviderResult,
    failed_result,
    successful_result,
)
from trading_system.domain.enums import (
    CollectionOutcome,
    CorporateEventType,
    DataType,
    MarketDataOrigin,
    SourceTier,
)
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = [
    "CorporateEventProvider",
    "FixtureCorporateEventProvider",
    "FixtureNewsProvider",
    "NewsProvider",
]


class NewsProvider(DataProvider):
    """Interface for structured news retrieval."""

    @property
    def data_types(self) -> frozenset[DataType]:
        return frozenset({DataType.NEWS_ARTICLE})

    @abstractmethod
    def fetch_news(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> ProviderResult[NewsArticle]:
        """Retrieve articles mentioning ``symbol``.

        Returns structured records only. No classification, no scoring, no
        summarisation beyond what the source itself supplied.
        """


class CorporateEventProvider(DataProvider):
    """Interface for corporate calendar retrieval.

    Future-dated events are expected — that is what a calendar is. What the
    storage layer enforces is that an event cannot appear in a snapshot taken
    before it was announced.
    """

    @property
    def data_types(self) -> frozenset[DataType]:
        return frozenset({DataType.CORPORATE_EVENT})

    @abstractmethod
    def fetch_events(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
    ) -> ProviderResult[CorporateEvent]: ...


class FixtureNewsProvider(NewsProvider):
    """Replays recorded news articles from a directory of JSON files.

    Each file holds a list of article objects. Every article must carry its own
    ``published_at``, ``source_name``, ``source_tier`` and ``url``: an article
    that cannot be attributed and placed in time is rejected rather than
    stored, because it could not be cited later and could not be filtered
    point-in-time.

    Not a substitute for a live provider and never labelled as one — its
    origin is ``HISTORICAL`` and its provider id says ``FIXTURE``.
    """

    provider_id = "FIXTURE_NEWS"
    display_name = "Recorded news fixtures"
    #: The tier of the *replay mechanism*. Each article carries its own
    #: original tier, which is what a consumer should trust.
    tier = SourceTier.TIER_4
    cost = ProviderCost.FREE
    origin = MarketDataOrigin.HISTORICAL
    notes = "Replays recorded articles. No live news provider is available for free."

    def __init__(
        self,
        directory: Path | str,
        *,
        clock: Clock | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self._directory = Path(directory)
        self._clock = clock or SystemClock()

    def availability(self) -> ProviderAvailability:
        return (
            ProviderAvailability.AVAILABLE
            if self._directory.is_dir()
            else ProviderAvailability.UNAVAILABLE
        )

    def fetch_news(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> ProviderResult[NewsArticle]:
        key = symbol.upper()
        path = self._directory / f"{key}.json"
        if not path.exists():
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.NEWS_ARTICLE,
                key=key,
                outcome=CollectionOutcome.NO_DATA,
                error=f"no recorded news fixture for {key}",
            )

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.NEWS_ARTICLE,
                key=key,
                outcome=CollectionOutcome.INVALID_DATA,
                error=f"corrupt news fixture {path}: {exc}",
            )

        retrieved_at = self._clock.now()
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.NEWS_ARTICLE,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            source_identifier=str(path),
            request={"symbol": key},
        )

        articles, rejected = self._parse(payload, key, retrieved_at, since=since, limit=limit)
        if not articles and rejected:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.NEWS_ARTICLE,
                key=key,
                outcome=CollectionOutcome.INVALID_DATA,
                error=f"every article in {path.name} was unusable: {rejected[0]}",
                raw=raw,
            )
        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.NEWS_ARTICLE,
            key=key,
            records=articles,
            raw=raw,
            partial=bool(rejected),
            notes=rejected,
        )

    def _parse(
        self,
        payload: Any,
        symbol: str,
        retrieved_at: datetime,
        *,
        since: datetime | None,
        limit: int,
    ) -> tuple[list[NewsArticle], list[str]]:
        rows: Sequence[Any] = payload if isinstance(payload, list) else []
        articles: list[NewsArticle] = []
        rejected: list[str] = []

        for index, row in enumerate(rows):
            if len(articles) >= limit:
                break
            if not isinstance(row, dict):
                rejected.append(f"entry {index} is not an object")
                continue
            published_at = _parse_timestamp(row.get("published_at"))
            headline = str(row.get("headline") or "").strip()
            url = str(row.get("url") or row.get("source_identifier") or "").strip()
            if not headline or published_at is None or not url:
                rejected.append(
                    f"entry {index} lacks a headline, publication time or URL and cannot be cited"
                )
                continue
            if since is not None and published_at < since:
                continue

            articles.append(
                NewsArticle(
                    as_of=published_at,
                    source=self.metadata(
                        retrieved_at=retrieved_at,
                        source_identifier=url,
                        published_at=published_at,
                        source_timestamp=published_at,
                    ).model_copy(
                        update={
                            # The article keeps the tier and name of whoever
                            # actually published it, not of the replayer.
                            "source_name": str(row.get("source_name") or "unknown"),
                            "source_tier": _parse_tier(row.get("source_tier")),
                        }
                    ),
                    article_id=str(row.get("article_id") or f"fixture:{stable_hash([url])}"),
                    headline=headline,
                    summary=_optional_str(row.get("summary")),
                    symbols=[str(s).upper() for s in row.get("symbols", [symbol])],
                    entities=[str(e) for e in row.get("entities", [])],
                    language=_optional_str(row.get("language")),
                    relevance=_parse_relevance(row.get("relevance")),
                )
            )
        return articles, rejected


class FixtureCorporateEventProvider(CorporateEventProvider):
    """Replays recorded corporate events from a directory of JSON files.

    Exists for the same reason as :class:`FixtureNewsProvider`: no free,
    reliable earnings-calendar API is available, so the interface and the
    storage path are delivered and exercised while the live source is
    deferred.

    ``announced_at`` is required on every entry. Without it there is no way to
    tell when the market learned of the event, and the record would leak into
    reconstructions of a time before it was known.
    """

    provider_id = "FIXTURE_EVENTS"
    display_name = "Recorded corporate event fixtures"
    tier = SourceTier.TIER_4
    cost = ProviderCost.FREE
    origin = MarketDataOrigin.HISTORICAL
    notes = "Replays recorded events. No free corporate calendar API is wired up."

    def __init__(
        self,
        directory: Path | str,
        *,
        clock: Clock | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self._directory = Path(directory)
        self._clock = clock or SystemClock()

    def availability(self) -> ProviderAvailability:
        return (
            ProviderAvailability.AVAILABLE
            if self._directory.is_dir()
            else ProviderAvailability.UNAVAILABLE
        )

    def fetch_events(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
    ) -> ProviderResult[CorporateEvent]:
        key = symbol.upper()
        path = self._directory / f"{key}.json"
        if not path.exists():
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.CORPORATE_EVENT,
                key=key,
                outcome=CollectionOutcome.NO_DATA,
                error=f"no recorded event fixture for {key}",
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.CORPORATE_EVENT,
                key=key,
                outcome=CollectionOutcome.INVALID_DATA,
                error=f"corrupt event fixture {path}: {exc}",
            )

        retrieved_at = self._clock.now()
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.CORPORATE_EVENT,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            source_identifier=str(path),
            request={"symbol": key},
        )

        events: list[CorporateEvent] = []
        rejected: list[str] = []
        for index, row in enumerate(payload if isinstance(payload, list) else []):
            if not isinstance(row, dict):
                rejected.append(f"entry {index} is not an object")
                continue
            event_time = _parse_timestamp(row.get("event_time"))
            announced_at = _parse_timestamp(row.get("announced_at"))
            if event_time is None or announced_at is None:
                rejected.append(
                    f"entry {index} lacks an event_time or announced_at and cannot be "
                    f"placed on a timeline"
                )
                continue
            if since is not None and announced_at < since:
                continue
            events.append(
                CorporateEvent(
                    as_of=announced_at,
                    source=self.metadata(
                        retrieved_at=retrieved_at,
                        source_identifier=str(row.get("url") or path),
                        published_at=announced_at,
                        source_timestamp=announced_at,
                    ),
                    event_id=str(row.get("event_id") or f"fixture:{stable_hash([key, index])}"),
                    event_type=_corporate_event_type(row.get("event_type")),
                    symbol=key,
                    event_time=event_time,
                    announced_at=announced_at,
                    confirmed=bool(row.get("confirmed", False)),
                    description=_optional_str(row.get("description")),
                    detail={str(k): str(v) for k, v in (row.get("detail") or {}).items()},
                )
            )

        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.CORPORATE_EVENT,
            key=key,
            records=events,
            raw=raw,
            partial=bool(rejected),
            notes=rejected,
        )


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive publication time has no defined position on the timeline, which
    # makes point-in-time filtering guesswork. Rejected rather than assumed.
    return parsed if parsed.tzinfo is not None else None


def _parse_tier(value: Any) -> SourceTier:
    if isinstance(value, str):
        try:
            return SourceTier(value.strip().upper())
        except ValueError:
            pass
    # Unknown provenance is Tier 4. Never promoted on a guess.
    return SourceTier.TIER_4


def _parse_relevance(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        relevance = float(value)
        return relevance if 0.0 <= relevance <= 1.0 else None
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _corporate_event_type(value: Any) -> CorporateEventType:
    if isinstance(value, str):
        try:
            return CorporateEventType(value.strip().upper())
        except ValueError:
            pass
    return CorporateEventType.OTHER
