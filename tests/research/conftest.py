"""Fixtures for the research suites.

Three rules hold across every test here, the same ones the data and universe
suites use:

* **No network, no broker, no model.** The research service is constructed with
  a fake :class:`LLMClient` or none at all. Nothing in these tests can reach an
  API, and no test needs a credential.
* **No writing into the repository's own ``data/``.** Every store is rooted at
  ``tmp_path``, so a run leaves nothing behind and cannot be influenced by an
  artifact a previous run left.
* **Evidence is stored through the real repository.** These tests persist
  quotes, chains, news, events, filings and fundamentals the way the collector
  would, so the point-in-time rules, the hashing and the ledger are all
  exercised. An assembler that only works against a hand-built object has not
  been tested against the thing it will actually see.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.agents.base import (
    AgentUnavailableError,
    LLMResponse,
    ModelIdentity,
    StructuredRequest,
)
from trading_system.data.models import (
    CorporateEvent,
    DataQualityReport,
    DataRecord,
    DataSourceMetadata,
    FundamentalSnapshot,
    MarketQuote,
    NewsArticle,
    OptionChain,
    OptionContract,
    OptionQuote,
    RegulatoryEvent,
)
from trading_system.data.repository import FilesystemDataRepository, build_snapshot
from trading_system.domain.enums import (
    ConfidenceLevel,
    CorporateEventType,
    DataType,
    MarketDataOrigin,
    OptionRight,
    RegulatoryFormType,
    SecurityType,
    SelectionMethod,
    SourceTier,
    UniverseEligibility,
    UniverseSelectionReason,
    UniverseSelectionStatus,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import (
    ResearchAgentConfig,
    ResearchConfidenceConfig,
    ResearchConfig,
    ResearchEvidenceConfig,
    ResearchHorizonConfig,
    ResearchLimitsConfig,
    ResearchWindowConfig,
    Settings,
    SystemConfig,
)
from trading_system.research.service import ResearchService
from trading_system.research.store import FilesystemResearchRepository
from trading_system.universe.models import (
    CandidateProvenance,
    DataQualitySummary,
    FilterConfigSnapshot,
    SelectedAsset,
    UniverseSelectionResult,
    UniverseSourceRef,
)
from trading_system.universe.store import FilesystemUniverseRepository

#: A weekday inside the calendar's covered years, during US market hours.
RESEARCH_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

#: Replaced by :class:`FakeLLMClient` with the run id the service derived. Run
#: ids are content-derived, so a fixture cannot predict one and hard-coding it
#: would make every test brittle to an unrelated change in what a run consumes.
RUN_ID_PLACEHOLDER = "__RUN_ID__"


@pytest.fixture
def research_now() -> datetime:
    return RESEARCH_NOW


@pytest.fixture
def research_clock() -> FixedClock:
    return FixedClock(RESEARCH_NOW)


@pytest.fixture
def data_repo(tmp_path: Path, research_clock: FixedClock) -> FilesystemDataRepository:
    return FilesystemDataRepository(tmp_path / "data", clock=research_clock)


@pytest.fixture
def universe_repo(tmp_path: Path) -> FilesystemUniverseRepository:
    return FilesystemUniverseRepository(tmp_path / "data" / "universe")


@pytest.fixture
def research_repo(tmp_path: Path) -> FilesystemResearchRepository:
    return FilesystemResearchRepository(tmp_path / "data" / "research")


# ---------------------------------------------------------------------------
# Storing evidence through the real repository
# ---------------------------------------------------------------------------
def _metadata(
    *,
    provider: str,
    tier: SourceTier,
    retrieved_at: datetime,
    origin: MarketDataOrigin = MarketDataOrigin.PROVIDER_DELAYED,
    published_at: datetime | None = None,
    identifier: str | None = None,
    name: str | None = None,
) -> DataSourceMetadata:
    return DataSourceMetadata(
        provider=provider,
        source_name=name or provider,
        source_tier=tier,
        origin=origin,
        retrieved_at=retrieved_at,
        source_timestamp=published_at,
        published_at=published_at,
        source_identifier=identifier,
    )


def _store(
    repo: FilesystemDataRepository,
    *,
    data_type: DataType,
    key: str,
    records: Sequence[DataRecord],
    provider: str,
    tier: SourceTier,
    as_of: datetime,
    retrieved_at: datetime,
    origin: MarketDataOrigin = MarketDataOrigin.PROVIDER_DELAYED,
) -> str:
    snapshot = build_snapshot(
        data_type=data_type,
        key=key.upper(),
        records=records,
        provider=provider,
        source_tier=tier,
        origin=origin,
        as_of=as_of,
        retrieved_at=retrieved_at,
        quality=DataQualityReport(evaluated_at=retrieved_at),
    )
    repo.save_snapshot(snapshot)
    return snapshot.snapshot_id


@pytest.fixture
def store_quote(data_repo: FilesystemDataRepository) -> Callable[..., MarketQuote]:
    """Persist a market quote snapshot the way the collector would."""

    def _make(
        symbol: str = "NVDA",
        *,
        as_of: datetime = RESEARCH_NOW,
        retrieved_at: datetime | None = None,
        last: Decimal | None = Decimal("180.25"),
        close: Decimal | None = Decimal("179.10"),
        bid: Decimal | None = Decimal("180.20"),
        ask: Decimal | None = Decimal("180.30"),
        volume: Decimal | None = Decimal("240000000"),
        research_usable: bool = True,
        freshness_valid: bool = True,
        plausibility_valid: bool = True,
        provider: str = "IBKR",
    ) -> MarketQuote:
        retrieved = retrieved_at or as_of
        quote = MarketQuote(
            as_of=as_of,
            source=_metadata(
                provider=provider,
                tier=SourceTier.TIER_1,
                retrieved_at=retrieved,
                origin=MarketDataOrigin.BROKER_DELAYED,
                published_at=as_of,
                identifier=f"{provider.lower()}:{symbol}",
            ),
            symbol=symbol.upper(),
            security_type=SecurityType.STOCK,
            currency="USD",
            exchange="NASDAQ",
            bid=bid,
            ask=ask,
            last=last,
            close=close,
            volume=volume,
            quality=DataQualityReport(
                research_usable=research_usable,
                freshness_valid=freshness_valid,
                plausibility_valid=plausibility_valid,
                evaluated_at=retrieved,
            ),
        )
        _store(
            data_repo,
            data_type=DataType.MARKET_QUOTE,
            key=symbol,
            records=[quote],
            provider=provider,
            tier=SourceTier.TIER_1,
            as_of=as_of,
            retrieved_at=retrieved,
            origin=MarketDataOrigin.BROKER_DELAYED,
        )
        return quote

    return _make


@pytest.fixture
def store_price_history(
    data_repo: FilesystemDataRepository,
) -> Callable[..., list[MarketQuote]]:
    """A run of daily quote snapshots, so historical context can be computed.

    Deliberately built from quotes rather than bars: that is what a daily
    collection cadence actually accumulates today, and the assembler must work
    against the store as it really is.
    """

    def _make(
        symbol: str = "NVDA",
        *,
        days: int = 30,
        start_price: Decimal = Decimal("150.00"),
        step: Decimal = Decimal("1.00"),
        end: datetime = RESEARCH_NOW,
    ) -> list[MarketQuote]:
        quotes: list[MarketQuote] = []
        for index in range(days):
            moment = end - timedelta(days=days - index)
            price = start_price + step * index
            quote = MarketQuote(
                as_of=moment,
                source=_metadata(
                    provider="IBKR",
                    tier=SourceTier.TIER_1,
                    retrieved_at=moment,
                    origin=MarketDataOrigin.BROKER_DELAYED,
                    published_at=moment,
                    identifier=f"ibkr:{symbol}",
                ),
                symbol=symbol.upper(),
                security_type=SecurityType.STOCK,
                currency="USD",
                last=price,
                close=price,
                volume=Decimal("200000000"),
                quality=DataQualityReport(evaluated_at=moment),
            )
            _store(
                data_repo,
                data_type=DataType.MARKET_QUOTE,
                key=symbol,
                records=[quote],
                provider="IBKR",
                tier=SourceTier.TIER_1,
                as_of=moment,
                retrieved_at=moment,
                origin=MarketDataOrigin.BROKER_DELAYED,
            )
            quotes.append(quote)
        return quotes

    return _make


@pytest.fixture
def store_news(data_repo: FilesystemDataRepository) -> Callable[..., NewsArticle]:
    """Persist one news article, with a real publication time and a real tier."""

    def _make(
        symbol: str = "NVDA",
        *,
        article_id: str = "reuters-1",
        headline: str = "Nvidia data-centre revenue accelerates, company says",
        summary: str | None = "Continued growth in the data-centre segment.",
        published_at: datetime | None = None,
        retrieved_at: datetime | None = None,
        source_name: str = "Reuters",
        url: str = "https://www.reuters.com/technology/nvda-2026-08-09/",
        tier: SourceTier = SourceTier.TIER_2,
        research_usable: bool = True,
        relevance: float | None = 0.9,
    ) -> NewsArticle:
        published = published_at or (RESEARCH_NOW - timedelta(days=1))
        retrieved = retrieved_at or published
        article = NewsArticle(
            as_of=published,
            source=_metadata(
                provider="FIXTURE_NEWS",
                tier=tier,
                retrieved_at=retrieved,
                origin=MarketDataOrigin.HISTORICAL,
                published_at=published,
                identifier=url,
                name=source_name,
            ),
            article_id=article_id,
            headline=headline,
            summary=summary,
            symbols=[symbol.upper()],
            relevance=relevance,
            quality=DataQualityReport(research_usable=research_usable, evaluated_at=retrieved),
        )
        _store(
            data_repo,
            data_type=DataType.NEWS_ARTICLE,
            key=symbol,
            records=[article],
            provider="FIXTURE_NEWS",
            tier=tier,
            as_of=published,
            retrieved_at=retrieved,
            origin=MarketDataOrigin.HISTORICAL,
        )
        return article

    return _make


@pytest.fixture
def store_event(data_repo: FilesystemDataRepository) -> Callable[..., CorporateEvent]:
    """Persist one corporate event. announced_at is what makes it visible."""

    def _make(
        symbol: str = "NVDA",
        *,
        event_id: str = "nvda-q2-earnings",
        event_type: CorporateEventType = CorporateEventType.EARNINGS,
        event_time: datetime | None = None,
        announced_at: datetime | None = None,
        retrieved_at: datetime | None = None,
        confirmed: bool = True,
        description: str = "Q2 FY2027 results",
    ) -> CorporateEvent:
        when = event_time or (RESEARCH_NOW + timedelta(days=17))
        announced = announced_at if announced_at is not None else RESEARCH_NOW - timedelta(days=4)
        retrieved = retrieved_at or (announced or RESEARCH_NOW - timedelta(days=4))
        event = CorporateEvent(
            as_of=announced or retrieved,
            source=_metadata(
                provider="FIXTURE_EVENTS",
                tier=SourceTier.TIER_1,
                retrieved_at=retrieved,
                origin=MarketDataOrigin.HISTORICAL,
                published_at=announced,
                identifier=f"https://investor.example.com/{event_id}",
                name="Company investor relations",
            ),
            event_id=event_id,
            event_type=event_type,
            symbol=symbol.upper(),
            event_time=when,
            announced_at=announced,
            confirmed=confirmed,
            description=description,
            quality=DataQualityReport(evaluated_at=retrieved),
        )
        _store(
            data_repo,
            data_type=DataType.CORPORATE_EVENT,
            key=symbol,
            records=[event],
            provider="FIXTURE_EVENTS",
            tier=SourceTier.TIER_1,
            as_of=event.as_of,
            retrieved_at=retrieved,
            origin=MarketDataOrigin.HISTORICAL,
        )
        return event

    return _make


@pytest.fixture
def store_filing(data_repo: FilesystemDataRepository) -> Callable[..., RegulatoryEvent]:
    def _make(
        symbol: str = "NVDA",
        *,
        event_id: str = "0000123-26-000001",
        form: RegulatoryFormType = RegulatoryFormType.FORM_10Q,
        filed_at: datetime | None = None,
        retrieved_at: datetime | None = None,
    ) -> RegulatoryEvent:
        filed = filed_at or (RESEARCH_NOW - timedelta(days=20))
        retrieved = retrieved_at or filed
        filing = RegulatoryEvent(
            as_of=filed,
            source=_metadata(
                provider="SEC_EDGAR",
                tier=SourceTier.TIER_1,
                retrieved_at=retrieved,
                origin=MarketDataOrigin.HISTORICAL,
                published_at=filed,
                identifier=f"https://www.sec.gov/Archives/{event_id}",
                name="SEC EDGAR",
            ),
            event_id=event_id,
            symbol=symbol.upper(),
            form_type=form,
            raw_form=form.value,
            filed_at=filed,
            accession_number=event_id,
            url=f"https://www.sec.gov/Archives/{event_id}",
            description="Quarterly report",
            quality=DataQualityReport(evaluated_at=retrieved),
        )
        _store(
            data_repo,
            data_type=DataType.REGULATORY_EVENT,
            key=symbol,
            records=[filing],
            provider="SEC_EDGAR",
            tier=SourceTier.TIER_1,
            as_of=filed,
            retrieved_at=retrieved,
            origin=MarketDataOrigin.HISTORICAL,
        )
        return filing

    return _make


@pytest.fixture
def store_fundamentals(
    data_repo: FilesystemDataRepository,
) -> Callable[..., FundamentalSnapshot]:
    def _make(
        symbol: str = "NVDA",
        *,
        period_end: date = date(2026, 6, 30),
        revenue: Decimal | None = Decimal("30000000000"),
        retrieved_at: datetime | None = None,
    ) -> FundamentalSnapshot:
        retrieved = retrieved_at or (RESEARCH_NOW - timedelta(days=20))
        record = FundamentalSnapshot(
            as_of=retrieved,
            source=_metadata(
                provider="SEC_XBRL",
                tier=SourceTier.TIER_1,
                retrieved_at=retrieved,
                origin=MarketDataOrigin.HISTORICAL,
                published_at=retrieved,
                identifier="https://data.sec.gov/api/xbrl/companyfacts",
                name="SEC XBRL",
            ),
            symbol=symbol.upper(),
            currency="USD",
            fiscal_period="Q2 FY2027",
            period_end=period_end,
            revenue=revenue,
            quality=DataQualityReport(evaluated_at=retrieved),
        )
        _store(
            data_repo,
            data_type=DataType.FUNDAMENTAL_SNAPSHOT,
            key=symbol,
            records=[record],
            provider="SEC_XBRL",
            tier=SourceTier.TIER_1,
            as_of=retrieved,
            retrieved_at=retrieved,
            origin=MarketDataOrigin.HISTORICAL,
        )
        return record

    return _make


@pytest.fixture
def store_chain(data_repo: FilesystemDataRepository) -> Callable[..., OptionChain]:
    def _make(
        symbol: str = "NVDA",
        *,
        as_of: datetime = RESEARCH_NOW,
        expirations: list[date] | None = None,
    ) -> OptionChain:
        chain = OptionChain(
            as_of=as_of,
            source=_metadata(
                provider="IBKR",
                tier=SourceTier.TIER_1,
                retrieved_at=as_of,
                origin=MarketDataOrigin.BROKER_DELAYED,
                published_at=as_of,
                identifier=f"ibkr:chain:{symbol}",
            ),
            underlying=symbol.upper(),
            exchange="SMART",
            trading_class=symbol.upper(),
            multiplier=100,
            expirations=expirations or [date(2026, 8, 28), date(2026, 9, 18)],
            strikes=[Decimal("170"), Decimal("180"), Decimal("190")],
            rights=[OptionRight.CALL, OptionRight.PUT],
        )
        _store(
            data_repo,
            data_type=DataType.OPTION_CHAIN,
            key=symbol,
            records=[chain],
            provider="IBKR",
            tier=SourceTier.TIER_1,
            as_of=as_of,
            retrieved_at=as_of,
            origin=MarketDataOrigin.BROKER_DELAYED,
        )
        return chain

    return _make


@pytest.fixture
def store_option_quotes(
    data_repo: FilesystemDataRepository,
) -> Callable[..., list[OptionQuote]]:
    def _make(
        symbol: str = "NVDA",
        *,
        as_of: datetime = RESEARCH_NOW,
        implied_volatility: Decimal | None = Decimal("0.42"),
        volume: Decimal | None = Decimal("1200"),
        open_interest: Decimal | None = Decimal("8400"),
    ) -> list[OptionQuote]:
        quotes = [
            OptionQuote(
                as_of=as_of,
                source=_metadata(
                    provider="IBKR",
                    tier=SourceTier.TIER_1,
                    retrieved_at=as_of,
                    origin=MarketDataOrigin.BROKER_DELAYED,
                    published_at=as_of,
                    identifier=f"ibkr:option:{symbol}:{strike}",
                ),
                contract=OptionContract(
                    underlying=symbol.upper(),
                    symbol=symbol.upper(),
                    expiration=date(2026, 8, 28),
                    strike=strike,
                    right=OptionRight.CALL,
                    multiplier=100,
                ),
                implied_volatility=implied_volatility,
                volume=volume,
                open_interest=open_interest,
                quality=DataQualityReport(evaluated_at=as_of),
            )
            for strike in (Decimal("170"), Decimal("180"))
        ]
        _store(
            data_repo,
            data_type=DataType.OPTION_QUOTE,
            key=symbol,
            records=quotes,
            provider="IBKR",
            tier=SourceTier.TIER_1,
            as_of=as_of,
            retrieved_at=as_of,
            origin=MarketDataOrigin.BROKER_DELAYED,
        )
        return quotes

    return _make


@pytest.fixture
def researchable_symbol(
    store_quote: Callable[..., MarketQuote],
    store_news: Callable[..., NewsArticle],
    store_event: Callable[..., CorporateEvent],
    store_chain: Callable[..., OptionChain],
) -> Callable[..., str]:
    """A symbol with enough stored evidence to produce a real research input."""

    def _make(symbol: str = "NVDA") -> str:
        store_quote(symbol)
        store_chain(symbol)
        store_news(symbol, article_id=f"{symbol.lower()}-news-1")
        store_event(symbol, event_id=f"{symbol.lower()}-earnings")
        return symbol.upper()

    return _make


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
@pytest.fixture
def store_universe(
    universe_repo: FilesystemUniverseRepository,
) -> Callable[..., UniverseSelectionResult]:
    """Persist a universe run for the research service to consume."""

    def _make(
        symbols: list[str] | None = None,
        *,
        as_of: datetime = RESEARCH_NOW,
        status: UniverseSelectionStatus = UniverseSelectionStatus.SUCCESS,
        run_id: str = "universe-run-test",
    ) -> UniverseSelectionResult:
        chosen = symbols if symbols is not None else ["NVDA"]
        quality = DataQualitySummary(research_usable=True, classification="OK")
        succeeded = status is UniverseSelectionStatus.SUCCESS
        result = UniverseSelectionResult(
            snapshot_id=f"universe-snapshot-{run_id}",
            run_id=run_id,
            as_of=as_of,
            generated_at=as_of,
            status=status,
            # A failed universe run carries no agent metadata, so it also may
            # not claim to have been AI-ranked.
            selection_method=(
                SelectionMethod.AI_RANKED if succeeded else SelectionMethod.DETERMINISTIC_ONLY
            ),
            universe_source=UniverseSourceRef(
                kind="STATIC", name="test-universe", version="1", symbol_count=len(chosen)
            ),
            deterministic_filter_config=FilterConfigSnapshot(
                max_candidates=50, max_selected_assets=10
            ),
            selected_assets=[
                SelectedAsset(
                    symbol=symbol,
                    rank=index + 1,
                    deterministic_eligibility=UniverseEligibility.ELIGIBLE,
                    reasons=[UniverseSelectionReason.OPTIONS_AVAILABLE],
                    data_quality=quality,
                    confidence=ConfidenceLevel.HIGH,
                    source=CandidateProvenance(
                        provider="IBKR",
                        retrieved_at=as_of,
                        snapshot_ids=[f"snap-{symbol.lower()}"],
                    ),
                )
                for index, symbol in enumerate(chosen)
            ]
            if succeeded
            else [],
            agent_metadata=_universe_agent_metadata(as_of) if succeeded else None,
            versions=SystemVersions(application_version="0.1.0", config_version="test"),
        )
        universe_repo.save(result)
        return result

    return _make


def _universe_agent_metadata(as_of: datetime):
    from trading_system.universe.models import AgentMetadata

    return AgentMetadata(
        model_provider="FAKE",
        model_name="fake-model-1",
        prompt_version="test-1.0.0",
        generated_at=as_of,
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@pytest.fixture
def make_research_config(system_config: SystemConfig) -> Callable[..., SystemConfig]:
    """Build a SystemConfig whose research policy the test controls.

    Everything outside ``research`` stays as shipped, so a test never silently
    depends on a made-up risk limit, freshness window or source tier list.
    """

    def _make(
        *,
        min_horizon: int = 14,
        max_horizon: int = 31,
        agent_enabled: bool = True,
        max_assets_per_run: int = 10,
        max_evidence_items: int = 40,
        max_news_items: int = 25,
        max_events: int = 15,
        max_input_characters: int = 120_000,
        min_evidence_items: int = 1,
        min_distinct_sources: int = 1,
        min_evidence_items_for_high: int = 3,
        min_evidence_items_for_medium: int = 1,
        min_source_tier_for_high: SourceTier = SourceTier.TIER_2,
        max_data_gaps_for_high: int = 3,
        require_research_usable_for_high: bool = True,
        require_contradiction_resolution_for_high: bool = True,
        news_lookback_days: int = 14,
        historical_lookback_days: int = 90,
        min_observations_for_volatility: int = 20,
        broad_index_symbol: str | None = None,
        dedup_enabled: bool = True,
    ) -> SystemConfig:
        research = ResearchConfig(
            config_version="test-research-1",
            horizon=ResearchHorizonConfig(min_days=min_horizon, max_days=max_horizon),
            window=ResearchWindowConfig(
                news_lookback_days=news_lookback_days,
                historical_lookback_days=historical_lookback_days,
                min_observations_for_volatility=min_observations_for_volatility,
            ),
            limits=ResearchLimitsConfig(
                max_assets_per_run=max_assets_per_run,
                max_evidence_items=max_evidence_items,
                max_news_items=max_news_items,
                max_events=max_events,
                max_input_characters=max_input_characters,
            ),
            deduplication=system_config.research.deduplication.model_copy(
                update={"enabled": dedup_enabled}
            ),
            confidence=ResearchConfidenceConfig(
                min_evidence_items_for_high=min_evidence_items_for_high,
                min_evidence_items_for_medium=min_evidence_items_for_medium,
                min_source_tier_for_high=min_source_tier_for_high,
                max_data_gaps_for_high=max_data_gaps_for_high,
                require_research_usable_for_high=require_research_usable_for_high,
                require_contradiction_resolution_for_high=(
                    require_contradiction_resolution_for_high
                ),
            ),
            evidence=ResearchEvidenceConfig(
                min_evidence_items=min_evidence_items,
                min_distinct_sources=min_distinct_sources,
            ),
            market_context=system_config.research.market_context.model_copy(
                update={"broad_index_symbol": broad_index_symbol}
            ),
            agent=ResearchAgentConfig(
                enabled=agent_enabled,
                model_provider="ANTHROPIC",
                model_name="claude-opus-5",
                prompt_version="test-1.0.0",
                timeout_seconds=30.0,
                max_output_tokens=4000,
                effort="low",
            ),
        )
        return system_config.model_copy(update={"research": research})

    return _make


@pytest.fixture
def research_config(make_research_config: Callable[..., SystemConfig]) -> ResearchConfig:
    return make_research_config().research


# ---------------------------------------------------------------------------
# Fake LLM clients
# ---------------------------------------------------------------------------
class FakeLLMClient:
    """Returns a canned response. Records the request it was given.

    Deliberately not a mocking-library double: several tests assert on what the
    agent actually *sent* — that the payload carries no repository handle, no
    strike, no budget — and a real object makes that readable.

    The canned text may contain ``__RUN_ID__``, replaced with the run id the
    service derived, because run ids are content-derived and a fixture cannot
    predict one.
    """

    def __init__(
        self,
        text: str | Callable[[StructuredRequest], str],
        *,
        stop_reason: str | None = "end_turn",
    ) -> None:
        self._text = text
        self._stop_reason = stop_reason
        self.requests: list[StructuredRequest] = []

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity(
            provider="FAKE",
            model_name="fake-model-1",
            prompt_version="test-1.0.0",
            prompt_fingerprint="0" * 32,
            model_version="fake-model-1-20260810",
        )

    def complete(self, request: StructuredRequest) -> LLMResponse:
        self.requests.append(request)
        raw = self._text(request) if callable(self._text) else self._text
        payload = json.loads(request.user_content)
        return LLMResponse(
            text=raw.replace(RUN_ID_PLACEHOLDER, str(payload["run_id"])),
            identity=self.identity,
            generated_at=RESEARCH_NOW + timedelta(seconds=1),
            latency_ms=42.0,
            input_tokens=2400,
            output_tokens=780,
            stop_reason=self._stop_reason,
        )


class UnavailableLLMClient:
    """Always fails, the way an unreachable or unconfigured model does."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or AgentUnavailableError("the model is unreachable")

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity(
            provider="FAKE",
            model_name="unreachable",
            prompt_version="test-1.0.0",
            prompt_fingerprint="0" * 32,
        )

    def complete(self, request: StructuredRequest) -> LLMResponse:
        raise self._error


@pytest.fixture
def outlook_text() -> Callable[..., str]:
    """Build a well-formed agent response against the ids the input carried.

    Returned as *text*, not as a parsed object, so the agent's own parsing and
    validation run in every test that uses it. A fixture that handed back a
    constructed model would skip the code most likely to break.
    """

    def _build(request: StructuredRequest, **overrides: object) -> str:
        payload = json.loads(request.user_content)
        evidence_ids = [item["evidence_id"] for item in payload.get("observations", [])]
        evidence_ids += [item["evidence_id"] for item in payload.get("news", [])]
        evidence_ids += [item["evidence_id"] for item in payload.get("regulatory_events", [])]
        first = evidence_ids[0] if evidence_ids else "missing"

        response: dict[str, object] = {
            "run_id": payload["run_id"],
            "symbol": payload["symbol"],
            "hypothesis": "B",
            "confidence": "MEDIUM",
            "direction": "BULLISH",
            "expected_magnitude": "MODERATE",
            "horizon_days": 21,
            "thesis": "Data-centre demand is accelerating into the next quarter.",
            "expected_behavior": "A gradual drift higher, with volatility around the results.",
            "evidence": [
                {
                    "evidence_id": evidence_id,
                    "claim": "Reported acceleration supports an upward bias.",
                    "direction": "SUPPORTS_UP",
                    "stance": "SUPPORTS",
                    "relevance": "HIGH",
                    "confidence": "MEDIUM",
                }
                for evidence_id in evidence_ids
            ],
            "key_events": [],
            "bullish_catalysts": [
                {"summary": "Accelerating segment revenue", "evidence_ids": [first]}
            ],
            "bearish_catalysts": [],
            "risks": [
                {
                    "category": "EVENT_RISK",
                    "description": "Results could disappoint.",
                    "evidence_ids": [first],
                }
            ],
            "invalidation_conditions": [
                {
                    "condition": "Guidance is cut at the next results.",
                    "observable": "Issuer guidance range in the results release.",
                    "evidence_ids": [first],
                }
            ],
            "missing_information": [],
        }
        response.update(overrides)
        return json.dumps(response)

    return _build


@pytest.fixture
def make_service(
    make_research_config: Callable[..., SystemConfig],
    data_repo: FilesystemDataRepository,
    universe_repo: FilesystemUniverseRepository,
    research_repo: FilesystemResearchRepository,
    research_clock: FixedClock,
) -> Callable[..., ResearchService]:
    """Build a research service wired to temporary stores and a fake model."""

    def _make(
        *,
        llm_client: object | None = None,
        config: SystemConfig | None = None,
        **config_kwargs: object,
    ) -> ResearchService:
        return ResearchService(
            settings=Settings(_env_file=None),
            config=config or make_research_config(**config_kwargs),
            clock=research_clock,
            data_repository=data_repo,
            universe_repository=universe_repo,
            research_repository=research_repo,
            llm_client=llm_client,  # type: ignore[arg-type]
        )

    return _make


@pytest.fixture
def build_input(
    make_research_config: Callable[..., SystemConfig],
    data_repo: FilesystemDataRepository,
) -> Callable[..., object]:
    """Assemble a research input directly, without running the whole service."""

    def _make(symbol: str = "NVDA", *, as_of: datetime = RESEARCH_NOW, **config_kwargs: object):
        from trading_system.research.context import ResearchInputBuilder
        from trading_system.research.sources import SourceTrustPolicy

        config = make_research_config(**config_kwargs)
        builder = ResearchInputBuilder(
            data_repo,
            config=config.research,
            source_policy=SourceTrustPolicy(config.sources),
        )
        return builder.build(symbol, as_of, run_id="research-test-run")

    return _make
