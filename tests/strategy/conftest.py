"""Fixtures for the strategy-stage suites.

Three rules hold across every test here, the same ones the research suites use:

* **No network, no broker, no model.** The service is constructed with a fake
  :class:`LLMClient` or none at all. Nothing here can reach an API, and no test
  needs a credential.
* **No writing into the repository's own ``data/``.** Every store is rooted at
  ``tmp_path``.
* **Research and option data go through the real repositories.** These tests
  persist research runs and option chains the way the earlier stages would, so
  the point-in-time rules and the readiness probe are exercised against the
  thing they will actually see.
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
    DataQualityReport,
    DataSourceMetadata,
    MarketQuote,
    OptionChain,
    OptionContract,
    OptionQuote,
)
from trading_system.data.repository import FilesystemDataRepository, build_snapshot
from trading_system.domain.enums import (
    ConfidenceLevel,
    DataType,
    Direction,
    EvidenceKind,
    ExpectedMagnitude,
    MarketDataOrigin,
    MarketEventType,
    MarketHypothesis,
    OptionRight,
    RelevanceLevel,
    ResearchStatus,
    SecurityType,
    SourceTier,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import (
    Settings,
    StrategyAgentConfig,
    StrategyEligibilityConfig,
    StrategyLimitsConfig,
    StrategyStageConfig,
    SystemConfig,
)
from trading_system.research.models import (
    Catalyst,
    InvalidationCondition,
    MarketResearchReport,
    ReportedEvent,
    ReportedEvidence,
    ResearchAgentMetadata,
    ResearchDataQualitySummary,
    ResearchHorizon,
    ResearchLimitsSnapshot,
    ResearchRunCounts,
    ResearchRunResult,
    ResearchSourcePolicySnapshot,
    ResearchWindowSnapshot,
    SourceProvenance,
)
from trading_system.research.store import FilesystemResearchRepository
from trading_system.strategies.service import StrategyService
from trading_system.strategies.store import (
    FilesystemContractSelectionRepository,
    FilesystemStrategyRepository,
)

#: A weekday inside the market calendar's covered years, during US hours.
STRATEGY_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

#: Replaced by :class:`FakeLLMClient` with the run id the service derived. Run
#: ids are content-derived, so a fixture cannot predict one.
RUN_ID_PLACEHOLDER = "__RUN_ID__"

RESEARCH_RUN_ID = "research-run-test"


@pytest.fixture
def strategy_now() -> datetime:
    return STRATEGY_NOW


@pytest.fixture
def strategy_clock() -> FixedClock:
    return FixedClock(STRATEGY_NOW)


@pytest.fixture
def data_repo(tmp_path: Path, strategy_clock: FixedClock) -> FilesystemDataRepository:
    return FilesystemDataRepository(tmp_path / "data", clock=strategy_clock)


@pytest.fixture
def research_repo(tmp_path: Path) -> FilesystemResearchRepository:
    return FilesystemResearchRepository(tmp_path / "data" / "research")


@pytest.fixture
def strategy_repo(tmp_path: Path) -> FilesystemStrategyRepository:
    return FilesystemStrategyRepository(tmp_path / "data" / "strategy")


@pytest.fixture
def contract_repo(tmp_path: Path) -> FilesystemContractSelectionRepository:
    return FilesystemContractSelectionRepository(tmp_path / "data" / "contracts")


# ---------------------------------------------------------------------------
# Research reports, as the research stage would have written them
# ---------------------------------------------------------------------------
def _provenance(snapshot_id: str = "snap-news") -> SourceProvenance:
    return SourceProvenance(
        provider="FIXTURE_NEWS",
        source_tier=SourceTier.TIER_2,
        retrieved_at=STRATEGY_NOW - timedelta(days=1),
        snapshot_id=snapshot_id,
        source_name="Reuters",
        source_identifier="https://www.reuters.com/technology/nvda-2026-08-09/",
        published_at=STRATEGY_NOW - timedelta(days=1),
    )


@pytest.fixture
def make_report() -> Callable[..., MarketResearchReport]:
    """One research report, with every field a strategy decision reads."""

    def _make(
        symbol: str = "NVDA",
        *,
        hypothesis: MarketHypothesis = MarketHypothesis.B,
        direction: Direction = Direction.BULLISH,
        magnitude: ExpectedMagnitude = ExpectedMagnitude.MODERATE,
        confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
        horizon_days: int = 21,
        status: ResearchStatus = ResearchStatus.SUCCESS,
        evidence_count: int = 2,
        contradicting: int = 0,
        research_usable: bool = True,
        event_days: int | None = None,
        event_relevance: RelevanceLevel = RelevanceLevel.HIGH,
        as_of: datetime = STRATEGY_NOW,
        run_id: str = RESEARCH_RUN_ID,
    ) -> MarketResearchReport:
        succeeded = status is ResearchStatus.SUCCESS
        evidence = [
            ReportedEvidence(
                evidence_id=f"ev-{index}",
                kind=EvidenceKind.NEWS,
                claim=f"evidence {index} supports the thesis",
                fact=f"a reported fact {index}",
                direction="SUPPORTS_UP",
                stance="CONTRADICTS" if index < contradicting else "SUPPORTS",
                relevance=RelevanceLevel.HIGH,
                confidence=ConfidenceLevel.MEDIUM,
                source=_provenance(),
            )
            for index in range(evidence_count)
        ]
        events = []
        if event_days is not None:
            events = [
                ReportedEvent(
                    event_id="evt-1",
                    event_type=MarketEventType.EARNINGS,
                    summary="quarterly results",
                    expected_event_time=as_of + timedelta(days=event_days),
                    announced_at=as_of - timedelta(days=4),
                    confirmed=True,
                    days_until=event_days,
                    within_horizon=0 <= event_days <= 31,
                    expected_relevance=event_relevance,
                    directional_uncertainty=True,
                    source=_provenance("snap-events"),
                )
            ]
        return MarketResearchReport(
            report_id=f"research-{symbol}-001",
            run_id=run_id,
            symbol=symbol.upper(),
            as_of=as_of,
            generated_at=as_of,
            horizon=ResearchHorizon(min_days=14, max_days=31),
            status=status,
            hypothesis=hypothesis if succeeded else None,
            confidence=confidence if succeeded else None,
            direction=direction if succeeded else None,
            expected_magnitude=magnitude if succeeded else None,
            horizon_days=horizon_days if succeeded else None,
            thesis="demand is accelerating into the next quarter" if succeeded else None,
            expected_behavior="a gradual drift higher" if succeeded else None,
            explanation=(
                "the situation fits none of A-D"
                if succeeded and hypothesis is MarketHypothesis.E
                else None
            ),
            evidence=evidence if succeeded else [],
            key_events=events if succeeded else [],
            bullish_catalysts=(
                [Catalyst(summary="accelerating revenue", evidence_ids=["ev-0"])]
                if succeeded and evidence
                else []
            ),
            invalidation_conditions=(
                [
                    InvalidationCondition(
                        condition="guidance is cut at the next results",
                        observable="the issuer's guidance range",
                        evidence_ids=["ev-0"] if evidence else [],
                    )
                ]
                if succeeded
                else []
            ),
            contradiction_resolution="the supporting side dominates" if contradicting else None,
            data_quality=ResearchDataQualitySummary(
                research_usable=research_usable,
                records_considered=evidence_count,
                records_research_usable=evidence_count if research_usable else 0,
            ),
            window=ResearchWindowSnapshot(
                news_lookback_days=14,
                event_lookahead_days=45,
                event_lookback_days=14,
                historical_lookback_days=90,
                fundamentals_lookback_days=400,
                regulatory_lookback_days=120,
                volatility_annualization_days=252,
            ),
            limits=ResearchLimitsSnapshot(
                max_evidence_items=40,
                max_news_items=25,
                max_events=15,
                max_regulatory_items=10,
                max_fundamental_periods=4,
            ),
            source_policy=ResearchSourcePolicySnapshot(config_version="test", tier_2_count=5),
            universe_run_id="universe-run-test",
            input_snapshot_ids=["snap-news"],
            agent_metadata=ResearchAgentMetadata(
                model_provider="FAKE",
                model_name="fake-model-1",
                prompt_version="test-1.0.0",
                generated_at=as_of,
            ),
            versions=SystemVersions(application_version="0.1.0", config_version="test"),
        )

    return _make


@pytest.fixture
def store_research(
    research_repo: FilesystemResearchRepository,
) -> Callable[..., ResearchRunResult]:
    """Persist a research run for the strategy stage to consume."""

    def _make(
        reports: Sequence[MarketResearchReport],
        *,
        as_of: datetime = STRATEGY_NOW,
        run_id: str = RESEARCH_RUN_ID,
        status: ResearchStatus = ResearchStatus.SUCCESS,
    ) -> ResearchRunResult:
        result = ResearchRunResult(
            run_id=run_id,
            as_of=as_of,
            generated_at=as_of,
            status=status,
            horizon=ResearchHorizon(min_days=14, max_days=31),
            universe_run_id="universe-run-test",
            universe_snapshot_id="universe-snapshot-test",
            reports=list(reports),
            counts=ResearchRunCounts(
                universe_assets=len(reports),
                researched=len(reports),
                succeeded=sum(1 for report in reports if report.succeeded),
            ),
            versions=SystemVersions(application_version="0.1.0", config_version="test"),
        )
        research_repo.save(result)
        return result

    return _make


# ---------------------------------------------------------------------------
# Option data, for the readiness probe
# ---------------------------------------------------------------------------
def _metadata(retrieved_at: datetime, identifier: str) -> DataSourceMetadata:
    return DataSourceMetadata(
        provider="IBKR",
        source_name="IBKR",
        source_tier=SourceTier.TIER_1,
        origin=MarketDataOrigin.BROKER_DELAYED,
        retrieved_at=retrieved_at,
        source_timestamp=retrieved_at,
        observed_at=retrieved_at,
        source_identifier=identifier,
    )


@pytest.fixture
def store_chain(data_repo: FilesystemDataRepository) -> Callable[..., OptionChain]:
    """A chain snapshot: what the readiness probe looks for."""

    def _make(symbol: str = "NVDA", *, as_of: datetime = STRATEGY_NOW) -> OptionChain:
        chain = OptionChain(
            as_of=as_of,
            source=_metadata(as_of, f"ibkr:chain:{symbol}"),
            underlying=symbol.upper(),
            exchange="SMART",
            trading_class=symbol.upper(),
            multiplier=100,
            expirations=[date(2026, 8, 28), date(2026, 9, 4)],
            strikes=[Decimal("170"), Decimal("180"), Decimal("190")],
            rights=[OptionRight.CALL, OptionRight.PUT],
        )
        snapshot = build_snapshot(
            data_type=DataType.OPTION_CHAIN,
            key=symbol.upper(),
            records=[chain],
            provider="IBKR",
            source_tier=SourceTier.TIER_1,
            origin=MarketDataOrigin.BROKER_DELAYED,
            as_of=as_of,
            retrieved_at=as_of,
            quality=DataQualityReport(evaluated_at=as_of),
        )
        data_repo.save_snapshot(snapshot)
        return chain

    return _make


@pytest.fixture
def store_underlying_quote(data_repo: FilesystemDataRepository) -> Callable[..., MarketQuote]:
    def _make(symbol: str = "NVDA", *, as_of: datetime = STRATEGY_NOW) -> MarketQuote:
        quote = MarketQuote(
            as_of=as_of,
            source=_metadata(as_of, f"ibkr:{symbol}"),
            symbol=symbol.upper(),
            security_type=SecurityType.STOCK,
            currency="USD",
            last=Decimal("180.00"),
            close=Decimal("179.10"),
            volume=Decimal("240000000"),
            quality=DataQualityReport(evaluated_at=as_of),
        )
        snapshot = build_snapshot(
            data_type=DataType.MARKET_QUOTE,
            key=symbol.upper(),
            records=[quote],
            provider="IBKR",
            source_tier=SourceTier.TIER_1,
            origin=MarketDataOrigin.BROKER_DELAYED,
            as_of=as_of,
            retrieved_at=as_of,
            quality=DataQualityReport(evaluated_at=as_of),
        )
        data_repo.save_snapshot(snapshot)
        return quote

    return _make


@pytest.fixture
def store_option_quotes(data_repo: FilesystemDataRepository) -> Callable[..., list[OptionQuote]]:
    """Per-contract quotes, so the deterministic selector has something to pick.

    Only used by the tests that drive the whole pipeline through to a contract;
    the strategy stage itself never sees a quote.
    """

    def _make(symbol: str = "NVDA", *, as_of: datetime = STRATEGY_NOW) -> list[OptionQuote]:
        quotes = [
            OptionQuote(
                as_of=as_of,
                source=_metadata(as_of, f"ibkr:option:{symbol}:{expiration}:{strike}:{right}"),
                contract=OptionContract(
                    underlying=symbol.upper(),
                    symbol=symbol.upper(),
                    expiration=expiration,
                    strike=strike,
                    right=right,
                    contract_id=abs(hash((symbol, expiration, str(strike), right.value))) % 10**8,
                    exchange="SMART",
                    currency="USD",
                    multiplier=100,
                    trading_class=symbol.upper(),
                ),
                bid=Decimal("5.95"),
                ask=Decimal("6.05"),
                last=Decimal("6.00"),
                volume=Decimal("1200"),
                open_interest=Decimal("8400"),
                implied_volatility=Decimal("0.35"),
                delta=Decimal("0.60") if right is OptionRight.CALL else Decimal("-0.60"),
                quality=DataQualityReport(evaluated_at=as_of),
            )
            for expiration in (date(2026, 8, 28), date(2026, 9, 4))
            for strike in (Decimal("170"), Decimal("180"), Decimal("190"))
            for right in (OptionRight.CALL, OptionRight.PUT)
        ]
        snapshot = build_snapshot(
            data_type=DataType.OPTION_QUOTE,
            key=symbol.upper(),
            records=quotes,
            provider="IBKR",
            source_tier=SourceTier.TIER_1,
            origin=MarketDataOrigin.BROKER_DELAYED,
            as_of=as_of,
            retrieved_at=as_of,
            quality=DataQualityReport(evaluated_at=as_of),
        )
        data_repo.save_snapshot(snapshot)
        return quotes

    return _make


@pytest.fixture
def researchable(
    store_chain: Callable[..., OptionChain],
    store_underlying_quote: Callable[..., MarketQuote],
) -> Callable[..., str]:
    """A symbol with the option data the readiness gate requires."""

    def _make(symbol: str = "NVDA") -> str:
        store_chain(symbol)
        store_underlying_quote(symbol)
        return symbol.upper()

    return _make


@pytest.fixture
def tradeable(
    researchable: Callable[..., str],
    store_option_quotes: Callable[..., list[OptionQuote]],
) -> Callable[..., str]:
    """A symbol a contract can actually be selected for."""

    def _make(symbol: str = "NVDA") -> str:
        researchable(symbol)
        store_option_quotes(symbol)
        return symbol.upper()

    return _make


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@pytest.fixture
def make_strategy_config(system_config: SystemConfig) -> Callable[..., SystemConfig]:
    """A SystemConfig whose strategy-stage policy the test controls.

    Everything outside ``strategy`` stays as shipped, so no test silently
    depends on a made-up risk limit or strategy specification.
    """

    def _make(
        *,
        agent_enabled: bool = True,
        min_confidence: ConfidenceLevel = ConfidenceLevel.LOW,
        require_research_usable: bool = True,
        require_option_chain: bool = True,
        require_horizon_overlap: bool = True,
        min_evidence_items: int = 1,
        max_symbols_per_run: int = 10,
        max_input_characters: int = 60_000,
    ) -> SystemConfig:
        stage = StrategyStageConfig(
            config_version="test-strategy-1",
            eligibility=StrategyEligibilityConfig(
                min_confidence=min_confidence,
                require_research_usable=require_research_usable,
                require_option_chain=require_option_chain,
                require_horizon_overlap=require_horizon_overlap,
                min_evidence_items=min_evidence_items,
            ),
            limits=StrategyLimitsConfig(
                max_symbols_per_run=max_symbols_per_run,
                max_input_characters=max_input_characters,
            ),
            agent=StrategyAgentConfig(
                enabled=agent_enabled,
                model_provider="ANTHROPIC",
                model_name="claude-opus-5",
                prompt_version="test-1.0.0",
                timeout_seconds=30.0,
                max_output_tokens=2000,
                effort="low",
            ),
        )
        return system_config.model_copy(update={"strategy": stage})

    return _make


@pytest.fixture
def strategy_stage_config(make_strategy_config: Callable[..., SystemConfig]) -> StrategyStageConfig:
    return make_strategy_config().strategy


# ---------------------------------------------------------------------------
# Fake model clients
# ---------------------------------------------------------------------------
class FakeLLMClient:
    """Returns a canned decision. Records the request it was given.

    Deliberately not a mocking-library double: several tests assert on what the
    agent actually *sent* — that the payload carries no chain, no strike and no
    budget — and a real object makes that readable.
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
            generated_at=STRATEGY_NOW + timedelta(seconds=1),
            latency_ms=21.0,
            input_tokens=1200,
            output_tokens=180,
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
def decision_text() -> Callable[..., str]:
    """Build a well-formed decision against the strategies the input offered.

    Returned as *text*, not as a parsed object, so the agent's own parsing and
    validation run in every test that uses it.
    """

    def _build(request: StructuredRequest, **overrides: object) -> str:
        payload = json.loads(request.user_content)
        offered = [option["strategy_id"] for option in payload["eligible_strategies"]]
        response: dict[str, object] = {
            "run_id": payload["run_id"],
            "symbol": payload["symbol"],
            "action": "BUY",
            "selected_strategy": offered[0] if offered else None,
            "confidence": "MEDIUM",
            "reasons": ["HYPOTHESIS_MATCH"],
            "rationale": (
                "the research hypothesis matches what this strategy expresses over the "
                "stated horizon"
            ),
        }
        response.update(overrides)
        return json.dumps(response)

    return _build


@pytest.fixture
def no_trade_text() -> Callable[..., str]:
    """A well-formed NO_TRADE decision."""

    def _build(request: StructuredRequest, **overrides: object) -> str:
        payload = json.loads(request.user_content)
        response: dict[str, object] = {
            "run_id": payload["run_id"],
            "symbol": payload["symbol"],
            "action": "NO_TRADE",
            "selected_strategy": None,
            "confidence": "LOW",
            "reasons": ["RESEARCH_INCOMPATIBLE"],
            "rationale": "nothing offered expresses this outlook well enough to act on",
        }
        response.update(overrides)
        return json.dumps(response)

    return _build


@pytest.fixture
def make_service(
    make_strategy_config: Callable[..., SystemConfig],
    data_repo: FilesystemDataRepository,
    research_repo: FilesystemResearchRepository,
    strategy_repo: FilesystemStrategyRepository,
    strategy_clock: FixedClock,
) -> Callable[..., StrategyService]:
    """A strategy service wired to temporary stores and a fake model."""

    def _make(
        *,
        llm_client: object | None = None,
        config: SystemConfig | None = None,
        **config_kwargs: object,
    ) -> StrategyService:
        return StrategyService(
            settings=Settings(_env_file=None),
            config=config or make_strategy_config(**config_kwargs),
            clock=strategy_clock,
            data_repository=data_repo,
            research_repository=research_repo,
            strategy_repository=strategy_repo,
            llm_client=llm_client,  # type: ignore[arg-type]
        )

    return _make
