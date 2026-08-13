"""Shared test fixtures and the test-suite safety guard.

Two things happen here that matter more than convenience:

* **Hermetic safety settings.** Every test runs with ``TRADING_MODE=PAPER`` and
  ``ALLOW_LIVE_TESTS=false`` forced into the environment. Environment variables
  outrank ``.env``, so a developer's local ``.env`` cannot drag the suite into
  a mode it was not written for.
* **Live tests are skipped by default.** Anything marked ``live``, ``ibkr`` or
  ``llm`` is skipped unless ``ALLOW_LIVE_TESTS=true`` is set deliberately
  (specification section 35).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_system.domain.enums import (
    Direction,
    ExitAction,
    ExitReason,
    ExpectedMagnitude,
    LegAction,
    MarketHypothesis,
    OptionRight,
    OrderStatus,
    OrderType,
    PositionState,
    RiskOutcome,
    RiskReasonCode,
    SourceTier,
    StrategyAction,
    StrategyType,
    TradingMode,
)
from trading_system.domain.models import (
    AllocationDecision,
    AllocationEntry,
    ContractSelection,
    ExecutionResult,
    ExitDecision,
    Fill,
    OptionLeg,
    OrderIntent,
    PositionSnapshot,
    PurchaseCard,
    ResearchReport,
    RiskDecision,
    SourceReference,
    StrategyDecision,
    SystemVersions,
    TradeSnapshot,
    UniverseCandidate,
    UniverseSelection,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import (
    SystemConfig,
    default_config_dir,
    load_config,
    project_root,
)

#: Fixed instant used across the suite so nothing depends on wall-clock time.
FIXED_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _force_safe_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the safety-critical environment for every test.

    Explicit values rather than deletion: environment variables take priority
    over ``.env`` in pydantic-settings, so this neutralises whatever the
    developer has configured locally.
    """
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("ALLOW_LIVE_TESTS", "false")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "false")
    monkeypatch.setenv("LIVE_READINESS_CHECKLIST_SIGNED_OFF", "false")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip broker/LLM-touching tests unless explicitly unlocked.

    ``paper_execution`` is gated twice over, and deliberately so: it is the only
    marker in the suite that can place a real order, and a developer who
    unlocked the gateway to run a read-only diagnostic must not thereby have
    authorised one (brief section 45). It needs its own variable, and it is
    checked *before* the general unlock so that ``ALLOW_LIVE_TESTS=true`` alone
    never reaches it.
    """
    import os

    execution_unlocked = (
        os.environ.get("ALLOW_LIVE_TESTS", "false").lower() == "true"
        and os.environ.get("RUN_PAPER_EXECUTION_TESTS", "false").lower() == "true"
    )
    skip_execution = pytest.mark.skip(
        reason=(
            "SUBMITS A REAL PAPER ORDER: requires both ALLOW_LIVE_TESTS=true and "
            "RUN_PAPER_EXECUTION_TESTS=true"
        )
    )
    for item in items:
        if "paper_execution" in item.keywords and not execution_unlocked:
            item.add_marker(skip_execution)

    if os.environ.get("ALLOW_LIVE_TESTS", "false").lower() == "true":
        return

    skip = pytest.mark.skip(
        reason="requires ALLOW_LIVE_TESTS=true; may reach a real broker or paid API"
    )
    for item in items:
        if {"live", "ibkr", "paper", "llm", "paper_execution"} & set(item.keywords):
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Paths, configuration, schemas
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def repo_root() -> Path:
    return project_root()


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return default_config_dir()


@pytest.fixture(scope="session")
def system_config(config_dir: Path) -> SystemConfig:
    return load_config(config_dir)


@pytest.fixture(scope="session")
def schemas_dir(repo_root: Path) -> Path:
    return repo_root / "schemas"


@pytest.fixture(scope="session")
def load_schema(schemas_dir: Path) -> Callable[[str], dict[str, Any]]:
    """Return a loader for a named workflow-boundary schema."""

    def _load(name: str) -> dict[str, Any]:
        path = schemas_dir / f"{name}.json"
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    return _load


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


# ---------------------------------------------------------------------------
# Sample artifacts
#
# A single consistent chain: every id below is referenced by the next stage, so
# the contract tests can assert that a producer's output is actually consumable
# by its consumer rather than merely well-formed in isolation.
# ---------------------------------------------------------------------------
@pytest.fixture
def versions() -> SystemVersions:
    return SystemVersions(
        application_version="0.1.0",
        config_version="2026.08.10-1",
        strategy_spec_version="1.0.0",
        agent_version="test",
        model_id="test-model",
    )


@pytest.fixture
def source_reference() -> SourceReference:
    return SourceReference(
        source_name="SEC EDGAR",
        source_url_or_identifier="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
        source_tier=SourceTier.TIER_1,
        published_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        retrieved_at=FIXED_NOW,
        relevance=0.9,
    )


@pytest.fixture
def universe_selection(versions: SystemVersions) -> UniverseSelection:
    return UniverseSelection(
        universe_id="universe-001",
        as_of=FIXED_NOW,
        candidates=[
            UniverseCandidate(ticker="NVDA", rank=1, selection_score=94.0),
            UniverseCandidate(ticker="AAPL", rank=2, selection_score=88.0),
        ],
        versions=versions,
    )


@pytest.fixture
def universe_run_result(versions: SystemVersions):
    """A complete Milestone 4 run record, feeding the research stage.

    Built by hand rather than by running the service: a contract test asserts
    that the *artifact shape* crosses the boundary correctly, and should fail
    for a schema change rather than for an unrelated pipeline change.
    """
    from trading_system.domain.enums import (
        ConfidenceLevel,
        DataQuality,
        Optionability,
        SelectionMethod,
        UniverseEligibility,
        UniverseRejectionReason,
        UniverseSelectionReason,
        UniverseSelectionStatus,
    )
    from trading_system.universe.models import (
        AgentMetadata,
        CandidateProvenance,
        DataQualitySummary,
        FilterConfigSnapshot,
        RejectedAsset,
        SelectedAsset,
        UniverseRunCounts,
        UniverseSelectionResult,
        UniverseSourceRef,
    )

    quality = DataQualitySummary(research_usable=True, classification=DataQuality.OK)
    provenance = CandidateProvenance(
        provider="IBKR", retrieved_at=FIXED_NOW, snapshot_ids=["snap-nvda"]
    )
    return UniverseSelectionResult(
        snapshot_id="universe-snapshot-001",
        run_id="universe-run-001",
        as_of=FIXED_NOW,
        generated_at=FIXED_NOW,
        status=UniverseSelectionStatus.SUCCESS,
        selection_method=SelectionMethod.AI_RANKED,
        universe_source=UniverseSourceRef(
            kind="STATIC", name="liquid-us-optionable-core", version="1", symbol_count=2
        ),
        deterministic_filter_config=FilterConfigSnapshot(
            allowed_security_types=["STOCK"],
            allowed_currencies=["USD"],
            min_price=Decimal("5.00"),
            min_average_daily_volume=1_000_000,
            max_data_age_seconds=86_400,
            optionability_policy="REQUIRED",
            max_candidates=50,
            max_selected_assets=10,
        ),
        selected_assets=[
            SelectedAsset(
                symbol="NVDA",
                rank=1,
                deterministic_eligibility=UniverseEligibility.ELIGIBLE,
                reasons=[
                    UniverseSelectionReason.OPTIONS_AVAILABLE,
                    UniverseSelectionReason.HIGH_UNDERLYING_LIQUIDITY,
                ],
                data_quality=quality,
                confidence=ConfidenceLevel.HIGH,
                selection_score=94.0,
                rationale="Established option chain and deep underlying volume.",
                optionability=Optionability.TRUE,
                reference_price=Decimal("180.25"),
                underlying_volume=Decimal("240000000"),
                source=provenance,
            ),
            SelectedAsset(
                symbol="AAPL",
                rank=2,
                deterministic_eligibility=UniverseEligibility.ELIGIBLE,
                reasons=[UniverseSelectionReason.OPTIONS_AVAILABLE],
                data_quality=quality,
                confidence=ConfidenceLevel.MEDIUM,
                selection_score=88.0,
                optionability=Optionability.TRUE,
                reference_price=Decimal("225.10"),
                underlying_volume=Decimal("55000000"),
                source=CandidateProvenance(
                    provider="IBKR", retrieved_at=FIXED_NOW, snapshot_ids=["snap-aapl"]
                ),
            ),
        ],
        rejected_assets=[
            RejectedAsset(
                symbol="IWM",
                deterministic_eligibility=UniverseEligibility.ELIGIBLE,
                reason=UniverseRejectionReason.NOT_SELECTED_BY_RANKING,
                detail="ranked but below the size limit",
                optionability=Optionability.TRUE,
            )
        ],
        agent_metadata=AgentMetadata(
            model_provider="ANTHROPIC",
            model_name="claude-opus-5",
            prompt_version="1.0.0",
            agent_version="1.0.0+abcdef123456",
            generated_at=FIXED_NOW,
        ),
        input_snapshot_ids=["snap-aapl", "snap-nvda"],
        counts=UniverseRunCounts(
            candidates=3, deterministic_pass=3, ai_input=3, ai_selected=2, final=2
        ),
        versions=versions,
    )


@pytest.fixture
def research_report(versions: SystemVersions, source_reference: SourceReference) -> ResearchReport:
    return ResearchReport(
        report_id="research-001",
        ticker="NVDA",
        as_of=FIXED_NOW,
        hypothesis=MarketHypothesis.B,
        direction=Direction.BULLISH,
        expected_magnitude=ExpectedMagnitude.MODERATE,
        confidence=0.72,
        expected_horizon_days=21,
        catalysts=["Q2 earnings on 2026-08-27"],
        invalidation_conditions=[
            "Guidance withdrawn or cut",
            "Close below the 200-day moving average",
        ],
        evidence=["Filed 10-Q shows accelerating data-centre revenue"],
        sources=[source_reference],
        versions=versions,
    )


@pytest.fixture
def market_research_input(versions: SystemVersions):
    """A Milestone 5 research input, as the point-in-time assembler produces one.

    Built by hand rather than by running the builder: the contract tests assert
    that the *artifact shape* crosses the boundary correctly, and should fail
    for a schema change rather than for an unrelated pipeline change.
    """
    from trading_system.domain.enums import (
        EvidenceKind,
        MarketDataOrigin,
        MarketEventType,
        ResearchDataGap,
    )
    from trading_system.research.models import (
        EventItem,
        EvidenceItem,
        MarketSnapshot,
        OptionMarketContext,
        OptionTermPoint,
        ResearchDataQualitySummary,
        ResearchHorizon,
        ResearchInput,
        ResearchLimitsSnapshot,
        ResearchSourcePolicySnapshot,
        ResearchWindowSnapshot,
        SourceProvenance,
    )

    news_source = SourceProvenance(
        provider="FIXTURE_NEWS",
        source_tier=SourceTier.TIER_2,
        retrieved_at=FIXED_NOW,
        snapshot_id="snap-news-nvda",
        source_name="Reuters",
        source_identifier="https://www.reuters.com/technology/nvda-2026-08-09/",
        published_at=datetime(2026, 8, 9, 13, 5, tzinfo=UTC),
        origin=MarketDataOrigin.HISTORICAL,
        provider_relevance=0.9,
    )
    market_source = SourceProvenance(
        provider="IBKR",
        source_tier=SourceTier.TIER_1,
        retrieved_at=FIXED_NOW,
        snapshot_id="snap-quote-nvda",
        source_name="IBKR",
        source_identifier="ibkr:NVDA",
        origin=MarketDataOrigin.BROKER_DELAYED,
    )

    return ResearchInput(
        run_id="research-run-001",
        symbol="NVDA",
        as_of=FIXED_NOW,
        horizon=ResearchHorizon(min_days=14, max_days=31),
        universe_run_id="universe-run-001",
        universe_snapshot_id="universe-snapshot-001",
        data_snapshot_ids=["snap-chain-nvda", "snap-news-nvda", "snap-quote-nvda"],
        market_snapshot=MarketSnapshot(
            symbol="NVDA",
            as_of=FIXED_NOW,
            origin=MarketDataOrigin.BROKER_DELAYED,
            source=market_source,
            last=Decimal("180.25"),
            close=Decimal("179.10"),
            volume=Decimal("240000000"),
            currency="USD",
            age_seconds=0.0,
        ),
        option_context=OptionMarketContext(
            underlying="NVDA",
            as_of=FIXED_NOW,
            source=SourceProvenance(
                provider="IBKR",
                source_tier=SourceTier.TIER_1,
                retrieved_at=FIXED_NOW,
                snapshot_id="snap-chain-nvda",
                source_name="IBKR",
            ),
            expiration_count=35,
            strike_count=491,
            nearest_expiration_days=11,
            atm_implied_volatility=Decimal("0.42"),
            term_structure=[
                OptionTermPoint(
                    days_to_expiration=11,
                    atm_implied_volatility=Decimal("0.42"),
                    contract_count=2,
                )
            ],
        ),
        news=[
            EvidenceItem(
                evidence_id="ev-news-1",
                kind=EvidenceKind.NEWS,
                summary="Nvidia data-centre revenue accelerates, company says",
                source=news_source,
                occurred_at=datetime(2026, 8, 9, 13, 5, tzinfo=UTC),
                duplicate_count=2,
                duplicate_source_names=["Barrons", "MarketWatch"],
            )
        ],
        observations=[
            EvidenceItem(
                evidence_id="ev-market-1",
                kind=EvidenceKind.MARKET_DATA,
                summary="NVDA quote: last=180.25 close=179.10 volume=240000000",
                source=market_source,
                occurred_at=FIXED_NOW,
            )
        ],
        events=[
            EventItem(
                event_id="evt-nvda-q2",
                event_type=MarketEventType.EARNINGS,
                summary="Q2 FY2027 results",
                expected_event_time=datetime(2026, 8, 27, 20, 20, tzinfo=UTC),
                source=SourceProvenance(
                    provider="FIXTURE_EVENTS",
                    source_tier=SourceTier.TIER_1,
                    retrieved_at=FIXED_NOW,
                    snapshot_id="snap-events-nvda",
                    source_name="Company investor relations",
                ),
                announced_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
                confirmed=True,
                days_until=17,
                within_horizon=True,
            )
        ],
        data_quality_summary=ResearchDataQualitySummary(
            research_usable=True,
            records_considered=2,
            records_research_usable=2,
            gaps=[ResearchDataGap.FUNDAMENTALS_UNAVAILABLE],
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
        source_policy=ResearchSourcePolicySnapshot(
            config_version="2026.08.10-1",
            min_sources_per_report=2,
            tier_1_count=7,
            tier_2_count=5,
        ),
    )


@pytest.fixture
def market_research_agent_output():
    """A well-formed agent response for the input fixture above."""
    from trading_system.research.models import (
        AgentEvidenceAssessment,
        Catalyst,
        InvalidationCondition,
        ResearchAgentOutput,
        RiskAssessment,
    )

    return ResearchAgentOutput(
        run_id="research-run-001",
        symbol="NVDA",
        hypothesis=MarketHypothesis.B,
        confidence="MEDIUM",
        direction=Direction.BULLISH,
        expected_magnitude=ExpectedMagnitude.MODERATE,
        horizon_days=21,
        thesis="Data-centre demand is accelerating into the next set of results.",
        expected_behavior="A gradual drift higher, with volatility around the results date.",
        evidence=[
            AgentEvidenceAssessment(
                evidence_id="ev-news-1",
                claim="Reported acceleration supports an upward bias.",
                direction="SUPPORTS_UP",
                stance="SUPPORTS",
                relevance="HIGH",
                confidence="MEDIUM",
            )
        ],
        bullish_catalysts=[
            Catalyst(summary="Accelerating segment revenue", evidence_ids=["ev-news-1"])
        ],
        risks=[
            RiskAssessment(
                category="EVENT_RISK",
                description="The results could disappoint.",
                evidence_ids=["ev-news-1"],
            )
        ],
        invalidation_conditions=[
            InvalidationCondition(
                condition="Guidance is cut at the 27 August results.",
                observable="Issuer guidance range in the results release.",
                evidence_ids=["ev-news-1"],
            )
        ],
    )


@pytest.fixture
def market_research_report(versions: SystemVersions, market_research_input):
    """A complete Milestone 5 report, feeding the strategy stage."""
    from trading_system.domain.enums import EvidenceKind, ResearchStatus
    from trading_system.research.models import (
        Catalyst,
        InvalidationCondition,
        MarketResearchReport,
        ReportedEvent,
        ReportedEvidence,
        ResearchAgentMetadata,
        RiskAssessment,
    )

    news = market_research_input.news[0]
    event = market_research_input.events[0]
    return MarketResearchReport(
        report_id="research-001",
        run_id="research-run-001",
        symbol="NVDA",
        as_of=FIXED_NOW,
        generated_at=FIXED_NOW,
        horizon=market_research_input.horizon,
        status=ResearchStatus.SUCCESS,
        hypothesis=MarketHypothesis.B,
        confidence="MEDIUM",
        direction=Direction.BULLISH,
        expected_magnitude=ExpectedMagnitude.MODERATE,
        horizon_days=21,
        thesis="Data-centre demand is accelerating into the next set of results.",
        expected_behavior="A gradual drift higher, with volatility around the results date.",
        evidence=[
            ReportedEvidence(
                evidence_id=news.evidence_id,
                kind=EvidenceKind.NEWS,
                claim="Reported acceleration supports an upward bias.",
                fact=news.summary,
                direction="SUPPORTS_UP",
                stance="SUPPORTS",
                relevance="HIGH",
                confidence="MEDIUM",
                source=news.source,
                occurred_at=news.occurred_at,
                duplicate_count=news.duplicate_count,
                duplicate_source_names=list(news.duplicate_source_names),
            )
        ],
        key_events=[
            ReportedEvent(
                event_id=event.event_id,
                event_type=event.event_type,
                summary=event.summary,
                expected_event_time=event.expected_event_time,
                announced_at=event.announced_at,
                confirmed=event.confirmed,
                days_until=event.days_until,
                within_horizon=event.within_horizon,
                expected_relevance="HIGH",
                directional_uncertainty=True,
                source=event.source,
            )
        ],
        bullish_catalysts=[
            Catalyst(summary="Accelerating segment revenue", evidence_ids=[news.evidence_id])
        ],
        risks=[
            RiskAssessment(
                category="EVENT_RISK",
                description="The results could disappoint.",
                evidence_ids=[news.evidence_id],
            )
        ],
        invalidation_conditions=[
            InvalidationCondition(
                condition="Guidance is cut at the 27 August results.",
                observable="Issuer guidance range in the results release.",
                evidence_ids=[news.evidence_id],
            )
        ],
        data_quality=market_research_input.data_quality_summary,
        window=market_research_input.window,
        limits=market_research_input.limits,
        source_policy=market_research_input.source_policy,
        universe_run_id="universe-run-001",
        universe_snapshot_id="universe-snapshot-001",
        input_snapshot_ids=list(market_research_input.data_snapshot_ids),
        agent_metadata=ResearchAgentMetadata(
            model_provider="ANTHROPIC",
            model_name="claude-opus-5",
            prompt_version="1.0.0",
            prompt_fingerprint="a" * 32,
            agent_version="1.0.0+aaaaaaaaaaaa",
            generated_at=FIXED_NOW,
        ),
        versions=versions,
    )


@pytest.fixture
def market_research_run(versions: SystemVersions, market_research_report):
    """The immutable record of one research run."""
    from trading_system.domain.enums import ResearchStatus
    from trading_system.research.models import ResearchRunCounts, ResearchRunResult

    return ResearchRunResult(
        run_id="research-run-001",
        as_of=FIXED_NOW,
        generated_at=FIXED_NOW,
        status=ResearchStatus.SUCCESS,
        horizon=market_research_report.horizon,
        universe_run_id="universe-run-001",
        universe_snapshot_id="universe-snapshot-001",
        reports=[market_research_report],
        counts=ResearchRunCounts(universe_assets=2, researched=1, succeeded=1),
        versions=versions,
    )


@pytest.fixture
def strategy_decision(versions: SystemVersions) -> StrategyDecision:
    return StrategyDecision(
        decision_id="strategy-001",
        ticker="NVDA",
        research_report_id="research-001",
        as_of=FIXED_NOW,
        action=StrategyAction.BUY,
        strategy_type=StrategyType.LONG_CALL,
        rationale="Hypothesis B (bullish) with a 21-day horizon matches a long call.",
        versions=versions,
    )


@pytest.fixture
def strategy_decision_record(versions: SystemVersions):
    """A complete Milestone 6 decision record, feeding the contract stage.

    Built by hand rather than by running the service: a contract test asserts
    that the *artifact shape* crosses the boundary correctly, and should fail
    for a schema change rather than for an unrelated pipeline change.
    """
    from trading_system.domain.enums import (
        ConfidenceLevel,
        DecisionMethod,
        StrategySelectionReason,
        StrategySelectionStatus,
    )
    from trading_system.strategies.models import (
        DataReadiness,
        StrategyAgentMetadata,
        StrategyDecisionRecord,
    )

    return StrategyDecisionRecord(
        decision_id="strategy-NVDA-001",
        run_id="strategy-run-001",
        symbol="NVDA",
        as_of=FIXED_NOW,
        generated_at=FIXED_NOW,
        status=StrategySelectionStatus.SUCCESS,
        action=StrategyAction.BUY,
        selected_strategy=StrategyType.LONG_CALL,
        strategy_version="1.0.0",
        decision_method=DecisionMethod.AI_SELECTED,
        confidence=ConfidenceLevel.MEDIUM,
        reasons=[
            StrategySelectionReason.HYPOTHESIS_MATCH,
            StrategySelectionReason.DIRECTIONAL_VIEW_SUPPORTED,
        ],
        rationale="Hypothesis B over a 21-day horizon is what a long call expresses.",
        hypothesis=MarketHypothesis.B,
        research_confidence=ConfidenceLevel.MEDIUM,
        research_horizon_days=21,
        research_report_id="research-001",
        research_run_id="research-run-001",
        universe_run_id="universe-run-001",
        eligible_strategies=[StrategyType.LONG_CALL],
        data_readiness=DataReadiness(
            option_chain_available=True,
            option_quotes_available=True,
            underlying_quote_available=True,
            expirations_visible=4,
            strikes_visible=7,
            chain_snapshot_id="snap-chain-nvda",
            quote_snapshot_id="snap-option-quotes-nvda",
            underlying_snapshot_id="snap-quote-nvda",
        ),
        input_snapshot_ids=["snap-chain-nvda", "snap-option-quotes-nvda", "snap-quote-nvda"],
        agent_metadata=StrategyAgentMetadata(
            model_provider="ANTHROPIC",
            model_name="claude-opus-5",
            prompt_version="1.0.0",
            prompt_fingerprint="b" * 32,
            agent_version="1.0.0+bbbbbbbbbbbb",
            generated_at=FIXED_NOW,
        ),
        versions=versions,
    )


@pytest.fixture
def contract_selection_result(versions: SystemVersions, strategy_decision_record):
    """A complete Milestone 6 contract selection, feeding the purchase card."""
    from trading_system.domain.enums import (
        ContractSelectionStatus,
        ExpirationSelectionPolicy,
        StrikeSelectionPolicy,
    )
    from trading_system.strategies.models import (
        ContractCostEstimate,
        ContractSelectionResult,
        RejectedContract,
        SelectedLeg,
    )

    return ContractSelectionResult(
        selection_id="contract-NVDA-001",
        run_id="contract-run-001",
        symbol="NVDA",
        as_of=FIXED_NOW,
        generated_at=FIXED_NOW,
        selection_status=ContractSelectionStatus.SUCCESS,
        strategy=StrategyType.LONG_CALL,
        strategy_version="1.0.0",
        strategy_run_id="strategy-run-001",
        strategy_decision_id=strategy_decision_record.decision_id,
        research_report_id="research-001",
        legs=[
            SelectedLeg(
                leg_index=0,
                action=LegAction.BUY,
                right=OptionRight.CALL,
                underlying="NVDA",
                expiration=date(2026, 8, 31),
                dte=21,
                strike=Decimal("180.00"),
                multiplier=100,
                trading_class="NVDA",
                contract_id=771234567,
                exchange="SMART",
                currency="USD",
                strike_policy=StrikeSelectionPolicy.TARGET_DELTA,
                reference_price=Decimal("180.25"),
                selection_reason="TARGET_DELTA: delta 0.60 is closest to the configured target",
                bid=Decimal("5.95"),
                ask=Decimal("6.05"),
                last=Decimal("6.00"),
                implied_volatility=Decimal("0.35"),
                delta=Decimal("0.60"),
                volume=Decimal("1200"),
                open_interest=Decimal("8400"),
                chain_snapshot_id="snap-chain-nvda",
                quote_snapshot_id="snap-option-quotes-nvda",
                quote_as_of=FIXED_NOW,
            )
        ],
        expiration=date(2026, 8, 31),
        dte=21,
        expiration_policy=ExpirationSelectionPolicy.TARGET_DTE,
        expiration_reason="expiration 2026-08-31 (DTE 21) chosen by TARGET_DTE",
        reference_price=Decimal("180.25"),
        reference_price_field="LAST",
        cost=ContractCostEstimate(
            available=True,
            currency="USD",
            estimated_debit=Decimal("605.00"),
            estimated_mid_debit=Decimal("600.00"),
            max_leg_spread_pct=1.67,
        ),
        selection_policy_version="1.0.0",
        input_snapshot_ids=["snap-chain-nvda", "snap-option-quotes-nvda", "snap-quote-nvda"],
        reasons=["expiration 2026-08-31 (DTE 21) chosen by TARGET_DTE"],
        rejected_candidates=[
            RejectedContract(
                reason="NOT_SELECTED_BY_POLICY",
                leg_index=0,
                contract_id=771234568,
                expiration=date(2026, 8, 31),
                strike=Decimal("185.00"),
                right=OptionRight.CALL,
                detail="valid, but another strike matched the policy more closely",
            )
        ],
        candidates_considered=14,
        versions=versions,
    )


@pytest.fixture
def contract_selection() -> ContractSelection:
    return ContractSelection(
        underlying="NVDA",
        strategy_type=StrategyType.LONG_CALL,
        as_of=FIXED_NOW,
        legs=[
            OptionLeg(
                underlying="NVDA",
                right=OptionRight.CALL,
                strike=Decimal("180.00"),
                expiration=date(2026, 8, 31),
                action=LegAction.BUY,
            )
        ],
        dte=21,
        target_delta=0.60,
        selection_rules_version="1.0.0",
    )


@pytest.fixture
def purchase_card(
    versions: SystemVersions,
    contract_selection: ContractSelection,
    source_reference: SourceReference,
) -> PurchaseCard:
    return PurchaseCard(
        card_id="card-001",
        created_at=FIXED_NOW,
        underlying="NVDA",
        strategy_type=StrategyType.LONG_CALL,
        contract=contract_selection,
        hypothesis=MarketHypothesis.B,
        confidence=0.72,
        expected_magnitude=ExpectedMagnitude.MODERATE,
        expected_horizon_days=21,
        quantity=2,
        requested_allocation_eur=Decimal("1200.00"),
        risk_limits={"max_allocation_per_trade_eur": "1500", "max_loss_pct": "50.0"},
        entry_conditions=["Bid-ask spread below 8%"],
        exit_policy={"trailing_stop_pct": "30.0", "close_at_dte": "7"},
        thesis_invalidation_conditions=["Guidance withdrawn or cut"],
        research_report_id="research-001",
        strategy_decision_id="strategy-001",
        sources=[source_reference],
        versions=versions,
    )


@pytest.fixture
def allocation_decision(versions: SystemVersions) -> AllocationDecision:
    return AllocationDecision(
        allocation_id="allocation-001",
        campaign_id="campaign-001",
        as_of=FIXED_NOW,
        total_budget_eur=Decimal("5000"),
        allocated_eur=Decimal("1200.00"),
        reserve_eur=Decimal("3800.00"),
        entries=[
            AllocationEntry(
                opportunity_id="card-001",
                ticker="NVDA",
                rank=1,
                opportunity_score=94.0,
                allocated_eur=Decimal("1200.00"),
            )
        ],
        versions=versions,
    )


@pytest.fixture
def account_snapshot():
    """A Milestone 7 account snapshot: broker reality, captured once."""
    from trading_system.risk.models import AccountPosition, AccountSnapshot

    return AccountSnapshot(
        snapshot_id="account-20260810T143000Z-abcdef0123456789",
        as_of=FIXED_NOW,
        captured_at=FIXED_NOW,
        broker="SIMULATOR",
        account_id="DU0000000",
        currency="EUR",
        trading_mode=TradingMode.PAPER,
        cash=Decimal("100000.00"),
        net_liquidation=Decimal("100000.00"),
        buying_power=Decimal("400000.00"),
        available_funds=Decimal("98000.00"),
        positions=[
            AccountPosition(
                symbol="SPY",
                security_type="STOCK",
                quantity=Decimal("100"),
                average_cost=Decimal("450.00"),
                currency="USD",
            )
        ],
        read_only=True,
        orders_submitted=0,
        simulated=True,
    )


@pytest.fixture
def campaign_snapshot():
    """A Milestone 7 campaign: EUR 5,000 with a 20% reserve, nothing committed."""
    from trading_system.risk.models import CampaignSnapshot

    return CampaignSnapshot(
        campaign_id="campaign-001",
        as_of=FIXED_NOW,
        currency="EUR",
        budget=Decimal("5000"),
        reserve=Decimal("1000.00"),
        open_positions=[],
        realized_pnl_today=None,
    )


@pytest.fixture
def eur_contract_selection(contract_selection_result):
    """The Milestone 6 selection, priced in the campaign's own currency.

    The shipped ``contract_selection_result`` is a US-listed contract quoted in
    USD, and the shipped campaign is denominated in EUR with no FX rate source
    configured — so the risk engine rejects it with ``CURRENCY_MISMATCH``. That
    is the correct behaviour and is asserted directly in
    ``tests/risk/test_engine.py``; converting at an invented rate would size a
    position wrongly by an amount nobody recorded.

    The allocation fixtures below need an *approved* authorisation to describe
    the boundary with, so this re-denominates the same contract rather than
    weakening the currency policy to accommodate a fixture.
    """
    legs = [leg.model_copy(update={"currency": "EUR"}) for leg in contract_selection_result.legs]
    cost = contract_selection_result.cost
    assert cost is not None
    return contract_selection_result.model_copy(
        update={"legs": legs, "cost": cost.model_copy(update={"currency": "EUR"})}
    )


@pytest.fixture
def allocation_candidate(eur_contract_selection, strategy_decision_record):
    """A Milestone 7 purchase candidate, carried across from Milestone 6.

    Built through the real builder rather than by hand: the contract tests
    assert that the *boundary* is crossed correctly, and a hand-assembled
    candidate would pass even if the translation were broken.
    """
    from trading_system.allocation.candidates import build_candidate
    from trading_system.domain.enums import ExpectedMagnitude, StrategyType
    from trading_system.infrastructure.settings import CampaignRankingConfig
    from trading_system.strategies.registry import StrategyRegistry

    registry = StrategyRegistry.from_config(load_config(default_config_dir()))
    specification = registry.get(StrategyType.LONG_CALL)
    assert specification is not None

    return build_candidate(
        eur_contract_selection,
        specification,
        CampaignRankingConfig(),
        decision=strategy_decision_record,
        expected_magnitude=ExpectedMagnitude.MODERATE,
    )


@pytest.fixture
def risk_evaluation(allocation_candidate, campaign_snapshot, account_snapshot):
    """The deterministic verdict on the candidate above."""
    from trading_system.risk.engine import RiskEngine
    from trading_system.risk.limits import resolve_limits

    limits = resolve_limits(load_config(default_config_dir()))
    return RiskEngine(limits).evaluate(
        allocation_candidate,
        campaign_snapshot,
        as_of=FIXED_NOW,
        account=account_snapshot,
        trading_mode=TradingMode.PAPER,
    )


@pytest.fixture
def campaign_allocation(
    versions: SystemVersions,
    allocation_candidate,
    campaign_snapshot,
    account_snapshot,
    risk_evaluation,
):
    """One capital authorisation: the Milestone 7 to Milestone 8 boundary."""
    from trading_system.allocation.budget_allocator import AllocationEngine
    from trading_system.allocation.models import CampaignAllocation, allocation_identifier
    from trading_system.domain.enums import AllocationOutcome
    from trading_system.risk.engine import RiskEngine
    from trading_system.risk.exposure import would_add
    from trading_system.risk.limits import resolve_limits

    limits = resolve_limits(load_config(default_config_dir()))
    [decision] = AllocationEngine(limits, RiskEngine(limits)).allocate(
        [allocation_candidate],
        campaign_snapshot,
        as_of=FIXED_NOW,
        account=account_snapshot,
    )
    assert decision.outcome is AllocationOutcome.APPROVED, decision.evaluation.explain()
    calculation = decision.calculation
    assert calculation is not None

    return CampaignAllocation(
        allocation_id=allocation_identifier(
            run_id="allocation-run-001",
            opportunity_id=allocation_candidate.opportunity_id,
            outcome=decision.outcome,
            quantity=decision.quantity,
            capital_committed=decision.capital_committed,
        ),
        run_id="allocation-run-001",
        opportunity_id=allocation_candidate.opportunity_id,
        campaign_id="campaign-001",
        symbol="NVDA",
        as_of=FIXED_NOW,
        decided_at=FIXED_NOW,
        outcome=decision.outcome,
        strategy=allocation_candidate.strategy,
        strategy_version="1.0.0",
        direction=allocation_candidate.risk_profile.directional_view,
        legs=list(allocation_candidate.legs),
        expiration=allocation_candidate.expiration,
        dte=allocation_candidate.dte,
        quantity=decision.quantity,
        unit_cost=calculation.unit_cost,
        capital_committed=decision.capital_committed,
        unit_max_loss=calculation.unit_max_loss,
        total_max_loss=decision.total_max_loss,
        risk_basis=calculation.max_loss_basis,
        price_source=allocation_candidate.price.source,
        currency=allocation_candidate.price.currency,
        calculation=calculation,
        risk_outcome=decision.evaluation.outcome,
        reason_codes=list(decision.evaluation.reason_codes),
        allocation_reasons=list(decision.reasons),
        opportunity_score=allocation_candidate.score,
        rank=decision.rank,
        risk_evaluation=decision.evaluation,
        exposure_after=would_add(
            campaign_snapshot,
            allocation_candidate,
            quantity=decision.quantity,
            unit_cost=calculation.unit_cost,
            unit_max_loss=calculation.unit_max_loss,
        ),
        contract_selection_id=allocation_candidate.contract_selection_id,
        contract_run_id=allocation_candidate.contract_run_id,
        strategy_decision_id=allocation_candidate.strategy_decision_id,
        strategy_run_id=allocation_candidate.strategy_run_id,
        research_report_id=allocation_candidate.research_report_id,
        account_snapshot_id=account_snapshot.snapshot_id,
        campaign_snapshot_as_of=campaign_snapshot.as_of,
        input_snapshot_ids=list(allocation_candidate.input_snapshot_ids),
        trading_mode=TradingMode.PAPER,
        versions=versions,
    )


@pytest.fixture
def allocation_run(versions: SystemVersions, campaign_snapshot, campaign_allocation):
    """The immutable record of one Milestone 7 allocation run."""
    from trading_system.allocation.models import AllocationRunCounts, AllocationRunResult
    from trading_system.domain.enums import AllocationPolicy, AllocationRunStatus

    return AllocationRunResult(
        run_id="allocation-run-001",
        campaign_id="campaign-001",
        as_of=FIXED_NOW,
        generated_at=FIXED_NOW,
        status=AllocationRunStatus.SUCCESS,
        policy=AllocationPolicy.PRIORITY_FIRST_FIT,
        policy_version="1.0.0",
        trading_mode=TradingMode.PAPER,
        campaign_before=campaign_snapshot,
        account_snapshot_id=campaign_allocation.account_snapshot_id,
        budget=campaign_snapshot.budget,
        reserve=campaign_snapshot.reserve,
        allocated_before=campaign_snapshot.allocated,
        allocated_this_run=campaign_allocation.capital_committed,
        available_after=campaign_snapshot.available - campaign_allocation.capital_committed,
        risk_authorized_this_run=campaign_allocation.total_max_loss,
        allocations=[campaign_allocation],
        counts=AllocationRunCounts(candidates_considered=1, approved=1),
        contract_run_id=campaign_allocation.contract_run_id,
        strategy_run_id=campaign_allocation.strategy_run_id,
        research_run_id="research-run-001",
        versions=versions,
    )


@pytest.fixture
def risk_decision(versions: SystemVersions) -> RiskDecision:
    return RiskDecision(
        decision_id="risk-001",
        purchase_card_id="card-001",
        as_of=FIXED_NOW,
        outcome=RiskOutcome.APPROVED,
        reason_codes=[RiskReasonCode.OK],
        evaluated_limits={"campaign_budget_eur": "5000", "open_positions": "0"},
        trading_mode=TradingMode.PAPER,
        versions=versions,
    )


@pytest.fixture
def order_intent(versions: SystemVersions, contract_selection: ContractSelection) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-001",
        purchase_card_id="card-001",
        risk_decision_id="risk-001",
        created_at=FIXED_NOW,
        underlying="NVDA",
        strategy_type=StrategyType.LONG_CALL,
        legs=list(contract_selection.legs),
        quantity=2,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("6.00"),
        max_slippage_bps=50,
        trading_mode=TradingMode.PAPER,
        versions=versions,
    )


@pytest.fixture
def execution_result() -> ExecutionResult:
    return ExecutionResult(
        intent_id="intent-001",
        broker="SIMULATOR",
        broker_order_id="sim-1",
        status=OrderStatus.FILLED,
        orders_submitted=1,
        filled_quantity=2,
        average_fill_price=Decimal("5.95"),
        fills=[
            Fill(
                fill_id="fill-001",
                leg_index=0,
                quantity=2,
                price=Decimal("5.95"),
                commission=Decimal("1.30"),
                filled_at=FIXED_NOW,
            )
        ],
        submitted_at=FIXED_NOW,
        last_update_at=FIXED_NOW,
        trading_mode=TradingMode.PAPER,
    )


@pytest.fixture
def position_snapshot(contract_selection: ContractSelection) -> PositionSnapshot:
    return PositionSnapshot(
        position_id="position-001",
        purchase_card_id="card-001",
        as_of=FIXED_NOW,
        state=PositionState.OPEN,
        underlying="NVDA",
        strategy_type=StrategyType.LONG_CALL,
        legs=list(contract_selection.legs),
        quantity=2,
        average_entry_price=Decimal("5.95"),
        market_value_eur=Decimal("1250.00"),
        unrealized_pnl_eur=Decimal("60.00"),
        days_to_expiration=18,
        source="SIMULATOR",
    )


@pytest.fixture
def exit_decision(versions: SystemVersions) -> ExitDecision:
    return ExitDecision(
        decision_id="exit-001",
        position_id="position-001",
        as_of=FIXED_NOW,
        decision=ExitAction.SELL,
        reason=ExitReason.TRAILING_STOP,
        detail="Drawdown from peak exceeded the configured 30% trailing stop.",
        versions=versions,
    )


@pytest.fixture
def trade_snapshot(versions: SystemVersions) -> TradeSnapshot:
    return TradeSnapshot(
        trade_id="trade-001",
        underlying="NVDA",
        strategy_type=StrategyType.LONG_CALL,
        opened_at=FIXED_NOW,
        closed_at=FIXED_NOW,
        final_state=PositionState.CLOSED,
        purchase_card_id="card-001",
        research_report_id="research-001",
        strategy_decision_id="strategy-001",
        allocation_id="allocation-001",
        risk_decision_id="risk-001",
        order_intent_id="intent-001",
        exit_decision_id="exit-001",
        realized_pnl_eur=Decimal("180.00"),
        r_multiple=0.6,
        max_favorable_excursion_eur=Decimal("260.00"),
        max_adverse_excursion_eur=Decimal("-95.00"),
        exit_reason=ExitReason.TRAILING_STOP,
        versions=versions,
    )


@pytest.fixture
def tmp_config_dir(tmp_path: Path, config_dir: Path) -> Iterator[Path]:
    """A writable copy of the real configuration tree, for mutation tests."""
    import shutil

    destination = tmp_path / "config"
    shutil.copytree(config_dir, destination)
    yield destination


# ---------------------------------------------------------------------------
# Milestone 9: positions, reservations and reconciliation
#
# Two position records, deliberately separate: what the broker says it holds,
# and what confirmed fills say should exist. The contract suite validates each
# against its own schema, exactly as it does for every earlier milestone.
# ---------------------------------------------------------------------------
@pytest.fixture
def broker_position_snapshot():
    """What the broker reported holding at one instant."""
    from tests.positions.factories import ACCOUNT, option_position
    from trading_system.positions.snapshot import build_position_snapshot

    return build_position_snapshot(
        [option_position()],
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=FIXED_NOW,
        observed_at=FIXED_NOW,
    )


@pytest.fixture
def position_fill():
    """One recorded broker fill, in the broker's own quoted terms."""
    from tests.positions.factories import MASKED, broker_execution
    from trading_system.positions.fills import ContractTerms, to_observed_fill

    return to_observed_fill(
        broker_execution(),
        observed_at=FIXED_NOW,
        account_reference=MASKED,
        terms=ContractTerms(multiplier=100),
        execution_id="execution-1",
    )


@pytest.fixture
def expected_position(position_fill):
    """What that fill says should exist — the internal ledger, not the broker's."""
    from tests.positions.factories import MASKED
    from trading_system.positions.expected import expected_from_fills

    [position] = expected_from_fills([position_fill], as_of=FIXED_NOW, account_reference=MASKED)
    return position


@pytest.fixture
def reservation():
    """Capital committed to one authorisation, before anything moved."""
    from tests.positions import factories

    return factories.reservation()


@pytest.fixture
def reservation_event(reservation):
    """One appended observation about that capital."""
    from trading_system.domain.enums import (
        ReservationEventType,
        ReservationReasonCode,
        ReservationState,
    )
    from trading_system.reservations.models import ReservationEvent

    return ReservationEvent(
        event_id="resevt-0000000000000000000",
        reservation_id=reservation.reservation_id,
        sequence=0,
        event_type=ReservationEventType.RESERVATION_CONSUMED,
        state=ReservationState.CONSUMED,
        occurred_at=FIXED_NOW,
        observed_at=FIXED_NOW,
        source="reconciliation",
        consumed_delta=reservation.authorized_amount,
        reason_code=ReservationReasonCode.FILLED,
        detail="the broker reported a complete fill",
    )


@pytest.fixture
def reconciliation_result(broker_position_snapshot, expected_position, system_config):
    """One comparison between the internal ledger and broker reality."""
    from tests.positions.factories import MASKED
    from trading_system.domain.enums import BrokerReadStatus
    from trading_system.reconciliation.engine import (
        ReconciliationEngine,
        ReconciliationInputs,
    )

    return ReconciliationEngine(system_config.reconciliation).reconcile(
        ReconciliationInputs(
            campaign_id="campaign-001",
            broker="SIMULATOR",
            account_reference=MASKED,
            trading_mode=TradingMode.PAPER,
            as_of=FIXED_NOW,
            observed_at=FIXED_NOW,
            snapshot=broker_position_snapshot,
            expected=(expected_position,),
            account_read=BrokerReadStatus.OK,
            orders_read=BrokerReadStatus.EMPTY,
            fills_read=BrokerReadStatus.EMPTY,
            config_version="test",
        )
    )


@pytest.fixture
def reconciliation_event(reconciliation_result):
    """One appended step in that comparison's own history."""
    from trading_system.domain.enums import ReconciliationEventType
    from trading_system.reconciliation.models import ReconciliationEvent

    return ReconciliationEvent(
        event_id="recevt-0000000000000000000",
        reconciliation_id=reconciliation_result.reconciliation_id,
        sequence=0,
        event_type=ReconciliationEventType.RECONCILIATION_STARTED,
        occurred_at=FIXED_NOW,
        observed_at=FIXED_NOW,
        source="reconciliation",
        detail="comparing against SIMULATOR",
    )
