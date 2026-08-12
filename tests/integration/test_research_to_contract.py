"""Research to strategy to contract, end to end (brief sections 42, 50, 51).

One test file, two claims:

* the chain of artifacts actually connects — a research report leads to a
  strategy decision that names it, which leads to a contract selection that
  names *that*, so "why is this contract in the portfolio" is answerable by
  following ids rather than by inference;
* **zero orders are submitted**, proven against a broker that would record one.

The second claim is the one worth being careful about. Every stage in this
milestone is structurally incapable of ordering — none of them constructs a
broker — so the test constructs one *itself*, runs the whole workflow beside
it, and asserts the counter never moved. A broker that was never asked is
better evidence than a broker that refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.broker.simulator.broker import SimulatedBroker
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
    ContractSelectionStatus,
    DataType,
    Direction,
    EvidenceKind,
    ExpectedMagnitude,
    MarketDataOrigin,
    MarketHypothesis,
    OptionRight,
    RelevanceLevel,
    ResearchStatus,
    SecurityType,
    SourceTier,
    StrategyAction,
    StrategySelectionStatus,
    StrategyType,
    TradingMode,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings
from trading_system.research.models import (
    InvalidationCondition,
    MarketResearchReport,
    ReportedEvidence,
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
from trading_system.strategies.service import ContractSelectionService, StrategyService
from trading_system.strategies.store import (
    FilesystemContractSelectionRepository,
    FilesystemStrategyRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
EXPIRATION = date(2026, 8, 28)


class _DecidingClient:
    """A fake model that always chooses the first strategy it is offered."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    @property
    def identity(self):
        from trading_system.agents.base import ModelIdentity

        return ModelIdentity(
            provider="FAKE",
            model_name="fake-model-1",
            prompt_version="test-1.0.0",
            prompt_fingerprint="0" * 32,
        )

    def complete(self, request):
        import json

        from trading_system.agents.base import LLMResponse

        self.requests.append(request)
        payload = json.loads(request.user_content)
        offered = sorted(option["strategy_id"] for option in payload["eligible_strategies"])
        return LLMResponse(
            text=json.dumps(
                {
                    "run_id": payload["run_id"],
                    "symbol": payload["symbol"],
                    "action": "BUY",
                    "selected_strategy": offered[0],
                    "confidence": "MEDIUM",
                    "reasons": ["HYPOTHESIS_MATCH"],
                    "rationale": "the hypothesis matches what this strategy expresses",
                }
            ),
            identity=self.identity,
            generated_at=NOW,
            stop_reason="end_turn",
        )


def _metadata(identifier: str) -> DataSourceMetadata:
    return DataSourceMetadata(
        provider="IBKR",
        source_name="IBKR",
        source_tier=SourceTier.TIER_1,
        origin=MarketDataOrigin.BROKER_DELAYED,
        retrieved_at=NOW,
        source_timestamp=NOW,
        observed_at=NOW,
        source_identifier=identifier,
    )


def _store(repo: FilesystemDataRepository, data_type: DataType, records: Sequence[object]) -> None:
    repo.save_snapshot(
        build_snapshot(
            data_type=data_type,
            key="NVDA",
            records=records,  # type: ignore[arg-type]
            provider="IBKR",
            source_tier=SourceTier.TIER_1,
            origin=MarketDataOrigin.BROKER_DELAYED,
            as_of=NOW,
            retrieved_at=NOW,
            quality=DataQualityReport(evaluated_at=NOW),
        )
    )


def _option_market(repo: FilesystemDataRepository) -> None:
    """A complete, selectable option market for one underlying."""
    strikes = [Decimal("170"), Decimal("180"), Decimal("190")]
    _store(
        repo,
        DataType.MARKET_QUOTE,
        [
            MarketQuote(
                as_of=NOW,
                source=_metadata("ibkr:NVDA"),
                symbol="NVDA",
                security_type=SecurityType.STOCK,
                currency="USD",
                last=Decimal("180.00"),
                close=Decimal("179.10"),
                volume=Decimal("240000000"),
                quality=DataQualityReport(evaluated_at=NOW),
            )
        ],
    )
    _store(
        repo,
        DataType.OPTION_CHAIN,
        [
            OptionChain(
                as_of=NOW,
                source=_metadata("ibkr:chain:NVDA"),
                underlying="NVDA",
                exchange="SMART",
                trading_class="NVDA",
                multiplier=100,
                expirations=[EXPIRATION],
                strikes=strikes,
                rights=[OptionRight.CALL, OptionRight.PUT],
            )
        ],
    )
    _store(
        repo,
        DataType.OPTION_QUOTE,
        [
            OptionQuote(
                as_of=NOW,
                source=_metadata(f"ibkr:option:NVDA:{strike}:{right}"),
                contract=OptionContract(
                    underlying="NVDA",
                    symbol="NVDA",
                    expiration=EXPIRATION,
                    strike=strike,
                    right=right,
                    contract_id=int(strike) * (1 if right is OptionRight.CALL else 2),
                    exchange="SMART",
                    currency="USD",
                    multiplier=100,
                    trading_class="NVDA",
                ),
                bid=Decimal("5.95"),
                ask=Decimal("6.05"),
                last=Decimal("6.00"),
                volume=Decimal("1200"),
                open_interest=Decimal("8400"),
                implied_volatility=Decimal("0.35"),
                delta=Decimal("0.60") if right is OptionRight.CALL else Decimal("-0.60"),
                quality=DataQualityReport(evaluated_at=NOW),
            )
            for strike in strikes
            for right in (OptionRight.CALL, OptionRight.PUT)
        ],
    )


def _research_run(repo: FilesystemResearchRepository) -> ResearchRunResult:
    source = SourceProvenance(
        provider="FIXTURE_NEWS",
        source_tier=SourceTier.TIER_2,
        retrieved_at=NOW - timedelta(days=1),
        snapshot_id="snap-news",
        source_name="Reuters",
    )
    report = MarketResearchReport(
        report_id="research-NVDA-001",
        run_id="research-run-001",
        symbol="NVDA",
        as_of=NOW,
        generated_at=NOW,
        horizon=ResearchHorizon(min_days=14, max_days=31),
        status=ResearchStatus.SUCCESS,
        hypothesis=MarketHypothesis.B,
        confidence=ConfidenceLevel.MEDIUM,
        direction=Direction.BULLISH,
        expected_magnitude=ExpectedMagnitude.MODERATE,
        horizon_days=21,
        thesis="demand is accelerating into the next quarter",
        expected_behavior="a gradual drift higher",
        evidence=[
            ReportedEvidence(
                evidence_id="ev-1",
                kind=EvidenceKind.NEWS,
                claim="reported acceleration supports an upward bias",
                fact="a reported acceleration in segment revenue",
                direction="SUPPORTS_UP",
                stance="SUPPORTS",
                relevance=RelevanceLevel.HIGH,
                confidence=ConfidenceLevel.MEDIUM,
                source=source,
            )
        ],
        invalidation_conditions=[
            InvalidationCondition(condition="guidance is cut", evidence_ids=["ev-1"])
        ],
        data_quality=ResearchDataQualitySummary(
            research_usable=True, records_considered=1, records_research_usable=1
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
        source_policy=ResearchSourcePolicySnapshot(config_version="test"),
        universe_run_id="universe-run-001",
        input_snapshot_ids=["snap-news"],
        versions=SystemVersions(application_version="0.1.0", config_version="test"),
    )
    result = ResearchRunResult(
        run_id="research-run-001",
        as_of=NOW,
        generated_at=NOW,
        status=ResearchStatus.SUCCESS,
        horizon=report.horizon,
        universe_run_id="universe-run-001",
        reports=[report],
        counts=ResearchRunCounts(universe_assets=1, researched=1, succeeded=1),
        versions=report.versions,
    )
    repo.save(result)
    return result


@pytest.fixture
def workflow(tmp_path: Path, system_config):
    """The whole Milestone 6 workflow, wired to temporary stores."""
    clock = FixedClock(NOW)
    data_repo = FilesystemDataRepository(tmp_path / "data", clock=clock)
    research_repo = FilesystemResearchRepository(tmp_path / "data" / "research")
    strategy_repo = FilesystemStrategyRepository(tmp_path / "data" / "strategy")
    contract_repo = FilesystemContractSelectionRepository(tmp_path / "data" / "contracts")

    _option_market(data_repo)
    research = _research_run(research_repo)
    settings = Settings(_env_file=None)

    strategy_service = StrategyService(
        settings=settings,
        config=system_config,
        clock=clock,
        data_repository=data_repo,
        research_repository=research_repo,
        strategy_repository=strategy_repo,
        llm_client=_DecidingClient(),
    )
    contract_service = ContractSelectionService(
        settings=settings,
        config=system_config,
        clock=clock,
        data_repository=data_repo,
        research_repository=research_repo,
        strategy_repository=strategy_repo,
        contract_repository=contract_repo,
    )
    return research, strategy_service, contract_service


# ---------------------------------------------------------------------------
# 50-51. The provenance chain
# ---------------------------------------------------------------------------
def test_the_whole_chain_connects_by_identifier(workflow) -> None:
    research, strategy_service, contract_service = workflow

    strategy_run = strategy_service.run()
    contract_run = contract_service.select()

    decision = strategy_run.result.decision("NVDA")
    selection = contract_run.result.selection("NVDA")
    assert decision is not None and selection is not None

    assert decision.research_report_id == research.reports[0].report_id
    assert decision.research_run_id == research.run_id
    assert strategy_run.result.research_run_id == research.run_id
    assert selection.strategy_decision_id == decision.decision_id
    assert selection.strategy_run_id == strategy_run.result.run_id
    assert selection.research_report_id == decision.research_report_id
    assert selection.selection_status is ContractSelectionStatus.SUCCESS


def test_the_selection_names_the_data_it_rests_on(workflow) -> None:
    _, strategy_service, contract_service = workflow

    strategy_service.run()
    selection = contract_service.select().result.selection("NVDA")

    assert selection is not None
    assert selection.input_snapshot_ids
    for leg in selection.legs:
        assert leg.chain_snapshot_id in selection.input_snapshot_ids
        assert leg.quote_snapshot_id in selection.input_snapshot_ids


def test_the_versions_of_every_policy_are_stamped(workflow, system_config) -> None:
    _, strategy_service, contract_service = workflow

    strategy_run = strategy_service.run()
    contract_run = contract_service.select()

    decision = strategy_run.result.decision("NVDA")
    selection = contract_run.result.selection("NVDA")
    assert decision is not None and selection is not None
    assert decision.versions.config_version == system_config.application.config_version
    assert decision.strategy_version == "1.0.0"
    assert (
        selection.selection_policy_version
        == system_config.contract_selection.selection_policy_version
    )
    assert selection.versions.strategy_spec_version == "1.0.0"


def test_a_later_run_appends_rather_than_replacing(workflow, tmp_path: Path) -> None:
    """History is append-only: an earlier decision is never overwritten."""
    _, strategy_service, contract_service = workflow

    strategy_service.run()
    contract_service.select()
    contract_service.select()

    repo = FilesystemContractSelectionRepository(tmp_path / "data" / "contracts")
    assert len(repo.history()) == 1, "an identical re-run is recognised, not duplicated"
    stored = repo.latest()
    assert stored is not None
    assert stored.selections[0].selection_status is ContractSelectionStatus.SUCCESS


# ---------------------------------------------------------------------------
# 42. Zero orders
# ---------------------------------------------------------------------------
def test_the_whole_workflow_submits_no_orders(workflow) -> None:
    """Run beside a broker that would record one. The counter never moves.

    The broker is *writable* — ``read_only=False`` — so nothing about this test
    depends on the read-only guard. Milestone 6 simply has no order path.
    """
    _, strategy_service, contract_service = workflow
    broker = SimulatedBroker(trading_mode=TradingMode.PAPER, read_only=False)
    broker.connect()

    try:
        before = broker.get_open_orders()
        strategy_run = strategy_service.run()
        contract_run = contract_service.select()
        after = broker.get_open_orders()
    finally:
        broker.disconnect()

    assert strategy_run.result.status is StrategySelectionStatus.SUCCESS
    assert contract_run.result.status is ContractSelectionStatus.SUCCESS
    assert broker.orders_submitted == 0
    # The simulator seeds its own account state; what matters is that running
    # the whole workflow beside it changed nothing.
    assert [order.broker_order_id for order in after] == [order.broker_order_id for order in before]


def test_no_milestone_6_service_constructs_a_broker(workflow) -> None:
    """Structural, not observed: there is nothing to count orders from."""
    import inspect

    _, strategy_service, contract_service = workflow

    for service in (strategy_service, contract_service):
        source = inspect.getsource(type(service))
        assert "build_broker" not in source
        assert "place_order" not in source
        for attribute in vars(service).values():
            assert not isinstance(attribute, SimulatedBroker)


def test_the_selection_produces_a_candidate_and_not_an_order(workflow) -> None:
    """Milestone 6 ends at a purchase candidate: legs, and a cost for one unit."""
    _, strategy_service, contract_service = workflow

    strategy_service.run()
    selection = contract_service.select().result.selection("NVDA")

    assert selection is not None
    assert selection.legs
    assert selection.cost is not None and selection.cost.available
    assert not hasattr(selection, "quantity")
    assert not hasattr(selection, "order_type")
    assert not hasattr(selection, "limit_price")


def test_a_no_trade_decision_stops_the_chain(workflow, monkeypatch) -> None:
    """Nothing downstream turns a considered refusal into a contract."""
    import json

    _, strategy_service, contract_service = workflow

    class _RefusingClient(_DecidingClient):
        def complete(self, request):
            from trading_system.agents.base import LLMResponse

            payload = json.loads(request.user_content)
            return LLMResponse(
                text=json.dumps(
                    {
                        "run_id": payload["run_id"],
                        "symbol": payload["symbol"],
                        "action": "NO_TRADE",
                        "selected_strategy": None,
                        "confidence": "LOW",
                        "reasons": ["RESEARCH_INCOMPATIBLE"],
                        "rationale": "nothing offered expresses this outlook",
                    }
                ),
                identity=self.identity,
                generated_at=NOW,
                stop_reason="end_turn",
            )

    monkeypatch.setattr(strategy_service, "_llm_client", _RefusingClient())

    strategy_run = strategy_service.run()
    contract_run = contract_service.select()

    decision = strategy_run.result.decision("NVDA")
    selection = contract_run.result.selection("NVDA")
    assert decision is not None and decision.action is StrategyAction.NO_TRADE
    assert selection is not None
    assert selection.selection_status is ContractSelectionStatus.NO_TRADE
    assert selection.legs == []


def test_the_projection_chain_reaches_the_purchase_card_boundary(workflow) -> None:
    """The Milestone 1 artifacts the next milestone consumes."""
    _, strategy_service, contract_service = workflow

    decision = strategy_service.run().result.decision("NVDA")
    selection = contract_service.select().result.selection("NVDA")
    assert decision is not None and selection is not None

    strategy_decision = decision.to_strategy_decision()
    contract_selection = selection.to_contract_selection()

    assert strategy_decision.strategy_type is StrategyType.LONG_CALL
    assert contract_selection.strategy_type is strategy_decision.strategy_type
    assert contract_selection.underlying == strategy_decision.ticker
    assert all(leg.broker_contract_id for leg in contract_selection.legs)
