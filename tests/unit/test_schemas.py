"""The workflow-boundary schemas exist, load, and are themselves valid."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

#: The workflow boundaries the specification requires a schema for
#: (specification section 25). Declared here rather than derived from the
#: directory listing: a test that reads the answer off disk proves nothing.
REQUIRED_SCHEMAS = (
    "universe_selection",
    # Milestone 4 adds the three universe-selection boundaries: what the
    # deterministic pre-filter hands the agent, what the agent may return, and
    # the immutable record of a whole run.
    "universe_selection_input",
    "universe_agent_ranking",
    "universe_selection_result",
    "research_report",
    # Milestone 5 adds the three research boundaries plus the run record: what
    # the point-in-time assembler hands the agent, what the agent may return,
    # the canonical outlook for one underlying, and the record of a whole run.
    # `research_report` above stays the narrow Milestone 1 boundary the strategy
    # stage consumes; `market_research_report` is the audit artifact that
    # projects onto it.
    "research_input",
    "research_agent_output",
    "market_research_report",
    "research_run",
    "strategy_decision",
    # Milestone 6 adds the two stored artifacts of the strategy and contract
    # stages. `strategy_decision` above stays the narrow Milestone 1 boundary;
    # `strategy_selection` is the audit record that projects onto it, exactly
    # as `market_research_report` does for `research_report`.
    "strategy_selection",
    "contract_selection",
    "purchase_card",
    "allocation_decision",
    "risk_decision",
    # Milestone 7 adds the six allocation and risk boundaries: the two pieces of
    # state a decision rests on, the candidate it is about, the risk verdict,
    # the authorisation, and the record of a whole run. `allocation_decision`
    # and `risk_decision` above stay the narrow Milestone 1 boundaries;
    # `campaign_allocation` is the audit record that projects onto the first,
    # exactly as `market_research_report` does for `research_report`.
    "account_snapshot",
    "campaign_snapshot",
    "allocation_candidate",
    "risk_evaluation",
    "campaign_allocation",
    "allocation_run",
    "order_intent",
    "execution_result",
    # Milestone 8 adds the three execution boundaries. `execution_result` above
    # stays the narrow Milestone 1 contract; `execution_record` is the audit
    # artifact that projects onto it, exactly as `market_research_report` does
    # for `research_report`. `execution_event` is the append-only history a
    # record is folded from, and `execution_run` the record of one run.
    "execution_request",
    "execution_record",
    "execution_event",
    "execution_run",
    "position_snapshot",
    # Milestone 9 adds the position, reservation and reconciliation boundaries.
    # `position_snapshot` above stays the narrow Milestone 1 contract, which a
    # `StrategyPosition` projects onto; `broker_position_snapshot` is what the
    # broker actually reported, `expected_position` is what confirmed fills say
    # should exist, and the two are deliberately different artifacts. There is
    # no Milestone 1 schema for a reconciliation report, so
    # `reconciliation_result` is the only stored shape for one.
    "broker_position_snapshot",
    "expected_position",
    "position_fill",
    "reservation",
    "reservation_event",
    "reconciliation_result",
    "reconciliation_event",
    "exit_decision",
    # Milestone 10 adds the exit-management boundaries. `exit_decision` above
    # stays the narrow Milestone 1 contract (HOLD/SELL); `exit_decision_record`
    # is the wide audit artifact that projects onto it, exactly as
    # `market_research_report` does for `research_report`. `exit_evaluation` is
    # what every policy concluded and what it was measured against,
    # `trailing_state` the stop's own state machine,
    # `position_lifecycle_snapshot` what became of the position, `exit_request`
    # the boundary handed to Milestone 8, and `exit_run` the record of one
    # monitoring run.
    "exit_evaluation",
    "exit_decision_record",
    "trailing_state",
    "position_lifecycle_snapshot",
    "exit_request",
    "exit_run",
    # Milestone 11 adds the realised-result, settlement and operational
    # boundaries. `realized_pnl` is what one closed structure actually made,
    # from broker-confirmed fills and nothing else; `daily_pnl` is the
    # exchange-local roll-up the daily loss limit is evaluated against, and it
    # carries a *status* alongside the figure because an untracked day and a
    # break-even day are different facts. `reservation_settlement` records
    # capital returning to the campaign — or the refusal to return it, which is
    # the more interesting of the two because it names the missing evidence.
    #
    # There is no Milestone 1 schema for any of these; unlike research,
    # strategy and execution, this milestone has no narrow boundary to project
    # onto, because nothing downstream consumes a realised result yet.
    "realized_pnl",
    "daily_pnl",
    "reservation_settlement",
    # The operational artifacts. `job_run` and `scheduler_run` are the
    # scheduler's own record — what was due, what ran, what was skipped and
    # why — and both carry an order count read off the service each job
    # invoked, so "the scheduler placed no orders" is evidence rather than a
    # claim. `operational_health` keeps trading health and observability health
    # in separate fields computed from disjoint components. `alert` is a
    # notification and can express no trade.
    "job_run",
    "scheduler_run",
    "operational_health",
    "alert",
    "trade_snapshot",
)


@pytest.mark.unit
def test_schema_directory_exists(schemas_dir: Path) -> None:
    assert schemas_dir.is_dir()


@pytest.mark.unit
def test_exactly_the_required_schemas_are_present(schemas_dir: Path) -> None:
    found = {path.stem for path in schemas_dir.glob("*.json")}
    assert found == set(REQUIRED_SCHEMAS)


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_is_valid_json(name: str, schemas_dir: Path) -> None:
    payload = json.loads((schemas_dir / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_is_a_valid_json_schema(
    name: str, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """An invalid schema would silently accept everything it was meant to reject."""
    Draft202012Validator.check_schema(load_schema(name))


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_is_self_describing(name: str, load_schema: Callable[[str], dict[str, Any]]) -> None:
    schema = load_schema(name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith(f"{name}.json")
    assert schema["title"]
    assert schema["description"]


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_forbids_unexpected_top_level_fields(
    name: str, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Extra fields from an agent are a contract violation, not a bonus."""
    assert load_schema(name)["additionalProperties"] is False


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_declares_required_fields(
    name: str, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    assert load_schema(name)["required"]


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_internal_refs_resolve(name: str, load_schema: Callable[[str], dict[str, Any]]) -> None:
    """Every $ref points at a definition that exists in the same file."""
    schema = load_schema(name)
    defs = set(schema.get("$defs", {}))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                assert ref.startswith("#/$defs/"), f"{name}: unsupported $ref {ref}"
                assert ref.removeprefix("#/$defs/") in defs, f"{name}: dangling {ref}"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)


@pytest.mark.unit
def test_money_is_never_a_json_number(schemas_dir: Path) -> None:
    """Monetary values serialise as strings so exact decimals survive JSON."""
    for path in schemas_dir.glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        for def_name in ("money", "nullable_money"):
            definition = schema.get("$defs", {}).get(def_name)
            if definition is None:
                continue
            types = definition["type"]
            types = [types] if isinstance(types, str) else types
            assert "number" not in types, f"{path.name}: {def_name} allows a JSON number"
            assert "string" in types
