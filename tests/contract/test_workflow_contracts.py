"""Workflow-boundary contract tests (specification section 25).

Two things are checked here that unit tests cannot cover:

* every stage's serialised output validates against its published JSON Schema,
  so model and schema cannot drift apart silently;
* each stage's output is actually *consumable* by the next stage — the ids
  line up, so the chain
  ``ResearchReport -> StrategyDecision -> PurchaseCard -> AllocationDecision ->
  RiskDecision -> OrderIntent -> ExecutionResult -> PositionSnapshot ->
  ExitDecision -> TradeSnapshot`` can be walked end to end.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from trading_system.domain.models import (
    AllocationDecision,
    DomainModel,
    ExecutionResult,
    ExitDecision,
    OrderIntent,
    PositionSnapshot,
    PurchaseCard,
    ResearchReport,
    RiskDecision,
    StrategyDecision,
    TradeSnapshot,
)

#: fixture name -> schema name
BOUNDARIES = {
    "universe_selection": "universe_selection",
    "research_report": "research_report",
    "strategy_decision": "strategy_decision",
    "purchase_card": "purchase_card",
    "allocation_decision": "allocation_decision",
    "risk_decision": "risk_decision",
    "order_intent": "order_intent",
    "execution_result": "execution_result",
    "position_snapshot": "position_snapshot",
    "exit_decision": "exit_decision",
    "trade_snapshot": "trade_snapshot",
}


def _validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _dump(model: DomainModel) -> dict[str, Any]:
    payload: dict[str, Any] = model.model_dump(mode="json")
    return payload


# ---------------------------------------------------------------------------
# Model output validates against the published schema
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.parametrize("fixture_name", sorted(BOUNDARIES))
def test_stage_output_validates_against_its_schema(
    fixture_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    model = request.getfixturevalue(fixture_name)
    schema = load_schema(BOUNDARIES[fixture_name])
    _validator(schema).validate(_dump(model))


@pytest.mark.contract
@pytest.mark.parametrize("fixture_name", sorted(BOUNDARIES))
def test_schema_rejects_unknown_fields(
    fixture_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    """Proves the schema actually constrains, rather than accepting anything."""
    payload = _dump(request.getfixturevalue(fixture_name))
    payload["smuggled_field"] = "surprise"

    with pytest.raises(ValidationError):
        _validator(load_schema(BOUNDARIES[fixture_name])).validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize("fixture_name", sorted(BOUNDARIES))
def test_schema_requires_its_required_fields(
    fixture_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    schema = load_schema(BOUNDARIES[fixture_name])
    payload = _dump(request.getfixturevalue(fixture_name))

    for field in schema["required"]:
        broken = copy.deepcopy(payload)
        del broken[field]
        with pytest.raises(ValidationError):
            _validator(schema).validate(broken)


# ---------------------------------------------------------------------------
# Decimal safety survives serialisation
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_money_serialises_as_an_exact_string(purchase_card: PurchaseCard) -> None:
    payload = _dump(purchase_card)
    assert payload["requested_allocation_eur"] == "1200.00"
    assert isinstance(payload["contract"]["legs"][0]["strike"], str)


@pytest.mark.contract
def test_schema_rejects_money_as_a_json_number(
    purchase_card: PurchaseCard, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(purchase_card)
    payload["requested_allocation_eur"] = 1200.00

    with pytest.raises(ValidationError):
        _validator(load_schema("purchase_card")).validate(payload)


@pytest.mark.contract
def test_timestamps_serialise_as_utc(research_report: ResearchReport) -> None:
    assert _dump(research_report)["as_of"].endswith("Z")


# ---------------------------------------------------------------------------
# Conditional invariants agree between model and schema
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_schema_rejects_no_trade_carrying_a_strategy(
    strategy_decision: StrategyDecision, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(strategy_decision)
    payload["action"] = "NO_TRADE"  # strategy_type is still LONG_CALL

    with pytest.raises(ValidationError):
        _validator(load_schema("strategy_decision")).validate(payload)


@pytest.mark.contract
def test_schema_rejects_buy_without_a_strategy(
    strategy_decision: StrategyDecision, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(strategy_decision)
    payload["strategy_type"] = None

    with pytest.raises(ValidationError):
        _validator(load_schema("strategy_decision")).validate(payload)


@pytest.mark.contract
def test_schema_rejects_approval_with_a_rejection_code(
    risk_decision: RiskDecision, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(risk_decision)
    payload["reason_codes"] = ["SPREAD_TOO_WIDE"]

    with pytest.raises(ValidationError):
        _validator(load_schema("risk_decision")).validate(payload)


@pytest.mark.contract
def test_schema_rejects_rejection_claiming_ok(
    risk_decision: RiskDecision, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(risk_decision)
    payload["outcome"] = "REJECTED"

    with pytest.raises(ValidationError):
        _validator(load_schema("risk_decision")).validate(payload)


@pytest.mark.contract
def test_schema_rejects_limit_order_without_a_price(
    order_intent: OrderIntent, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(order_intent)
    payload["limit_price"] = None

    with pytest.raises(ValidationError):
        _validator(load_schema("order_intent")).validate(payload)


@pytest.mark.contract
def test_schema_rejects_sell_without_a_reason(
    exit_decision: ExitDecision, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(exit_decision)
    payload["reason"] = None

    with pytest.raises(ValidationError):
        _validator(load_schema("exit_decision")).validate(payload)


# ---------------------------------------------------------------------------
# Producer output is consumable by the next stage
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_a_universe_run_feeds_the_research_stage(
    universe_run_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Milestone 4 -> Milestone 5.

    The full run record is the audit artifact; the researcher consumes the
    narrow ``UniverseSelection`` boundary. This asserts the projection actually
    validates against the schema the next stage was built against, rather than
    merely being convertible.
    """
    selection = universe_run_result.to_universe_selection()

    _validator(load_schema("universe_selection")).validate(_dump(selection))
    assert selection.universe_id == universe_run_result.snapshot_id
    assert [c.ticker for c in selection.candidates] == universe_run_result.symbols


@pytest.mark.contract
def test_the_universe_boundary_carries_only_underlyings(universe_run_result) -> None:
    """No strike, no expiry, no strategy crosses into research."""
    payload = _dump(universe_run_result.to_universe_selection())

    for candidate in payload["candidates"]:
        assert set(candidate) <= {"ticker", "rank", "selection_score", "rationale"}


@pytest.mark.contract
def test_a_failed_universe_run_produces_no_research_candidates(
    universe_run_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """A run that did not produce a universe must hand the next stage nothing."""
    from trading_system.domain.enums import UniverseSelectionStatus

    failed = universe_run_result.model_copy(
        update={
            "status": UniverseSelectionStatus.AI_UNAVAILABLE,
            "selected_assets": [],
            "selection_method": universe_run_result.selection_method,
            "agent_metadata": universe_run_result.agent_metadata,
        }
    )
    selection = failed.to_universe_selection()

    assert selection.candidates == []
    _validator(load_schema("universe_selection")).validate(_dump(selection))


@pytest.mark.contract
def test_the_universe_result_validates_against_its_own_schema(
    universe_run_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    _validator(load_schema("universe_selection_result")).validate(_dump(universe_run_result))


@pytest.mark.contract
def test_research_feeds_strategy(
    research_report: ResearchReport, strategy_decision: StrategyDecision
) -> None:
    assert strategy_decision.research_report_id == research_report.report_id
    assert strategy_decision.ticker == research_report.ticker


@pytest.mark.contract
def test_strategy_and_research_feed_the_purchase_card(
    research_report: ResearchReport,
    strategy_decision: StrategyDecision,
    purchase_card: PurchaseCard,
) -> None:
    assert purchase_card.research_report_id == research_report.report_id
    assert purchase_card.strategy_decision_id == strategy_decision.decision_id
    assert purchase_card.strategy_type == strategy_decision.strategy_type
    assert purchase_card.underlying == research_report.ticker
    # The card must not quietly restate the thesis differently.
    assert purchase_card.hypothesis == research_report.hypothesis
    assert purchase_card.confidence == research_report.confidence


@pytest.mark.contract
def test_purchase_card_contract_matches_its_strategy(purchase_card: PurchaseCard) -> None:
    assert purchase_card.contract.strategy_type == purchase_card.strategy_type
    assert purchase_card.contract.underlying == purchase_card.underlying


@pytest.mark.contract
def test_allocation_covers_the_purchase_card(
    purchase_card: PurchaseCard, allocation_decision: AllocationDecision
) -> None:
    entry = next(
        e for e in allocation_decision.entries if e.opportunity_id == purchase_card.card_id
    )
    assert entry.allocated_eur == purchase_card.requested_allocation_eur


@pytest.mark.contract
def test_risk_decision_refers_to_the_purchase_card(
    purchase_card: PurchaseCard, risk_decision: RiskDecision
) -> None:
    assert risk_decision.purchase_card_id == purchase_card.card_id


@pytest.mark.contract
def test_order_intent_descends_from_an_approved_risk_decision(
    purchase_card: PurchaseCard, risk_decision: RiskDecision, order_intent: OrderIntent
) -> None:
    assert risk_decision.outcome.value == "APPROVED"
    assert order_intent.risk_decision_id == risk_decision.decision_id
    assert order_intent.purchase_card_id == purchase_card.card_id
    assert order_intent.trading_mode == risk_decision.trading_mode


@pytest.mark.contract
def test_order_intent_legs_match_the_purchase_card(
    purchase_card: PurchaseCard, order_intent: OrderIntent
) -> None:
    assert list(order_intent.legs) == list(purchase_card.contract.legs)
    assert order_intent.quantity == purchase_card.quantity


@pytest.mark.contract
def test_execution_result_answers_the_intent(
    order_intent: OrderIntent, execution_result: ExecutionResult
) -> None:
    assert execution_result.intent_id == order_intent.intent_id
    assert execution_result.filled_quantity <= order_intent.quantity
    assert execution_result.trading_mode == order_intent.trading_mode


@pytest.mark.contract
def test_position_snapshot_traces_back_to_the_card(
    purchase_card: PurchaseCard, position_snapshot: PositionSnapshot
) -> None:
    assert position_snapshot.purchase_card_id == purchase_card.card_id
    assert position_snapshot.strategy_type == purchase_card.strategy_type


@pytest.mark.contract
def test_exit_decision_refers_to_the_position(
    position_snapshot: PositionSnapshot, exit_decision: ExitDecision
) -> None:
    assert exit_decision.position_id == position_snapshot.position_id


@pytest.mark.contract
def test_trade_snapshot_closes_the_loop(
    research_report: ResearchReport,
    strategy_decision: StrategyDecision,
    purchase_card: PurchaseCard,
    allocation_decision: AllocationDecision,
    risk_decision: RiskDecision,
    order_intent: OrderIntent,
    exit_decision: ExitDecision,
    trade_snapshot: TradeSnapshot,
) -> None:
    """Every upstream artifact must be reachable from the final record."""
    assert trade_snapshot.research_report_id == research_report.report_id
    assert trade_snapshot.strategy_decision_id == strategy_decision.decision_id
    assert trade_snapshot.purchase_card_id == purchase_card.card_id
    assert trade_snapshot.allocation_id == allocation_decision.allocation_id
    assert trade_snapshot.risk_decision_id == risk_decision.decision_id
    assert trade_snapshot.order_intent_id == order_intent.intent_id
    assert trade_snapshot.exit_decision_id == exit_decision.decision_id
    assert trade_snapshot.exit_reason == exit_decision.reason


@pytest.mark.contract
def test_every_artifact_is_version_stamped(
    request: pytest.FixtureRequest,
) -> None:
    """Comparing strategy versions later requires the stamp to exist now."""
    for fixture_name in BOUNDARIES:
        model = request.getfixturevalue(fixture_name)
        versions = getattr(model, "versions", None)
        if versions is None:
            # ExecutionResult and PositionSnapshot record broker reality; their
            # provenance is the broker field, not an agent/config version.
            assert fixture_name in {"execution_result", "position_snapshot"}
            continue
        assert versions.application_version
        assert versions.config_version


@pytest.mark.contract
def test_broker_sourced_artifacts_record_their_origin(
    execution_result: ExecutionResult, position_snapshot: PositionSnapshot
) -> None:
    """Broker state is authoritative, so which broker said so must be recorded."""
    assert execution_result.broker
    assert position_snapshot.source


@pytest.mark.contract
def test_execution_result_counts_submitted_orders(execution_result: ExecutionResult) -> None:
    """Read-only broker tests assert this is zero; it must therefore be tracked."""
    assert execution_result.orders_submitted == 1
    assert "orders_submitted" in _dump(execution_result)
