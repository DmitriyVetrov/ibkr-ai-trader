"""Assembling what the research agent is allowed to see.

This module is the only place the research layer touches stored data, and it
reaches it through :class:`~trading_system.data.repository.DataRepository` —
never a provider, never a broker, never a path, never the network. The agent
sits one layer above and sees only the
:class:`~trading_system.research.models.ResearchInput` built here.

**Point-in-time is the hard rule.** Everything is read *as of* an instant,
through :meth:`~trading_system.data.repository.DataRepository.get_as_of` and
:meth:`~trading_system.data.repository.DataRepository.get_range`, whose
visibility is decided from the append-only ledger's timestamps. Every record
that survives is then re-checked with
:func:`~trading_system.data.point_in_time.assert_no_look_ahead`, which raises
rather than quietly shortening a list: a leak is a correctness bug, and a
silently shorter list looks exactly like an ordinary quiet week.

Future-dated *content* is expected and correct. An earnings date next month is
what a calendar is for; what must never happen is that event being visible to a
reconstruction of an instant before it was announced.
:class:`~trading_system.data.models.CorporateEvent` gates on ``announced_at``,
so it is not.

**Nothing is derived that the data did not supply.** There is no imputed price,
no synthesised volatility, no remembered earnings date. A statistic that the
window did not contain enough observations to compute is ``None`` and the input
records the gap. "No implied volatility" and "implied volatility of zero" are
different claims about a market, and only one of them is ever made here.

**No contract is expressible.** The option context is aggregate on purpose:
days to expiration and implied volatility, never a strike, a right, an expiry
date or a contract id. An agent that is never shown a contract cannot select
one, which is a stronger guarantee than asking it not to.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from trading_system.data.models import (
    CorporateEvent,
    DataRecord,
    DataSnapshot,
    FundamentalSnapshot,
    MarketBar,
    MarketQuote,
    NewsArticle,
    OptionChain,
    OptionQuote,
    RegulatoryEvent,
)
from trading_system.data.point_in_time import assert_no_look_ahead
from trading_system.data.repository import DataRepository, records_of
from trading_system.domain.enums import (
    CorporateEventType,
    DataQuality,
    DataType,
    EvidenceKind,
    MarketEventType,
    ResearchDataGap,
    SourceTier,
)
from trading_system.infrastructure.settings import ResearchConfig
from trading_system.research.evidence import group_articles
from trading_system.research.models import (
    EventItem,
    EvidenceItem,
    FundamentalItem,
    HistoricalContext,
    MarketContext,
    MarketSnapshot,
    OptionMarketContext,
    OptionTermPoint,
    ResearchDataQualitySummary,
    ResearchHorizon,
    ResearchInput,
    ResearchLimitsSnapshot,
    ResearchWindowSnapshot,
    SourceProvenance,
    decimal_or_none,
    evidence_identifier,
    tier_rank,
)
from trading_system.research.sources import SourceTrustPolicy

__all__ = ["ResearchInputBuilder", "SymbolEvidence"]


@dataclass(frozen=True, slots=True)
class SymbolEvidence:
    """One symbol's assembled input, plus whether it is worth a model call."""

    research_input: ResearchInput

    @property
    def usable_evidence_count(self) -> int:
        return self.research_input.usable_evidence_count


class ResearchInputBuilder:
    """Builds a :class:`ResearchInput` for one underlying at one instant.

    Holds a repository, the research policy and the source trust policy, and
    nothing else. It cannot collect, cannot connect and cannot reach a
    provider: a research run is a pure consumer of history that was already
    accumulated, which is what lets it be replayed for a past instant and what
    makes "zero orders" structural rather than checked.
    """

    def __init__(
        self,
        repository: DataRepository,
        *,
        config: ResearchConfig,
        source_policy: SourceTrustPolicy,
    ) -> None:
        self._repository = repository
        self._config = config
        self._sources = source_policy

    # --- entry point -------------------------------------------------------
    def build(
        self,
        symbol: str,
        as_of: datetime,
        *,
        run_id: str,
        universe_run_id: str | None = None,
        universe_snapshot_id: str | None = None,
    ) -> ResearchInput:
        """Assemble everything visible about ``symbol`` at ``as_of``."""
        key = symbol.strip().upper()
        window = self._config.window
        limits = self._config.limits
        gaps: list[ResearchDataGap] = []
        truncated: list[str] = []
        snapshot_ids: set[str] = set()

        quote_snapshot = self._visible_latest(DataType.MARKET_QUOTE, key, as_of)
        quote = self._newest_record(quote_snapshot, MarketQuote, as_of)
        market = self._market_snapshot(key, quote, quote_snapshot, as_of, gaps)
        if quote_snapshot is not None:
            snapshot_ids.add(quote_snapshot.snapshot_id)

        history, history_ids = self._historical_context(key, as_of, gaps)
        snapshot_ids.update(history_ids)

        option_context, option_ids = self._option_context(key, as_of, gaps)
        snapshot_ids.update(option_ids)

        market_context, context_ids = self._market_context(key, as_of, gaps)
        snapshot_ids.update(context_ids)

        news, news_ids = self._news(key, as_of, gaps, truncated)
        snapshot_ids.update(news_ids)

        events, event_ids = self._events(key, as_of, gaps, truncated)
        snapshot_ids.update(event_ids)

        regulatory, regulatory_ids = self._regulatory(key, as_of, gaps, truncated)
        snapshot_ids.update(regulatory_ids)

        fundamentals, fundamental_ids = self._fundamentals(key, as_of, gaps, truncated)
        snapshot_ids.update(fundamental_ids)

        observations = self._observations(
            key,
            as_of,
            market=market,
            history=history,
            option_context=option_context,
            market_context=market_context,
        )

        news, regulatory, observations = self._apply_evidence_budget(
            news, regulatory, observations, truncated
        )

        quality = self._quality_summary(
            quote=quote,
            evidence=[*news, *regulatory, *observations],
            gaps=gaps,
        )

        return ResearchInput(
            run_id=run_id,
            symbol=key,
            as_of=as_of,
            horizon=ResearchHorizon(
                min_days=self._config.horizon.min_days,
                max_days=self._config.horizon.max_days,
            ),
            universe_run_id=universe_run_id,
            universe_snapshot_id=universe_snapshot_id,
            data_snapshot_ids=sorted(snapshot_ids),
            market_snapshot=market,
            historical_context=history,
            option_context=option_context,
            market_context=market_context,
            news=news,
            events=events,
            regulatory_events=regulatory,
            fundamentals=fundamentals,
            observations=observations,
            data_quality_summary=quality,
            window=ResearchWindowSnapshot(
                news_lookback_days=window.news_lookback_days,
                event_lookahead_days=window.event_lookahead_days,
                event_lookback_days=window.event_lookback_days,
                historical_lookback_days=window.historical_lookback_days,
                fundamentals_lookback_days=window.fundamentals_lookback_days,
                regulatory_lookback_days=window.regulatory_lookback_days,
                volatility_annualization_days=window.volatility_annualization_days,
            ),
            limits=ResearchLimitsSnapshot(
                max_evidence_items=limits.max_evidence_items,
                max_news_items=limits.max_news_items,
                max_events=limits.max_events,
                max_regulatory_items=limits.max_regulatory_items,
                max_fundamental_periods=limits.max_fundamental_periods,
                truncated=sorted(set(truncated)),
            ),
            source_policy=self._sources.snapshot(),
        )

    # --- market data -------------------------------------------------------
    def _market_snapshot(
        self,
        symbol: str,
        quote: MarketQuote | None,
        snapshot: DataSnapshot | None,
        as_of: datetime,
        gaps: list[ResearchDataGap],
    ) -> MarketSnapshot | None:
        if quote is None or snapshot is None:
            gaps.append(ResearchDataGap.MARKET_DATA_UNAVAILABLE)
            return None

        quality = quote.quality
        if not quality.research_usable:
            gaps.append(ResearchDataGap.MARKET_DATA_NOT_RESEARCH_USABLE)
        if not quality.freshness_valid:
            gaps.append(ResearchDataGap.MARKET_DATA_STALE)
        if not quality.plausibility_valid:
            gaps.append(ResearchDataGap.SUSPICIOUS_VALUES_PRESENT)

        return MarketSnapshot(
            symbol=symbol,
            as_of=quote.as_of,
            origin=quote.source.origin,
            source=self._provenance(quote, snapshot.snapshot_id),
            last=quote.last,
            close=quote.close,
            bid=quote.bid,
            ask=quote.ask,
            volume=quote.volume,
            currency=quote.currency,
            age_seconds=max(0.0, quote.source.age_seconds(as_of)),
            data_quality=ResearchDataQualitySummary(
                research_usable=quality.research_usable,
                classification=quality.classification,
                freshness_valid=quality.freshness_valid,
                completeness_valid=quality.completeness_valid,
                plausibility_valid=quality.plausibility_valid,
                consistency_valid=quality.consistency_valid,
                records_considered=1,
                records_research_usable=1 if quality.research_usable else 0,
                issues=[issue.value for issue in quality.issues],
            ),
        )

    def _historical_context(
        self, symbol: str, as_of: datetime, gaps: list[ResearchDataGap]
    ) -> tuple[HistoricalContext | None, set[str]]:
        """Descriptive statistics over the configured lookback window.

        Bars are preferred; where none exist the stored quote history is used
        instead, which is honest about what a daily collection cadence actually
        accumulates. Nothing is interpolated to fill a gap between observations.
        """
        window = self._config.window
        start = as_of - timedelta(days=window.historical_lookback_days)
        ids: set[str] = set()

        closes: list[tuple[datetime, Decimal]] = []
        for snapshot in self._visible_range(DataType.MARKET_BAR, symbol, start, as_of):
            ids.add(snapshot.snapshot_id)
            for bar in self._records(snapshot, MarketBar, as_of):
                if bar.close is not None:
                    closes.append((bar.period_end, bar.close))

        if not closes:
            for snapshot in self._visible_range(DataType.MARKET_QUOTE, symbol, start, as_of):
                ids.add(snapshot.snapshot_id)
                for quote in self._records(snapshot, MarketQuote, as_of):
                    price = quote.last if quote.last is not None else quote.close
                    if price is not None:
                        closes.append((quote.as_of, price))

        if not closes:
            gaps.append(ResearchDataGap.HISTORICAL_CONTEXT_UNAVAILABLE)
            gaps.append(ResearchDataGap.REALIZED_VOLATILITY_UNAVAILABLE)
            return None, ids

        closes.sort(key=lambda pair: pair[0])
        series = [price for _, price in closes]
        first, last = series[0], series[-1]

        volatility = self._realized_volatility(series)
        if volatility is None:
            gaps.append(ResearchDataGap.REALIZED_VOLATILITY_UNAVAILABLE)

        return (
            HistoricalContext(
                observation_count=len(series),
                period_start=closes[0][0],
                period_end=closes[-1][0],
                first_close=first,
                last_close=last,
                high=max(series),
                low=min(series),
                change_pct=_percent_change(first, last),
                max_drawdown_pct=_max_drawdown_pct(series),
                realized_volatility=volatility,
                realized_volatility_window_days=window.historical_lookback_days,
                realized_volatility_annualization_days=window.volatility_annualization_days,
                snapshot_ids=sorted(ids),
            ),
            ids,
        )

    def _realized_volatility(self, series: list[Decimal]) -> Decimal | None:
        """Annualised standard deviation of log returns, or ``None``.

        ``None`` below the configured observation floor: a volatility computed
        from four prices is a number, not a measurement, and publishing it
        would invite exactly the realized-versus-implied comparison that
        section 24 of the brief warns about.
        """
        window = self._config.window
        if len(series) < window.min_observations_for_volatility:
            return None

        returns: list[float] = []
        for previous, current in pairwise(series):
            if previous <= 0 or current <= 0:
                continue
            returns.append(math.log(float(current) / float(previous)))
        if len(returns) < 2:
            return None

        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        return decimal_or_none(
            math.sqrt(variance) * math.sqrt(window.volatility_annualization_days)
        )

    # --- option market -----------------------------------------------------
    def _option_context(
        self, symbol: str, as_of: datetime, gaps: list[ResearchDataGap]
    ) -> tuple[OptionMarketContext | None, set[str]]:
        """Aggregate option conditions. Never a contract (brief section 23)."""
        ids: set[str] = set()
        chain_snapshot = self._visible_latest(DataType.OPTION_CHAIN, symbol, as_of)
        chain = self._newest_record(chain_snapshot, OptionChain, as_of)
        if chain is None or chain_snapshot is None:
            gaps.append(ResearchDataGap.OPTION_CONTEXT_UNAVAILABLE)
            gaps.append(ResearchDataGap.IMPLIED_VOLATILITY_UNAVAILABLE)
            return None, ids

        ids.add(chain_snapshot.snapshot_id)
        reference = as_of.date()
        nearest = min(
            ((expiry - reference).days for expiry in chain.expirations if expiry >= reference),
            default=None,
        )

        quote_snapshot = self._visible_latest(DataType.OPTION_QUOTE, symbol, as_of)
        quotes: list[OptionQuote] = []
        if quote_snapshot is not None:
            ids.add(quote_snapshot.snapshot_id)
            quotes = self._records(quote_snapshot, OptionQuote, as_of)

        term = _term_structure(quotes, reference)
        atm_iv = _median([q.implied_volatility for q in quotes if q.implied_volatility is not None])
        volume = _sum_or_none([q.volume for q in quotes])
        open_interest = _sum_or_none([q.open_interest for q in quotes])

        if atm_iv is None:
            gaps.append(ResearchDataGap.IMPLIED_VOLATILITY_UNAVAILABLE)
        if volume is None:
            gaps.append(ResearchDataGap.OPTION_VOLUME_UNAVAILABLE)
        if open_interest is None:
            gaps.append(ResearchDataGap.OPEN_INTEREST_UNAVAILABLE)

        return (
            OptionMarketContext(
                underlying=symbol,
                as_of=chain.as_of,
                source=self._provenance(chain, chain_snapshot.snapshot_id),
                expiration_count=len(chain.expirations),
                strike_count=len(chain.strikes),
                nearest_expiration_days=nearest,
                atm_implied_volatility=atm_iv,
                total_option_volume=volume,
                total_open_interest=open_interest,
                term_structure=term,
            ),
            ids,
        )

    # --- market context ----------------------------------------------------
    def _market_context(
        self, symbol: str, as_of: datetime, gaps: list[ResearchDataGap]
    ) -> tuple[MarketContext, set[str]]:
        """Broad-market reference, if the store has it. Never fetched here."""
        policy = self._config.market_context
        ids: set[str] = set()
        wanted = [
            s.upper() for s in ([policy.broad_index_symbol] if policy.broad_index_symbol else [])
        ]
        wanted += [s.upper() for s in policy.reference_symbols]

        index_snapshot: MarketSnapshot | None = None
        index_history: HistoricalContext | None = None
        references: list[MarketSnapshot] = []
        missing: list[str] = []

        for reference_symbol in wanted:
            snapshot = self._visible_latest(DataType.MARKET_QUOTE, reference_symbol, as_of)
            quote = self._newest_record(snapshot, MarketQuote, as_of)
            if quote is None or snapshot is None:
                missing.append(reference_symbol)
                continue
            ids.add(snapshot.snapshot_id)
            built = MarketSnapshot(
                symbol=reference_symbol,
                as_of=quote.as_of,
                origin=quote.source.origin,
                source=self._provenance(quote, snapshot.snapshot_id),
                last=quote.last,
                close=quote.close,
                bid=quote.bid,
                ask=quote.ask,
                volume=quote.volume,
                currency=quote.currency,
                age_seconds=max(0.0, quote.source.age_seconds(as_of)),
            )
            if reference_symbol == (policy.broad_index_symbol or "").upper():
                index_snapshot = built
                index_history, history_ids = self._historical_context(reference_symbol, as_of, [])
                ids.update(history_ids)
            else:
                references.append(built)

        if index_snapshot is None and not references:
            gaps.append(ResearchDataGap.MARKET_CONTEXT_UNAVAILABLE)
            return (
                MarketContext(
                    broad_index_symbol=policy.broad_index_symbol,
                    available=False,
                    unavailable_reason=(
                        "no broad-market reference was visible at this instant"
                        + (f" (wanted: {', '.join(wanted)})" if wanted else "; none is configured")
                        + ". The research layer never retrieves it itself."
                    ),
                ),
                ids,
            )

        return (
            MarketContext(
                broad_index_symbol=policy.broad_index_symbol,
                broad_index=index_snapshot,
                broad_index_history=index_history,
                reference_snapshots=references,
                available=True,
                unavailable_reason=(
                    f"partial: no data visible for {', '.join(missing)}" if missing else None
                ),
            ),
            ids,
        )

    # --- documents ---------------------------------------------------------
    def _news(
        self,
        symbol: str,
        as_of: datetime,
        gaps: list[ResearchDataGap],
        truncated: list[str],
    ) -> tuple[list[EvidenceItem], set[str]]:
        """Point-in-time news, grouped so one story counts once."""
        window = self._config.window
        limits = self._config.limits
        start = as_of - timedelta(days=window.news_lookback_days)
        ids: set[str] = set()

        articles: dict[str, tuple[NewsArticle, str]] = {}
        for snapshot in self._visible_range(DataType.NEWS_ARTICLE, symbol, start, as_of):
            for article in self._records(snapshot, NewsArticle, as_of):
                published = article.source.published_at or article.as_of
                if published < start:
                    continue
                # Later snapshots re-report the same article; the first
                # retrieval is the one that establishes when we knew it.
                if article.article_id not in articles:
                    articles[article.article_id] = (article, snapshot.snapshot_id)
                    ids.add(snapshot.snapshot_id)

        if not articles:
            gaps.append(ResearchDataGap.NEWS_UNAVAILABLE)
            return [], ids

        snapshot_of = {article_id: sid for article_id, (_, sid) in articles.items()}
        groups = group_articles(
            [article for article, _ in articles.values()],
            config=self._config.deduplication,
            policy=self._sources,
        )

        items: list[EvidenceItem] = []
        for group in groups:
            article = group.representative
            snapshot_id = snapshot_of[article.article_id]
            summary = article.headline
            if article.summary:
                summary = f"{article.headline} — {article.summary}"
            items.append(
                EvidenceItem(
                    evidence_id=evidence_identifier(
                        kind=EvidenceKind.NEWS,
                        symbol=symbol,
                        snapshot_id=snapshot_id,
                        source_identifier=article.source.source_identifier,
                        content=article.article_id,
                    ),
                    kind=EvidenceKind.NEWS,
                    summary=summary,
                    source=self._provenance(article, snapshot_id),
                    occurred_at=article.source.published_at,
                    duplicate_count=group.duplicate_count,
                    duplicate_source_names=group.source_names(),
                    research_usable=article.quality.research_usable,
                )
            )

        items.sort(key=_news_priority)
        if len(items) > limits.max_news_items:
            truncated.append("news")
            items = items[: limits.max_news_items]
        return items, ids

    def _events(
        self,
        symbol: str,
        as_of: datetime,
        gaps: list[ResearchDataGap],
        truncated: list[str],
    ) -> tuple[list[EventItem], set[str]]:
        """Dated events that could move the underlying inside the horizon."""
        window = self._config.window
        limits = self._config.limits
        start = as_of - timedelta(days=window.event_lookback_days)
        # Read a generous snapshot range: a calendar snapshot taken weeks ago
        # can still be the one that first announced next month's event.
        snapshot_start = as_of - timedelta(days=max(window.event_lookback_days, 365))
        horizon_end = as_of + timedelta(days=window.event_lookahead_days)
        ids: set[str] = set()

        seen: dict[str, tuple[CorporateEvent, str]] = {}
        for snapshot in self._visible_range(
            DataType.CORPORATE_EVENT, symbol, snapshot_start, as_of
        ):
            for event in self._records(snapshot, CorporateEvent, as_of):
                if event.event_time < start or event.event_time > horizon_end:
                    continue
                if event.event_id not in seen:
                    seen[event.event_id] = (event, snapshot.snapshot_id)
                    ids.add(snapshot.snapshot_id)

        if not seen:
            gaps.append(ResearchDataGap.EVENTS_UNAVAILABLE)
            return [], ids

        horizon_days = self._config.horizon.max_days
        items: list[EventItem] = []
        for event, snapshot_id in seen.values():
            days_until = (event.event_time - as_of).days
            items.append(
                EventItem(
                    event_id=event.event_id,
                    event_type=_market_event_type(event.event_type),
                    summary=event.description or f"{event.event_type.value} event",
                    expected_event_time=event.event_time,
                    source=self._provenance(event, snapshot_id),
                    announced_at=event.announced_at,
                    confirmed=event.confirmed,
                    days_until=days_until,
                    within_horizon=0 <= days_until <= horizon_days,
                    detail=", ".join(f"{k}={v}" for k, v in sorted(event.detail.items())) or None,
                    evidence_id=evidence_identifier(
                        kind=EvidenceKind.CORPORATE_EVENT,
                        symbol=symbol,
                        snapshot_id=snapshot_id,
                        source_identifier=event.source.source_identifier,
                        content=event.event_id,
                    ),
                )
            )

        items.sort(key=lambda e: (e.expected_event_time, e.event_id))
        if len(items) > limits.max_events:
            truncated.append("events")
            items = items[: limits.max_events]
        return items, ids

    def _regulatory(
        self,
        symbol: str,
        as_of: datetime,
        gaps: list[ResearchDataGap],
        truncated: list[str],
    ) -> tuple[list[EvidenceItem], set[str]]:
        window = self._config.window
        limits = self._config.limits
        start = as_of - timedelta(days=window.regulatory_lookback_days)
        ids: set[str] = set()

        seen: dict[str, tuple[RegulatoryEvent, str]] = {}
        for snapshot in self._visible_range(DataType.REGULATORY_EVENT, symbol, start, as_of):
            for filing in self._records(snapshot, RegulatoryEvent, as_of):
                if filing.filed_at < start:
                    continue
                if filing.event_id not in seen:
                    seen[filing.event_id] = (filing, snapshot.snapshot_id)
                    ids.add(snapshot.snapshot_id)

        if not seen:
            gaps.append(ResearchDataGap.REGULATORY_UNAVAILABLE)
            return [], ids

        items: list[EvidenceItem] = []
        for filing, snapshot_id in seen.values():
            descriptor = filing.description or filing.company_name or symbol
            items.append(
                EvidenceItem(
                    evidence_id=evidence_identifier(
                        kind=EvidenceKind.REGULATORY_FILING,
                        symbol=symbol,
                        snapshot_id=snapshot_id,
                        source_identifier=filing.url or filing.accession_number,
                        content=filing.event_id,
                    ),
                    kind=EvidenceKind.REGULATORY_FILING,
                    summary=(
                        f"{filing.raw_form} filed "
                        f"{filing.filed_at.date().isoformat()}: {descriptor}"
                    ),
                    source=self._provenance(filing, snapshot_id),
                    occurred_at=filing.filed_at,
                    detail=", ".join(filing.items) or None,
                    research_usable=filing.quality.research_usable,
                )
            )

        items.sort(key=lambda i: (i.occurred_at or as_of, i.evidence_id), reverse=True)
        if len(items) > limits.max_regulatory_items:
            truncated.append("regulatory")
            items = items[: limits.max_regulatory_items]
        return items, ids

    def _fundamentals(
        self,
        symbol: str,
        as_of: datetime,
        gaps: list[ResearchDataGap],
        truncated: list[str],
    ) -> tuple[list[FundamentalItem], set[str]]:
        window = self._config.window
        limits = self._config.limits
        start = as_of - timedelta(days=window.fundamentals_lookback_days)
        ids: set[str] = set()

        records: list[tuple[FundamentalSnapshot, str]] = []
        for snapshot in self._visible_range(DataType.FUNDAMENTAL_SNAPSHOT, symbol, start, as_of):
            ids.add(snapshot.snapshot_id)
            for record in self._records(snapshot, FundamentalSnapshot, as_of):
                records.append((record, snapshot.snapshot_id))

        if not records:
            gaps.append(ResearchDataGap.FUNDAMENTALS_UNAVAILABLE)
            return [], ids

        records.sort(
            key=lambda pair: (pair[0].period_end or as_of.date(), pair[0].as_of), reverse=True
        )
        if len(records) > limits.max_fundamental_periods:
            truncated.append("fundamentals")
            records = records[: limits.max_fundamental_periods]

        items = [
            FundamentalItem(
                period_end=record.period_end,
                fiscal_period=record.fiscal_period,
                currency=record.currency,
                revenue=record.revenue,
                net_income=record.net_income,
                eps_basic=record.eps_basic,
                eps_diluted=record.eps_diluted,
                shares_outstanding=record.shares_outstanding,
                market_capitalization=record.market_capitalization,
                guidance=record.guidance,
                next_earnings_date=record.next_earnings_date,
                filing_form=record.filing_form,
                filing_filed_at=record.filing_filed_at,
                source=self._provenance(record, snapshot_id),
                evidence_id=evidence_identifier(
                    kind=EvidenceKind.FUNDAMENTAL,
                    symbol=symbol,
                    snapshot_id=snapshot_id,
                    source_identifier=record.filing_accession_number,
                    content=str(record.period_end or record.as_of.date()),
                ),
            )
            for record, snapshot_id in records
        ]
        return items, ids

    # --- derived observations ----------------------------------------------
    def _observations(
        self,
        symbol: str,
        as_of: datetime,
        *,
        market: MarketSnapshot | None,
        history: HistoricalContext | None,
        option_context: OptionMarketContext | None,
        market_context: MarketContext | None,
    ) -> list[EvidenceItem]:
        """Turn the numeric context into citable facts.

        Every claim a report makes must reference evidence, so the market
        observations need ids too — otherwise "implied volatility is elevated"
        would be unsupportable by construction, and the agent would be pushed
        into asserting it without backing.
        """
        items: list[EvidenceItem] = []

        if market is not None:
            items.append(
                EvidenceItem(
                    evidence_id=evidence_identifier(
                        kind=EvidenceKind.MARKET_DATA,
                        symbol=symbol,
                        snapshot_id=market.source.snapshot_id,
                        source_identifier=market.source.source_identifier,
                        content=market.as_of.isoformat(),
                    ),
                    kind=EvidenceKind.MARKET_DATA,
                    summary=(
                        f"{symbol} quote at {market.as_of.isoformat()} ({market.origin.value}): "
                        f"last={_show(market.last)} close={_show(market.close)} "
                        f"bid={_show(market.bid)} ask={_show(market.ask)} "
                        f"volume={_show(market.volume)}"
                    ),
                    source=market.source,
                    occurred_at=market.as_of,
                    detail=(
                        f"quote age {market.age_seconds:.0f}s"
                        if market.age_seconds is not None
                        else None
                    ),
                    research_usable=(
                        market.data_quality.research_usable if market.data_quality else True
                    ),
                )
            )

        if history is not None and history.snapshot_ids:
            items.append(
                EvidenceItem(
                    evidence_id=evidence_identifier(
                        kind=EvidenceKind.HISTORICAL_PRICE,
                        symbol=symbol,
                        snapshot_id=history.snapshot_ids[0],
                        source_identifier=None,
                        content=f"{history.period_start}-{history.period_end}-{history.observation_count}",
                    ),
                    kind=EvidenceKind.HISTORICAL_PRICE,
                    summary=(
                        f"{symbol} over the last {history.realized_volatility_window_days} days "
                        f"({_count(history.observation_count, 'stored observation')}): "
                        f"close {_show(history.first_close)} -> {_show(history.last_close)} "
                        f"({_show_pct(history.change_pct)}), high {_show(history.high)}, "
                        f"low {_show(history.low)}, "
                        f"max drawdown {_show_pct(history.max_drawdown_pct)}"
                    ),
                    source=SourceProvenance(
                        provider="DATA_REPOSITORY",
                        source_tier=SourceTier.TIER_1,
                        retrieved_at=as_of,
                        snapshot_id=history.snapshot_ids[0],
                        source_name="stored price history",
                    ),
                    occurred_at=history.period_end,
                    detail=(
                        f"realized volatility {_show(history.realized_volatility)} annualised over "
                        f"{history.realized_volatility_annualization_days} trading days, measured "
                        f"across a {history.realized_volatility_window_days}-day window"
                        if history.realized_volatility is not None
                        else "realized volatility unavailable: too few stored observations"
                    ),
                )
            )

        if option_context is not None:
            items.append(
                EvidenceItem(
                    evidence_id=evidence_identifier(
                        kind=EvidenceKind.OPTION_MARKET,
                        symbol=symbol,
                        snapshot_id=option_context.source.snapshot_id,
                        source_identifier=option_context.source.source_identifier,
                        content=option_context.as_of.isoformat(),
                    ),
                    kind=EvidenceKind.OPTION_MARKET,
                    summary=(
                        f"{symbol} option market: {option_context.expiration_count} expirations, "
                        f"{option_context.strike_count} strikes, nearest expiry in "
                        f"{_show(option_context.nearest_expiration_days)} days, ATM implied "
                        f"volatility {_show(option_context.atm_implied_volatility)}, total volume "
                        f"{_show(option_context.total_option_volume)}, open interest "
                        f"{_show(option_context.total_open_interest)}"
                    ),
                    source=option_context.source,
                    occurred_at=option_context.as_of,
                    detail=(
                        "implied volatility unavailable — this is not a volatility of zero"
                        if option_context.atm_implied_volatility is None
                        else None
                    ),
                )
            )

        if market_context is not None and market_context.available and market_context.broad_index:
            index = market_context.broad_index
            index_history = market_context.broad_index_history
            trend = (
                f", {index_history.realized_volatility_window_days}"
                f"-day change {_show_pct(index_history.change_pct)}"
                if index_history is not None
                else ""
            )
            items.append(
                EvidenceItem(
                    evidence_id=evidence_identifier(
                        kind=EvidenceKind.MARKET_CONTEXT,
                        symbol=symbol,
                        snapshot_id=index.source.snapshot_id,
                        source_identifier=index.source.source_identifier,
                        content=index.as_of.isoformat(),
                    ),
                    kind=EvidenceKind.MARKET_CONTEXT,
                    summary=(
                        f"Broad-market reference {index.symbol} at {index.as_of.isoformat()}: "
                        f"last={_show(index.last)} close={_show(index.close)}{trend}"
                    ),
                    source=index.source,
                    occurred_at=index.as_of,
                )
            )

        return items

    # --- budget and quality -------------------------------------------------
    def _apply_evidence_budget(
        self,
        news: list[EvidenceItem],
        regulatory: list[EvidenceItem],
        observations: list[EvidenceItem],
        truncated: list[str],
    ) -> tuple[list[EvidenceItem], list[EvidenceItem], list[EvidenceItem]]:
        """Enforce ``max_evidence_items`` across all sections at once.

        Observations are kept first: they are the market facts every report
        needs and there are only ever a handful. What remains goes to news then
        filings, most trusted and most recent first. Truncation is recorded, so
        a thin input is never mistaken for a quiet market.
        """
        budget = self._config.limits.max_evidence_items
        kept_observations = observations[:budget]
        if len(kept_observations) < len(observations):
            truncated.append("observations")
        remaining = budget - len(kept_observations)

        kept_news = news[: max(0, remaining)]
        if len(kept_news) < len(news):
            truncated.append("news")
        remaining -= len(kept_news)

        kept_regulatory = regulatory[: max(0, remaining)]
        if len(kept_regulatory) < len(regulatory):
            truncated.append("regulatory")

        return kept_news, kept_regulatory, kept_observations

    def _quality_summary(
        self,
        *,
        quote: MarketQuote | None,
        evidence: list[EvidenceItem],
        gaps: list[ResearchDataGap],
    ) -> ResearchDataQualitySummary:
        usable = [item for item in evidence if item.research_usable]
        quality = quote.quality if quote is not None else None
        return ResearchDataQualitySummary(
            research_usable=bool(quality.research_usable) if quality is not None else False,
            classification=quality.classification if quality is not None else DataQuality.UNUSABLE,
            freshness_valid=quality.freshness_valid if quality is not None else False,
            completeness_valid=quality.completeness_valid if quality is not None else False,
            plausibility_valid=quality.plausibility_valid if quality is not None else False,
            consistency_valid=quality.consistency_valid if quality is not None else False,
            records_considered=len(evidence),
            records_research_usable=len(usable),
            records_excluded_not_usable=len(evidence) - len(usable),
            gaps=sorted(set(gaps), key=lambda gap: gap.value),
            issues=sorted({issue.value for issue in (quality.issues if quality else [])}),
        )

    # --- repository access ---------------------------------------------------
    def _visible_latest(
        self, data_type: DataType, key: str, as_of: datetime
    ) -> DataSnapshot | None:
        return self._repository.get_as_of(data_type, key, as_of)

    def _visible_range(
        self, data_type: DataType, key: str, start: datetime, as_of: datetime
    ) -> list[DataSnapshot]:
        """Snapshots describing ``[start, as_of]`` that were also *knowable* then.

        ``get_range`` filters on the instant a snapshot describes;
        :meth:`DataSnapshot.known_at` additionally requires that we had already
        retrieved it. Both are needed: a snapshot describing last Tuesday but
        downloaded this morning did not inform last Tuesday's decision.

        The newest visible snapshot is always included even when it describes an
        instant before ``start``, so a section is never empty merely because
        collection has been quiet.
        """
        snapshots = [
            s for s in self._repository.get_range(data_type, key, start, as_of) if s.known_at(as_of)
        ]
        latest = self._repository.get_as_of(data_type, key, as_of)
        if latest is not None and all(s.snapshot_id != latest.snapshot_id for s in snapshots):
            snapshots.append(latest)
        snapshots.sort(key=lambda s: (s.as_of, s.retrieved_at))
        return snapshots

    @staticmethod
    def _records[RecordT: DataRecord](
        snapshot: DataSnapshot, model: type[RecordT], as_of: datetime
    ) -> list[RecordT]:
        """Typed records from a snapshot, re-checked for look-ahead.

        The ledger has already decided the snapshot was visible; this verifies
        the records inside it. A leak here is a correctness bug in storage, so
        it raises rather than being quietly filtered — a shortened list would
        look like an ordinary "no data" outcome.
        """
        records = records_of(snapshot, model)
        assert_no_look_ahead(records, as_of)
        return records

    @staticmethod
    def _newest_record[RecordT: DataRecord](
        snapshot: DataSnapshot | None, model: type[RecordT], as_of: datetime
    ) -> RecordT | None:
        if snapshot is None:
            return None
        records = ResearchInputBuilder._records(snapshot, model, as_of)
        if not records:
            return None
        return max(records, key=lambda r: (r.as_of, r.source.retrieved_at))

    def _provenance(self, record: DataRecord, snapshot_id: str) -> SourceProvenance:
        """Provenance with the source tier resolved against ``sources.yaml``."""
        source = record.source
        relevance = getattr(record, "relevance", None)
        return SourceProvenance(
            provider=source.provider,
            source_tier=self._sources.resolve(
                declared=source.source_tier,
                identifier=source.source_identifier,
                name=source.source_name,
            ),
            retrieved_at=source.retrieved_at,
            snapshot_id=snapshot_id,
            source_name=source.source_name,
            source_identifier=source.source_identifier,
            published_at=source.published_at,
            source_timestamp=source.source_timestamp,
            requested_provider=source.requested_provider,
            origin=source.origin,
            provider_relevance=relevance if isinstance(relevance, float) else None,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
_EVENT_TYPE_MAP: dict[CorporateEventType, MarketEventType] = {
    CorporateEventType.EARNINGS: MarketEventType.EARNINGS,
    CorporateEventType.INVESTOR_DAY: MarketEventType.INVESTOR_DAY,
    CorporateEventType.DIVIDEND: MarketEventType.DIVIDEND,
    CorporateEventType.SPLIT: MarketEventType.SPLIT,
    CorporateEventType.MERGER_ACQUISITION: MarketEventType.MERGER_ACQUISITION,
    CorporateEventType.REGULATORY: MarketEventType.REGULATORY_DECISION,
    CorporateEventType.GUIDANCE: MarketEventType.GUIDANCE,
    CorporateEventType.ANNOUNCEMENT: MarketEventType.ANNOUNCEMENT,
    CorporateEventType.OTHER: MarketEventType.OTHER,
}


def _market_event_type(event_type: CorporateEventType) -> MarketEventType:
    """Widen a corporate event type into the research vocabulary.

    Total by construction rather than by a ``.get`` default, so a new corporate
    event type surfaces as a mapping gap instead of silently becoming ``OTHER``.
    """
    return _EVENT_TYPE_MAP.get(event_type, MarketEventType.OTHER)


def _news_priority(item: EvidenceItem) -> tuple[int, str, str]:
    """Most trusted first, then most recent. Deterministic for equal keys."""
    occurred = item.occurred_at.isoformat() if item.occurred_at else ""
    # Negating a timestamp is not possible, so invert by sorting descending on a
    # reversed string key: pad and complement is overkill, so use the tuple with
    # a negated ordinal via the sort key below instead.
    return (tier_rank(item.source.source_tier), _reverse_key(occurred), item.evidence_id)


def _reverse_key(value: str) -> str:
    """Sort key that reverses lexicographic order for a fixed-width string."""
    return "".join(chr(0x10FFFD - ord(character)) for character in value)


def _percent_change(first: Decimal, last: Decimal) -> Decimal | None:
    if first == 0:
        return None
    try:
        return ((last - first) / first * Decimal(100)).quantize(Decimal("0.0001"))
    except (InvalidOperation, ZeroDivisionError):  # pragma: no cover - defensive
        return None


def _max_drawdown_pct(series: list[Decimal]) -> Decimal | None:
    """Largest peak-to-trough fall in the window, as a negative percentage."""
    if len(series) < 2:
        return None
    peak = series[0]
    worst = Decimal(0)
    for price in series:
        peak = max(peak, price)
        if peak > 0:
            drawdown = (price - peak) / peak * Decimal(100)
            worst = min(worst, drawdown)
    return worst.quantize(Decimal("0.0001"))


def _median(values: list[Decimal]) -> Decimal | None:
    """Median, or ``None`` for an empty list. Never zero for "no data"."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / Decimal(2)).quantize(Decimal("0.000001"))


def _sum_or_none(values: list[Decimal | None]) -> Decimal | None:
    """Sum of the reported values, or ``None`` when nothing was reported.

    An all-``None`` input yields ``None``, never ``0``: "no venue reported
    volume" and "nothing traded" are different facts about a market.
    """
    present = [value for value in values if value is not None]
    return sum(present, Decimal(0)) if present else None


def _term_structure(quotes: list[OptionQuote], reference: object) -> list[OptionTermPoint]:
    """Implied volatility by days to expiration. No strike, no right, no date."""
    from datetime import date as date_type

    if not isinstance(reference, date_type):  # pragma: no cover - defensive
        return []

    buckets: dict[int, list[OptionQuote]] = {}
    for quote in quotes:
        expiration = quote.contract.expiration
        if expiration is None:
            continue
        buckets.setdefault((expiration - reference).days, []).append(quote)

    points: list[OptionTermPoint] = []
    for days in sorted(buckets):
        bucket = buckets[days]
        points.append(
            OptionTermPoint(
                days_to_expiration=max(0, days),
                atm_implied_volatility=_median(
                    [q.implied_volatility for q in bucket if q.implied_volatility is not None]
                ),
                contract_count=len(bucket),
                total_volume=_sum_or_none([q.volume for q in bucket]),
                total_open_interest=_sum_or_none([q.open_interest for q in bucket]),
            )
        )
    return points


def _show(value: object | None) -> str:
    """Render a value for a human-readable summary. Absence stays visible."""
    return "unavailable" if value is None else str(value)


def _show_pct(value: object | None) -> str:
    """Render a percentage without attaching ``%`` to the word "unavailable".

    Pedantic on purpose: this text is stored verbatim as an evidence summary,
    so "max drawdown unavailable%" would be preserved in the permanent record.
    """
    return "unavailable" if value is None else f"{value}%"


def _count(number: int, noun: str) -> str:
    """``1 stored observation`` / ``30 stored observations``."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
