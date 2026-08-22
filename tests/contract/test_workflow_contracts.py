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
from decimal import Decimal
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
    assert payload["requested_allocation"] == "1200.00"
    assert payload["currency"] == "USD", "the currency is stated, never read off a field name"
    assert isinstance(payload["contract"]["legs"][0]["strike"], str)


@pytest.mark.contract
def test_schema_rejects_money_as_a_json_number(
    purchase_card: PurchaseCard, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(purchase_card)
    payload["requested_allocation"] = 1200.00

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
@pytest.mark.parametrize(
    ("schema_name", "definition"),
    [
        ("universe_selection_input", "candidate_asset"),
        ("universe_selection_result", "selected_asset"),
    ],
)
def test_both_volume_fields_are_declared_in_the_universe_schemas(
    schema_name: str, definition: str, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """The two volumes must stay two, in the schema as well as in the model.

    Both schemas set ``additionalProperties: false``, so a field added to the
    model and not to the schema fails validation loudly. The reverse — a schema
    that quietly drops one of them, or that merges them back into a single
    ``volume`` — would not, and that is the drift this pins: the session figure
    and the 90-day average answer different questions, and only one of them is
    the liquidity floor's input.
    """
    properties = load_schema(schema_name)["$defs"][definition]["properties"]

    assert "underlying_volume" in properties
    assert "average_daily_volume" in properties


@pytest.mark.contract
@pytest.mark.parametrize(
    ("schema_name", "definition"),
    [
        ("universe_selection_input", "candidate_asset"),
        ("universe_selection_result", "selected_asset"),
    ],
)
def test_the_universe_schemas_match_their_models_on_volume(
    schema_name: str, definition: str, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Neither side may grow a volume field the other does not know about."""
    from trading_system.universe.models import CandidateAsset, SelectedAsset

    model = CandidateAsset if definition == "candidate_asset" else SelectedAsset
    properties = set(load_schema(schema_name)["$defs"][definition]["properties"])
    fields = set(model.model_fields)

    assert {f for f in fields if "volume" in f} == {p for p in properties if "volume" in p}


@pytest.mark.contract
def test_a_research_run_validates_against_its_own_schema(
    market_research_run, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    _validator(load_schema("research_run")).validate(_dump(market_research_run))


@pytest.mark.contract
def test_a_research_report_validates_against_its_own_schema(
    market_research_report, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    _validator(load_schema("market_research_report")).validate(_dump(market_research_report))


@pytest.mark.contract
def test_a_research_input_validates_against_its_own_schema(
    market_research_input, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    _validator(load_schema("research_input")).validate(_dump(market_research_input))


@pytest.mark.contract
def test_an_agent_output_validates_against_its_own_schema(
    market_research_agent_output, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    _validator(load_schema("research_agent_output")).validate(_dump(market_research_agent_output))


@pytest.mark.contract
def test_the_agent_output_schema_rejects_e_without_an_explanation(
    market_research_agent_output, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(market_research_agent_output)
    payload["hypothesis"] = "E"
    payload["direction"] = "NEUTRAL"
    payload["explanation"] = None

    with pytest.raises(ValidationError):
        _validator(load_schema("research_agent_output")).validate(payload)


@pytest.mark.contract
def test_the_agent_output_schema_rejects_d_without_an_event(
    market_research_agent_output, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Milestone 5 brief section 13: D without an event is invalid."""
    payload = _dump(market_research_agent_output)
    payload["hypothesis"] = "D"
    payload["direction"] = "UNCERTAIN"
    payload["key_events"] = []

    with pytest.raises(ValidationError):
        _validator(load_schema("research_agent_output")).validate(payload)


@pytest.mark.contract
def test_the_agent_output_schema_rejects_a_direction_contradicting_the_hypothesis(
    market_research_agent_output, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(market_research_agent_output)
    payload["direction"] = "BEARISH"  # hypothesis is still B

    with pytest.raises(ValidationError):
        _validator(load_schema("research_agent_output")).validate(payload)


@pytest.mark.contract
def test_the_report_schema_rejects_a_failed_report_carrying_an_outlook(
    market_research_report, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """A failure is never a market view, in the schema as well as the model."""
    payload = _dump(market_research_report)
    payload["status"] = "AI_UNAVAILABLE"  # hypothesis and confidence are still set

    with pytest.raises(ValidationError):
        _validator(load_schema("market_research_report")).validate(payload)


@pytest.mark.contract
def test_the_report_schema_requires_an_invalidation_condition_on_success(
    market_research_report, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(market_research_report)
    payload["invalidation_conditions"] = []

    with pytest.raises(ValidationError):
        _validator(load_schema("market_research_report")).validate(payload)


@pytest.mark.contract
def test_a_universe_run_feeds_the_research_input(
    universe_run_result, market_research_input
) -> None:
    """Milestone 4 -> Milestone 5.

    The research input names the universe run and snapshot it descends from, so
    the chain universe -> research input -> research report is walkable in both
    directions after the fact.
    """
    assert market_research_input.universe_run_id == universe_run_result.run_id
    assert market_research_input.universe_snapshot_id == universe_run_result.snapshot_id
    assert market_research_input.symbol in universe_run_result.symbols


@pytest.mark.contract
def test_the_research_input_carries_only_underlying_level_evidence(
    market_research_input,
) -> None:
    """No strike, no expiry, no right crosses into the agent's view."""
    payload = _dump(market_research_input)
    option_context = payload.get("option_context") or {}

    for point in option_context.get("term_structure", []):
        assert set(point) <= {
            "days_to_expiration",
            "atm_implied_volatility",
            "contract_count",
            "total_volume",
            "total_open_interest",
        }


@pytest.mark.contract
def test_a_research_report_projects_onto_the_strategy_boundary(
    market_research_report, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Milestone 5 -> Milestone 6.

    The full report is the audit artifact; the strategy stage consumes the
    narrow ``ResearchReport`` boundary. This asserts the projection actually
    validates against the schema that stage was built against, rather than
    merely being convertible.
    """
    projected = market_research_report.to_research_report()

    _validator(load_schema("research_report")).validate(_dump(projected))
    assert projected.report_id == market_research_report.report_id
    assert projected.ticker == market_research_report.symbol
    assert projected.hypothesis == market_research_report.hypothesis
    assert projected.invalidation_conditions


@pytest.mark.contract
def test_a_failed_research_report_cannot_cross_the_boundary(market_research_report) -> None:
    """There is no way to hand a failed run downstream as though it were a view."""
    from trading_system.domain.enums import ResearchStatus

    failed = market_research_report.model_copy(
        update={
            "status": ResearchStatus.AI_UNAVAILABLE,
            "hypothesis": None,
            "confidence": None,
            "direction": None,
            "expected_magnitude": None,
            "thesis": None,
            "expected_behavior": None,
            "bullish_catalysts": [],
            "bearish_catalysts": [],
        }
    )

    with pytest.raises(ValueError, match="nothing to hand on"):
        failed.to_research_report()


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
    assert entry.allocated == purchase_card.requested_allocation
    assert allocation_decision.currency == purchase_card.currency


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
def test_a_strategy_decision_validates_against_its_own_schema(
    strategy_decision_record, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    _validator(load_schema("strategy_selection")).validate(_dump(strategy_decision_record))


@pytest.mark.contract
def test_a_contract_selection_validates_against_its_own_schema(
    contract_selection_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    _validator(load_schema("contract_selection")).validate(_dump(contract_selection_result))


@pytest.mark.contract
def test_the_strategy_schema_rejects_a_failed_decision_naming_a_strategy(
    strategy_decision_record, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """A failure is never a trade proposal."""
    payload = _dump(strategy_decision_record)
    payload["status"] = "AI_UNAVAILABLE"

    with pytest.raises(ValidationError):
        _validator(load_schema("strategy_selection")).validate(payload)


@pytest.mark.contract
def test_the_strategy_schema_rejects_no_trade_carrying_a_strategy(
    strategy_decision_record, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(strategy_decision_record)
    payload["action"] = "NO_TRADE"

    with pytest.raises(ValidationError):
        _validator(load_schema("strategy_selection")).validate(payload)


@pytest.mark.contract
def test_the_contract_schema_rejects_a_failed_selection_carrying_legs(
    contract_selection_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(contract_selection_result)
    payload["selection_status"] = "NO_VALID_STRIKE"

    with pytest.raises(ValidationError):
        _validator(load_schema("contract_selection")).validate(payload)


@pytest.mark.contract
def test_the_contract_schema_requires_a_broker_contract_id_on_every_leg(
    contract_selection_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(contract_selection_result)
    del payload["legs"][0]["contract_id"]

    with pytest.raises(ValidationError):
        _validator(load_schema("contract_selection")).validate(payload)


@pytest.mark.contract
def test_the_contract_schema_rejects_a_cost_with_no_figure_and_no_reason(
    contract_selection_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """An unavailable cost says why; an available one states a number."""
    payload = _dump(contract_selection_result)
    payload["cost"] = {"available": False, "estimated_debit": None, "unavailable_reason": None}

    with pytest.raises(ValidationError):
        _validator(load_schema("contract_selection")).validate(payload)


@pytest.mark.contract
def test_a_research_report_feeds_the_strategy_decision(
    market_research_report, strategy_decision_record
) -> None:
    """Milestone 5 -> Milestone 6: the decision names the outlook it rests on."""
    assert strategy_decision_record.research_report_id == market_research_report.report_id
    assert strategy_decision_record.hypothesis == market_research_report.hypothesis
    assert strategy_decision_record.symbol == market_research_report.symbol


@pytest.mark.contract
def test_a_strategy_decision_feeds_the_contract_selection(
    strategy_decision_record, contract_selection_result
) -> None:
    """The two halves of Milestone 6, connected by identifier."""
    assert contract_selection_result.strategy_decision_id == (strategy_decision_record.decision_id)
    assert contract_selection_result.strategy == strategy_decision_record.selected_strategy
    assert contract_selection_result.symbol == strategy_decision_record.symbol
    assert contract_selection_result.research_report_id == (
        strategy_decision_record.research_report_id
    )


@pytest.mark.contract
def test_a_strategy_decision_projects_onto_the_milestone_1_boundary(
    strategy_decision_record, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    projected = strategy_decision_record.to_strategy_decision()

    _validator(load_schema("strategy_decision")).validate(_dump(projected))
    assert projected.strategy_type == strategy_decision_record.selected_strategy


@pytest.mark.contract
def test_a_contract_selection_projects_onto_the_purchase_card_boundary(
    contract_selection_result, purchase_card, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Milestone 6 -> Milestone 7: the legs a purchase card would carry."""
    projected = contract_selection_result.to_contract_selection()
    payload = _dump(purchase_card)
    payload["contract"] = _dump(projected)

    _validator(load_schema("purchase_card")).validate(payload)
    assert projected.underlying == contract_selection_result.symbol
    assert len(projected.legs) == len(contract_selection_result.legs)


@pytest.mark.contract
def test_the_milestone_6_artifacts_carry_no_position_size(
    strategy_decision_record, contract_selection_result
) -> None:
    """How much is the risk and allocation engines' answer, not this stage's."""
    for payload in (_dump(strategy_decision_record), _dump(contract_selection_result)):
        serialised = str(payload)
        for forbidden in ("quantity", "allocated", "requested_allocation", "buying_power"):
            assert forbidden not in serialised


@pytest.mark.contract
def test_selected_legs_serialise_money_as_exact_strings(contract_selection_result) -> None:
    payload = _dump(contract_selection_result)

    leg = payload["legs"][0]
    assert isinstance(leg["strike"], str)
    assert isinstance(leg["bid"], str)
    assert isinstance(payload["cost"]["estimated_debit"], str)


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


# ---------------------------------------------------------------------------
# Milestone 7: allocation and risk
#
# The audit artifacts of the allocation stage, and the boundary they hand to
# execution. `allocation_decision` and `risk_decision` above stay the narrow
# Milestone 1 contracts; `campaign_allocation` is the wider record that
# projects onto the first, exactly as `market_research_report` does for
# `research_report`.
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.parametrize(
    ("fixture_name", "schema_name"),
    [
        ("account_snapshot", "account_snapshot"),
        ("campaign_snapshot", "campaign_snapshot"),
        ("allocation_candidate", "allocation_candidate"),
        ("risk_evaluation", "risk_evaluation"),
        ("campaign_allocation", "campaign_allocation"),
        ("allocation_run", "allocation_run"),
    ],
)
def test_a_milestone_7_artifact_validates_against_its_own_schema(
    fixture_name: str,
    schema_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    model = request.getfixturevalue(fixture_name)
    _validator(load_schema(schema_name)).validate(_dump(model))


@pytest.mark.contract
@pytest.mark.parametrize(
    ("fixture_name", "schema_name"),
    [
        ("account_snapshot", "account_snapshot"),
        ("campaign_snapshot", "campaign_snapshot"),
        ("allocation_candidate", "allocation_candidate"),
        ("risk_evaluation", "risk_evaluation"),
        ("campaign_allocation", "campaign_allocation"),
        ("allocation_run", "allocation_run"),
    ],
)
def test_a_milestone_7_schema_rejects_unknown_fields(
    fixture_name: str,
    schema_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    payload = _dump(request.getfixturevalue(fixture_name))
    payload["smuggled_field"] = "surprise"

    with pytest.raises(ValidationError):
        _validator(load_schema(schema_name)).validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("fixture_name", "schema_name"),
    [
        ("account_snapshot", "account_snapshot"),
        ("campaign_snapshot", "campaign_snapshot"),
        ("allocation_candidate", "allocation_candidate"),
        ("risk_evaluation", "risk_evaluation"),
        ("campaign_allocation", "campaign_allocation"),
        ("allocation_run", "allocation_run"),
    ],
)
def test_a_milestone_7_schema_requires_its_required_fields(
    fixture_name: str,
    schema_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    schema = load_schema(schema_name)
    payload = _dump(request.getfixturevalue(fixture_name))

    for field in schema["required"]:
        broken = copy.deepcopy(payload)
        del broken[field]
        with pytest.raises(ValidationError):
            _validator(schema).validate(broken)


@pytest.mark.contract
def test_the_allocation_schema_rejects_money_as_a_json_number(
    campaign_allocation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(campaign_allocation)
    payload["capital_committed"] = 1210.00

    with pytest.raises(ValidationError):
        _validator(load_schema("campaign_allocation")).validate(payload)


@pytest.mark.contract
def test_the_allocation_schema_rejects_a_timestamp_of_the_wrong_type(
    campaign_allocation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """The type constraint, which the schema enforces on its own.

    Deliberately not a malformed *string*: ``format: date-time`` is only
    enforced by ``jsonschema`` when an optional RFC 3339 validator is
    installed, and this project does not depend on one. Asserting a check that
    is silently inactive would be worse than not asserting it — the model's
    ``UtcDatetime`` is what actually rejects an unparseable instant, and
    ``tests/unit/test_domain_models.py`` covers that.
    """
    payload = _dump(campaign_allocation)
    payload["decided_at"] = 1_754_000_000

    with pytest.raises(ValidationError):
        _validator(load_schema("campaign_allocation")).validate(payload)


@pytest.mark.contract
def test_the_allocation_schema_rejects_an_invalid_enum(
    campaign_allocation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(campaign_allocation)
    payload["outcome"] = "PROBABLY"

    with pytest.raises(ValidationError):
        _validator(load_schema("campaign_allocation")).validate(payload)


@pytest.mark.contract
def test_the_allocation_schema_rejects_a_refusal_that_commits_capital(
    campaign_allocation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Only an approval commits capital, in the schema as well as the model."""
    payload = _dump(campaign_allocation)
    payload["outcome"] = "NO_TRADE"  # quantity and capital are still set

    with pytest.raises(ValidationError):
        _validator(load_schema("campaign_allocation")).validate(payload)


@pytest.mark.contract
def test_the_allocation_schema_rejects_an_approval_over_a_risk_rejection(
    campaign_allocation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """No layer may override the risk engine."""
    payload = _dump(campaign_allocation)
    payload["risk_outcome"] = "REJECTED"

    with pytest.raises(ValidationError):
        _validator(load_schema("campaign_allocation")).validate(payload)


@pytest.mark.contract
def test_the_risk_schema_rejects_an_approval_with_a_rejection_code(
    risk_evaluation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(risk_evaluation)
    payload["reason_codes"] = ["SPREAD_TOO_WIDE"]

    with pytest.raises(ValidationError):
        _validator(load_schema("risk_evaluation")).validate(payload)


@pytest.mark.contract
def test_the_risk_schema_requires_a_reason_on_a_failed_check(
    risk_evaluation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Every rejection must be machine-readable."""
    payload = _dump(risk_evaluation)
    payload["outcome"] = "REJECTED"
    payload["reason_codes"] = ["SPREAD_TOO_WIDE"]
    payload["checks"] = [
        {"name": "bid_ask_spread", "scope": "STRATEGY", "outcome": "FAIL", "reason_code": None}
    ]

    with pytest.raises(ValidationError):
        _validator(load_schema("risk_evaluation")).validate(payload)


@pytest.mark.contract
def test_the_account_schema_rejects_a_snapshot_claiming_a_submitted_order(
    account_snapshot, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Capturing an account reads and does nothing else."""
    payload = _dump(account_snapshot)
    payload["orders_submitted"] = 1

    with pytest.raises(ValidationError):
        _validator(load_schema("account_snapshot")).validate(payload)


@pytest.mark.contract
def test_the_candidate_schema_requires_a_broker_contract_id_on_every_leg(
    allocation_candidate, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(allocation_candidate)
    del payload["legs"][0]["contract_id"]

    with pytest.raises(ValidationError):
        _validator(load_schema("allocation_candidate")).validate(payload)


@pytest.mark.contract
def test_the_candidate_schema_rejects_a_price_with_no_figure_and_no_reason(
    allocation_candidate, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """An unavailable price says why; an available one states a number."""
    payload = _dump(allocation_candidate)
    payload["price"] = {"available": False, "source": None, "unit_cost": None}

    with pytest.raises(ValidationError):
        _validator(load_schema("allocation_candidate")).validate(payload)


# ---------------------------------------------------------------------------
# Producer output is consumable by the next stage
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_a_contract_selection_feeds_the_allocation_candidate(
    contract_selection_result, allocation_candidate
) -> None:
    """Milestone 6 -> Milestone 7.

    The candidate names the selection it descends from and carries the unit
    cost that selection established. Nothing is upgraded on the way through.
    """
    assert allocation_candidate.contract_selection_id == contract_selection_result.selection_id
    assert allocation_candidate.symbol == contract_selection_result.symbol
    assert allocation_candidate.strategy == contract_selection_result.strategy
    assert contract_selection_result.cost is not None
    assert allocation_candidate.price.unit_cost == contract_selection_result.cost.estimated_debit


@pytest.mark.contract
def test_the_candidate_carries_no_quantity_or_allocation(allocation_candidate) -> None:
    """Quantity is introduced by the allocation engine, not before it."""
    payload = _dump(allocation_candidate)

    for field in ("quantity", "capital_committed", "allocated", "budget"):
        assert field not in payload


@pytest.mark.contract
def test_a_risk_evaluation_answers_the_candidate(allocation_candidate, risk_evaluation) -> None:
    assert risk_evaluation.opportunity_id == allocation_candidate.opportunity_id
    assert risk_evaluation.symbol == allocation_candidate.symbol
    assert risk_evaluation.unit_cost == allocation_candidate.price.unit_cost


@pytest.mark.contract
def test_an_allocation_descends_from_its_risk_evaluation(
    campaign_allocation, risk_evaluation
) -> None:
    assert campaign_allocation.risk_evaluation.opportunity_id == risk_evaluation.opportunity_id
    assert campaign_allocation.risk_outcome.value == "APPROVED"
    assert campaign_allocation.quantity >= 1


@pytest.mark.contract
def test_an_allocation_run_projects_onto_the_milestone_1_boundary(
    allocation_run, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Milestone 7 -> the narrow ``allocation_decision`` contract.

    The full run is the audit artifact; this is what the rest of the chain was
    built against. ``allocated + reserve == budget`` has to hold exactly.
    """
    projected = allocation_run.to_allocation_decision()

    _validator(load_schema("allocation_decision")).validate(_dump(projected))
    assert projected.campaign_id == allocation_run.campaign_id
    assert projected.total_budget == allocation_run.budget
    assert projected.allocated + projected.reserve == projected.total_budget
    # The narrow boundary carries what a downstream stage has to spend, in the
    # currency it spends it in. The declared original and the rate stay on the
    # run, which is the audit artifact.
    assert projected.currency == allocation_run.currency == "USD"


@pytest.mark.contract
def test_the_allocation_arithmetic_survives_serialisation(campaign_allocation) -> None:
    """Money crosses the boundary as an exact string, never as a float."""
    payload = _dump(campaign_allocation)

    assert isinstance(payload["capital_committed"], str)
    assert isinstance(payload["unit_cost"], str)
    assert Decimal(payload["capital_committed"]) == Decimal(payload["unit_cost"]) * Decimal(
        payload["quantity"]
    )
    assert Decimal(payload["total_max_loss"]) == Decimal(payload["unit_max_loss"]) * Decimal(
        payload["quantity"]
    )


@pytest.mark.contract
def test_the_allocation_is_an_authorisation_and_not_an_order(campaign_allocation) -> None:
    """Milestone 7 ends at an authorisation boundary (brief section 42)."""
    payload = _dump(campaign_allocation)

    for field in (
        "order_type",
        "side",
        "limit_price",
        "time_in_force",
        "broker_order_id",
        "execution_price",
    ):
        assert field not in payload


@pytest.mark.contract
def test_the_allocation_names_every_upstream_artifact(campaign_allocation) -> None:
    """The chain has to be walkable by id, in both directions."""
    payload = _dump(campaign_allocation)

    for field in (
        "contract_selection_id",
        "contract_run_id",
        "strategy_decision_id",
        "research_report_id",
        "account_snapshot_id",
        "campaign_id",
        "opportunity_id",
        "allocation_id",
    ):
        assert payload[field], f"{field} is empty"


# ---------------------------------------------------------------------------
# Milestone 9: positions, reservations and reconciliation
#
# The milestone stores two position records deliberately kept apart — what the
# broker reported, and what confirmed fills say should exist — plus the capital
# ledger and the comparison between them. Each has its own schema, and the
# narrow Milestone 1 shapes (``position_snapshot``, ``ReconciliationReport``)
# are projected from them rather than replaced by them.
# ---------------------------------------------------------------------------
_MILESTONE_9_ARTIFACTS = [
    ("broker_position_snapshot", "broker_position_snapshot"),
    ("expected_position", "expected_position"),
    ("position_fill", "position_fill"),
    ("reservation", "reservation"),
    ("reservation_event", "reservation_event"),
    ("reconciliation_result", "reconciliation_result"),
    ("reconciliation_event", "reconciliation_event"),
]


@pytest.mark.contract
@pytest.mark.parametrize(("fixture_name", "schema_name"), _MILESTONE_9_ARTIFACTS)
def test_a_milestone_9_artifact_validates_against_its_own_schema(
    fixture_name: str,
    schema_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    model = request.getfixturevalue(fixture_name)
    _validator(load_schema(schema_name)).validate(_dump(model))


@pytest.mark.contract
@pytest.mark.parametrize(("fixture_name", "schema_name"), _MILESTONE_9_ARTIFACTS)
def test_a_milestone_9_schema_rejects_unknown_fields(
    fixture_name: str,
    schema_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    payload = _dump(request.getfixturevalue(fixture_name))
    payload["smuggled_field"] = "surprise"

    with pytest.raises(ValidationError):
        _validator(load_schema(schema_name)).validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize(("fixture_name", "schema_name"), _MILESTONE_9_ARTIFACTS)
def test_a_milestone_9_schema_requires_its_required_fields(
    fixture_name: str,
    schema_name: str,
    request: pytest.FixtureRequest,
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    schema = load_schema(schema_name)
    payload = _dump(request.getfixturevalue(fixture_name))

    for field in schema["required"]:
        broken = copy.deepcopy(payload)
        del broken[field]
        with pytest.raises(ValidationError):
            _validator(schema).validate(broken)


@pytest.mark.contract
def test_a_reconciliation_schema_refuses_a_submitted_order(
    reconciliation_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Structural, in the schema as well as in the model: it cannot have traded."""
    payload = _dump(reconciliation_result)
    payload["orders_submitted"] = 1

    with pytest.raises(ValidationError):
        _validator(load_schema("reconciliation_result")).validate(payload)


@pytest.mark.contract
def test_a_reconciliation_schema_refuses_a_corrective_order(
    reconciliation_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(reconciliation_result)
    payload["corrective_orders"] = 1

    with pytest.raises(ValidationError):
        _validator(load_schema("reconciliation_result")).validate(payload)


@pytest.mark.contract
def test_the_snapshot_schema_refuses_a_failed_read_carrying_positions(
    broker_position_snapshot, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """ "We could not look" must not be expressible as "here is what it holds"."""
    payload = _dump(broker_position_snapshot)
    payload["read_status"] = "UNAVAILABLE"
    payload["detail"] = "gateway down"

    with pytest.raises(ValidationError):
        _validator(load_schema("broker_position_snapshot")).validate(payload)


@pytest.mark.contract
def test_the_snapshot_schema_refuses_a_match_over_an_unread_broker(
    reconciliation_result, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(reconciliation_result)
    assert payload["status"] == "MATCH"
    payload["positions_read"] = "UNAVAILABLE"

    with pytest.raises(ValidationError):
        _validator(load_schema("reconciliation_result")).validate(payload)


@pytest.mark.contract
def test_the_reservation_schema_requires_a_reason_for_an_unknown(
    reservation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """An operator must be able to filter for exactly the locked capital."""
    payload = _dump(reservation)
    payload["state"] = "UNKNOWN"
    payload["reason_codes"] = ["AUTHORIZED"]

    with pytest.raises(ValidationError):
        _validator(load_schema("reservation")).validate(payload)


@pytest.mark.contract
def test_the_reservation_schema_requires_evidence_for_an_overrun(
    reservation, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    payload = _dump(reservation)
    payload["over_authorized_amount"] = "100.00"

    with pytest.raises(ValidationError):
        _validator(load_schema("reservation")).validate(payload)


@pytest.mark.contract
def test_reservation_money_crosses_the_boundary_as_exact_strings(reservation) -> None:
    payload = _dump(reservation)

    for field in ("authorized_amount", "consumed_amount", "released_amount", "remaining_amount"):
        assert isinstance(payload[field], str)
    assert Decimal(payload["consumed_amount"]) + Decimal(payload["released_amount"]) + Decimal(
        payload["remaining_amount"]
    ) == Decimal(payload["authorized_amount"])


@pytest.mark.contract
def test_a_reconciliation_projects_onto_the_milestone_1_report(reconciliation_result) -> None:
    """Milestone 9 -> the narrow ``ReconciliationReport`` the system was built on."""
    projected = reconciliation_result.to_reconciliation_report()

    assert projected.broker == reconciliation_result.broker
    assert projected.as_of == reconciliation_result.as_of
    assert projected.blocks_new_executions is (not reconciliation_result.matched)


@pytest.mark.contract
def test_a_strategy_position_projects_onto_the_milestone_1_position_snapshot(
    load_schema: Callable[[str], dict[str, Any]],
) -> None:
    from decimal import Decimal as _Decimal

    from tests.positions.factories import MASKED, NOW, execution_leg, execution_record

    from trading_system.domain.enums import TradingMode as _TradingMode
    from trading_system.domain.models import OptionLeg as _OptionLeg
    from trading_system.positions.expected import strategy_position_for
    from trading_system.positions.snapshot import build_position_snapshot

    record = execution_record(quantity=2, filled_quantity=2)
    snapshot = build_position_snapshot(
        [_broker_position_for(record)],
        broker="SIMULATOR",
        account_id="DU1234567",
        trading_mode=_TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
    )
    structure = strategy_position_for(record, snapshot=snapshot, as_of=NOW, account=MASKED)
    assert structure is not None

    leg = execution_leg()
    projected = structure.to_position_snapshot(
        legs=[
            _OptionLeg(
                underlying=leg.underlying,
                right=leg.right,
                strike=leg.strike,
                expiration=leg.expiration,
                action=leg.action,
                multiplier=leg.multiplier,
                broker_contract_id=leg.contract_id,
            )
        ],
        source="SIMULATOR",
        average_entry_price=_Decimal("5.95"),
        market_value=_Decimal("1210.00"),
        currency="USD",
    )

    _validator(load_schema("position_snapshot")).validate(_dump(projected))
    assert projected.quantity == 2
    # The valuation and its currency travel together: they are one observation,
    # and a figure labelled with a currency taken from somewhere else is a
    # figure nobody can check. The account behind this is based in EUR, which
    # is deliberately not what this says.
    assert projected.market_value == _Decimal("1210.00")
    assert projected.currency == "USD"


def _broker_position_for(record):
    """The broker holding one execution's legs would have created."""
    from decimal import Decimal as _Decimal

    from tests.positions.factories import ACCOUNT, NOW

    from trading_system.domain.enums import SecurityType as _SecurityType
    from trading_system.domain.models import BrokerPosition as _BrokerPosition

    leg = record.legs[0]
    return _BrokerPosition(
        account_id=ACCOUNT,
        symbol=record.underlying,
        security_type=_SecurityType.OPTION,
        as_of=NOW,
        source="SIMULATOR",
        contract_id=leg.contract_id,
        currency=leg.currency,
        multiplier=leg.multiplier,
        quantity=_Decimal(record.filled_quantity * leg.ratio),
        average_cost=_Decimal("595.00"),
        expiration=leg.expiration,
        strike=leg.strike,
        right=leg.right,
    )
