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
    """Skip broker/LLM-touching tests unless explicitly unlocked."""
    import os

    if os.environ.get("ALLOW_LIVE_TESTS", "false").lower() == "true":
        return

    skip = pytest.mark.skip(
        reason="requires ALLOW_LIVE_TESTS=true; may reach a real broker or paid API"
    )
    for item in items:
        if {"live", "ibkr", "llm"} & set(item.keywords):
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
