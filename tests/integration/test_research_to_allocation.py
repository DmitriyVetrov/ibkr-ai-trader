"""Research to strategy to contract to risk to allocation, end to end.

Brief sections 29, 37.9, 40 and 41. One test file, three claims:

* the chain of artifacts actually connects — a research report leads to a
  strategy decision that names it, which leads to a contract selection that
  names *that*, which leads to a capital authorisation that names all three, so
  "why is this position authorised" is answerable by following ids rather than
  by inference;
* **zero orders are submitted**, proven against a broker that would record one;
* the authorisation is an authorisation and not an order.

The second claim is the one worth being careful about. Every stage in this
milestone is structurally incapable of ordering — none of them constructs a
broker — so the test constructs one *itself*, runs the whole workflow beside
it, and asserts the counter never moved. A broker that was never asked is
better evidence than a broker that refused.

The account snapshot is built from that same simulated broker through the one
sanctioned boundary, which also demonstrates the shape the real capture takes:
one read, one stored snapshot, and a risk engine that never sees the broker.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.allocation.service import AllocationService
from trading_system.allocation.store import FilesystemAllocationRepository
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
    AllocationOutcome,
    AllocationRunStatus,
    ConfidenceLevel,
    ContractSelectionStatus,
    DataType,
    Direction,
    EvidenceKind,
    ExpectedMagnitude,
    MarketDataOrigin,
    MarketHypothesis,
    MaxLossBasis,
    OptionRight,
    RelevanceLevel,
    ResearchStatus,
    RiskOutcome,
    SecurityType,
    SourceTier,
    StrategySelectionStatus,
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
from trading_system.risk.account import build_account_snapshot
from trading_system.risk.store import FilesystemAccountSnapshotRepository
from trading_system.strategies.service import ContractSelectionService, StrategyService
from trading_system.strategies.store import (
    FilesystemContractSelectionRepository,
    FilesystemStrategyRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
EXPIRATION = date(2026, 8, 28)


class _DecidingClient:
    """A fake model that always chooses the first strategy it is offered.

    A protocol implementation, not a vendor: no credential, no network, and the
    same code path a real model takes. What it decides is irrelevant to this
    file — the point is that whatever it says, it cannot influence a quantity.
    """

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

        payload = json.loads(request.user_content)
        offered = sorted(option["strategy_id"] for option in payload["eligible_strategies"])
        return LLMResponse(
            text=json.dumps(
                {
                    "run_id": payload["run_id"],
                    "symbol": payload["symbol"],
                    "action": "BUY",
                    "selected_strategy": offered[0],
                    "confidence": "HIGH",
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
    """A complete, selectable and priced option market for one underlying."""
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
                currency="EUR",
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
                    currency="EUR",
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
        confidence=ConfidenceLevel.HIGH,
        direction=Direction.BULLISH,
        expected_magnitude=ExpectedMagnitude.LARGE,
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
                confidence=ConfidenceLevel.HIGH,
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
def broker() -> Iterator[SimulatedBroker]:
    """A **writable** broker, so nothing depends on the read-only guard.

    Milestone 7 simply has no order path; this proves it rather than relying on
    a refusal.
    """
    connection = SimulatedBroker(
        trading_mode=TradingMode.PAPER, read_only=False, clock=FixedClock(NOW)
    )
    connection.connect()
    try:
        yield connection
    finally:
        connection.disconnect()


@pytest.fixture
def workflow(tmp_path: Path, system_config, broker: SimulatedBroker):
    """The whole Milestone 5-7 workflow, wired to temporary stores."""
    clock = FixedClock(NOW)
    data_repo = FilesystemDataRepository(tmp_path / "data", clock=clock)
    research_repo = FilesystemResearchRepository(tmp_path / "data" / "research")
    strategy_repo = FilesystemStrategyRepository(tmp_path / "data" / "strategy")
    contract_repo = FilesystemContractSelectionRepository(tmp_path / "data" / "contracts")
    allocation_repo = FilesystemAllocationRepository(tmp_path / "data" / "allocation")
    account_repo = FilesystemAccountSnapshotRepository(tmp_path / "data" / "accounts")

    _option_market(data_repo)
    research = _research_run(research_repo)
    settings = Settings(_env_file=None)

    # The one sanctioned broker boundary: read once, store, and never look
    # again. The engines below hold no broker and could not refresh this.
    account_repo.save(
        build_account_snapshot(
            broker.get_account(),
            broker.get_positions(),
            broker=broker.name,
            trading_mode=TradingMode.PAPER,
            captured_at=NOW,
            orders_submitted=broker.orders_submitted,
            read_only=broker.read_only,
            simulated=True,
        )
    )

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
    allocation_service = AllocationService(
        settings=settings,
        config=system_config,
        clock=clock,
        research_repository=research_repo,
        strategy_repository=strategy_repo,
        contract_repository=contract_repo,
        allocation_repository=allocation_repo,
        account_repository=account_repo,
        root=tmp_path,
    )
    return research, strategy_service, contract_service, allocation_service


def _run_everything(workflow):
    research, strategy_service, contract_service, allocation_service = workflow
    strategy_run = strategy_service.run()
    contract_run = contract_service.select()
    allocation_run = allocation_service.run()
    return research, strategy_run, contract_run, allocation_run


# ---------------------------------------------------------------------------
# 40. The provenance chain
# ---------------------------------------------------------------------------
def test_the_whole_chain_connects_by_identifier(workflow) -> None:
    research, strategy_run, contract_run, allocation_run = _run_everything(workflow)

    decision = strategy_run.result.decision("NVDA")
    selection = contract_run.result.selection("NVDA")
    allocation = allocation_run.result.allocation("NVDA")
    assert decision is not None and selection is not None and allocation is not None

    assert decision.research_report_id == research.reports[0].report_id
    assert selection.strategy_decision_id == decision.decision_id
    assert allocation.contract_selection_id == selection.selection_id
    assert allocation.contract_run_id == contract_run.result.run_id
    assert allocation.strategy_decision_id == decision.decision_id
    assert allocation.research_report_id == decision.research_report_id
    assert allocation.account_snapshot_id is not None

    assert strategy_run.result.status is StrategySelectionStatus.SUCCESS
    assert contract_run.result.status is ContractSelectionStatus.SUCCESS
    assert allocation_run.result.status is AllocationRunStatus.SUCCESS


def test_the_allocation_is_anchored_at_the_research_instant(workflow) -> None:
    """One ``as_of`` runs the length of the chain, so nothing is re-priced."""
    research, _, contract_run, allocation_run = _run_everything(workflow)

    assert contract_run.result.as_of == research.as_of
    assert allocation_run.result.as_of == research.as_of


def test_the_authorisation_carries_the_arithmetic_that_produced_it(workflow) -> None:
    _, _, _, allocation_run = _run_everything(workflow)
    allocation = allocation_run.result.allocation("NVDA")

    assert allocation is not None
    assert allocation.outcome is AllocationOutcome.APPROVED
    assert allocation.quantity >= 1
    assert allocation.unit_cost == Decimal("605.00"), "ask 6.05 x 100 multiplier"
    assert allocation.capital_committed == allocation.unit_cost * allocation.quantity
    assert allocation.risk_basis is MaxLossBasis.NET_DEBIT_PAID
    assert allocation.total_max_loss == allocation.capital_committed
    assert allocation.calculation is not None
    assert allocation.calculation.binding_constraint is not None


def test_the_campaign_accounting_balances_end_to_end(workflow) -> None:
    _, _, _, allocation_run = _run_everything(workflow)
    result = allocation_run.result

    assert result.budget == Decimal("5000")
    assert result.reserve == Decimal("1000.00")
    assert (
        result.allocated_before + result.allocated_this_run + result.available_after
        == result.budget - result.reserve
    )
    assert result.allocated_this_run <= Decimal("4000.00")


def test_the_versions_of_every_policy_are_stamped(workflow, system_config) -> None:
    _, _, _, allocation_run = _run_everything(workflow)
    result = allocation_run.result

    assert result.versions.config_version == system_config.application.config_version
    assert result.versions.data_source_versions["risk"] == system_config.risk.config_version
    assert result.policy_version == system_config.campaign.allocation.policy_version


# ---------------------------------------------------------------------------
# 29. Zero orders
# ---------------------------------------------------------------------------
def test_the_whole_workflow_submits_no_orders(workflow, broker: SimulatedBroker) -> None:
    """Run beside a broker that would record one. The counter never moves."""
    before = broker.get_open_orders()

    *_, allocation_run = _run_everything(workflow)

    after = broker.get_open_orders()

    assert allocation_run.result.status is AllocationRunStatus.SUCCESS
    assert broker.orders_submitted == 0
    assert [o.broker_order_id for o in after] == [o.broker_order_id for o in before]


def test_no_milestone_7_service_constructs_a_broker(workflow) -> None:
    """Structural, not observed: there is nothing to count orders from."""
    import inspect

    _, strategy_service, contract_service, allocation_service = workflow

    for service in (strategy_service, contract_service, allocation_service):
        source = inspect.getsource(type(service))
        assert "build_broker" not in source
        assert "place_order" not in source
        for attribute in vars(service).values():
            assert not isinstance(attribute, SimulatedBroker)


def test_the_stored_account_snapshot_records_zero_orders(workflow, tmp_path: Path) -> None:
    """The capture boundary is read-only, and the record proves it."""
    _run_everything(workflow)
    snapshot = FilesystemAccountSnapshotRepository(tmp_path / "data" / "accounts").latest()

    assert snapshot is not None
    assert snapshot.orders_submitted == 0
    assert snapshot.simulated is True


def test_the_authorisation_is_not_an_order(workflow) -> None:
    """Milestone 7 ends at an authorisation boundary."""
    _, _, _, allocation_run = _run_everything(workflow)
    allocation = allocation_run.result.allocation("NVDA")

    assert allocation is not None
    for attribute in ("order_type", "limit_price", "side", "broker_order_id", "time_in_force"):
        assert not hasattr(allocation, attribute)


# ---------------------------------------------------------------------------
# 41. The boundary handed to Milestone 8
# ---------------------------------------------------------------------------
def test_an_approved_allocation_carries_everything_execution_needs(workflow) -> None:
    """M8 must not have to re-run research, strategy, contract or risk."""
    _, _, _, allocation_run = _run_everything(workflow)
    allocation = allocation_run.result.allocation("NVDA")

    assert allocation is not None
    assert allocation.campaign_id
    assert allocation.symbol == "NVDA"
    assert allocation.strategy is not None
    assert allocation.legs, "the legs to trade"
    assert all(leg.contract_id for leg in allocation.legs), "resolvable at the broker"
    assert all(leg.trading_class for leg in allocation.legs)
    assert allocation.quantity >= 1
    assert allocation.capital_committed > 0
    assert allocation.total_max_loss > 0
    assert allocation.allocation_id
    assert allocation.decided_at
    assert allocation.price_source is not None, "which figure priced it"
    assert allocation.input_snapshot_ids, "the data it rests on"


def test_the_milestone_one_projection_validates(workflow, load_schema) -> None:
    """The narrow ``allocation_decision`` boundary the rest of the chain uses."""
    from jsonschema import Draft202012Validator

    _, _, _, allocation_run = _run_everything(workflow)
    projected = allocation_run.result.to_allocation_decision()

    Draft202012Validator(
        load_schema("allocation_decision"),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(projected.model_dump(mode="json"))

    assert projected.allocated_eur + projected.reserve_eur == projected.total_budget_eur
    assert projected.entries[0].ticker == "NVDA"


# ---------------------------------------------------------------------------
# Determinism and idempotency across the whole chain
# ---------------------------------------------------------------------------
def test_the_allocation_result_is_deterministic(workflow) -> None:
    _, _, _, allocation_service = workflow
    _run_everything(workflow)

    first = allocation_service.run(dry_run=True).result
    second = allocation_service.run(dry_run=True).result

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_re_running_the_chain_does_not_authorise_twice(workflow) -> None:
    _, strategy_service, contract_service, allocation_service = workflow

    strategy_service.run()
    contract_service.select()
    first = allocation_service.run()
    second = allocation_service.run()

    assert first.result.allocated_this_run > Decimal("0")
    assert second.result.allocated_this_run == Decimal("0")
    repeat = second.result.allocation("NVDA")
    assert repeat is not None
    assert repeat.outcome is AllocationOutcome.ALREADY_ALLOCATED


def test_no_ai_decision_reaches_the_quantity(workflow) -> None:
    """The model said HIGH confidence. It changed the ordering, not the size.

    The quantity is the floor of ceilings that come entirely from
    configuration and from the campaign's own state. Nothing the agent
    returned appears in that arithmetic.
    """
    _, _, _, allocation_run = _run_everything(workflow)
    allocation = allocation_run.result.allocation("NVDA")

    assert allocation is not None
    assert allocation.risk_outcome is RiskOutcome.APPROVED
    calculation = allocation.calculation
    assert calculation is not None
    assert calculation.quantity == min(
        calculation.units_by_budget,
        calculation.units_by_risk,
        calculation.units_by_trade_cap,
        calculation.units_by_underlying_concentration,
        calculation.units_by_strategy_concentration,
        calculation.units_by_directional_exposure,
        calculation.units_by_contract_cap,
        calculation.units_by_buying_power
        if calculation.units_by_buying_power is not None
        else calculation.units_by_contract_cap,
    )
