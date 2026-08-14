"""The stored exit artifacts validate against their hand-written schemas.

Every workflow boundary in this system has a JSON schema, and a schema that
drifts from the model it describes is worse than none: it validates the shape
nobody writes. These tests serialise the artifacts the service actually
produces — not hand-built dictionaries — and validate them.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.domain.enums import (
    ExitDecisionType,
    ExitPolicyKind,
    ExitQuoteField,
    ExitReasonCode,
    OrderType,
    StrategyType,
    TimeInForce,
    TradingMode,
    TrailingStopState,
)
from trading_system.exit.models import ExitRequest

pytestmark = pytest.mark.contract


def _validate(schema: dict[str, Any], payload: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)


# ---------------------------------------------------------------------------
# What the service actually writes
# ---------------------------------------------------------------------------
def test_a_real_evaluation_validates(open_long_call, load_schema) -> None:
    service, _ = open_long_call
    run = service.monitor()

    _validate(load_schema("exit_evaluation"), run.result.evaluations[0].model_dump(mode="json"))


def test_a_real_decision_validates(open_long_call, load_schema) -> None:
    service, _ = open_long_call
    run = service.monitor()

    _validate(load_schema("exit_decision_record"), run.result.decisions[0].model_dump(mode="json"))


def test_a_real_run_validates(open_long_call, load_schema) -> None:
    service, _ = open_long_call
    run = service.monitor()

    _validate(load_schema("exit_run"), run.result.model_dump(mode="json"))


def test_a_real_lifecycle_validates(open_long_call, load_schema) -> None:
    service, _ = open_long_call
    service.monitor()
    lifecycle = service.lifecycle(service.open_positions()[0].position_id)

    assert lifecycle is not None
    _validate(load_schema("position_lifecycle_snapshot"), lifecycle.model_dump(mode="json"))


def test_a_real_trailing_record_validates(
    build_exit_service, data_repo, exit_repo, stored_research, load_schema
) -> None:
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("9.00"), ask=Decimal("9.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    service.monitor()
    trailing = exit_repo.trailing(service.open_positions()[0].position_id)

    assert trailing is not None
    assert trailing.state is TrailingStopState.ARMED
    _validate(load_schema("trailing_state"), trailing.model_dump(mode="json"))


def test_a_real_exit_request_validates(
    build_exit_service, data_repo, stored_research, load_schema
) -> None:
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("2.00"), ask=Decimal("2.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    run = service.monitor()
    request = service.build_request(run.outcomes[0], at=NOW)

    assert request is not None
    _validate(load_schema("exit_request"), request.model_dump(mode="json"))


def test_a_block_validates(build_exit_service, stored_research, load_schema) -> None:
    """The shape that carries several reason codes and a recommended action."""
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    run = service.monitor()
    decision = run.result.decisions[0]

    assert decision.decision is ExitDecisionType.BLOCK
    _validate(load_schema("exit_decision_record"), decision.model_dump(mode="json"))


def test_a_straddle_evaluation_validates(open_straddle, load_schema) -> None:
    service, _ = open_straddle
    run = service.monitor()

    _validate(load_schema("exit_evaluation"), run.result.evaluations[0].model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Money is a string; the schemas refuse a JSON number
# ---------------------------------------------------------------------------
def test_money_is_serialised_as_a_string(open_long_call) -> None:
    """A JSON number would import binary floating point into accounting."""
    service, _ = open_long_call
    run = service.monitor()
    payload = run.result.decisions[0].model_dump(mode="json")

    for field in ("exit_quote", "exit_value", "entry_cost", "unrealized_pnl"):
        assert isinstance(payload[field], str), field


def test_a_numeric_price_is_refused_by_the_schema(open_long_call, load_schema) -> None:
    service, _ = open_long_call
    run = service.monitor()
    payload = run.result.decisions[0].model_dump(mode="json")
    payload["exit_quote"] = 6.5

    with pytest.raises(ValidationError, match=r"6\.5"):
        _validate(load_schema("exit_decision_record"), payload)


# ---------------------------------------------------------------------------
# The schemas enforce what the models enforce
# ---------------------------------------------------------------------------
def test_the_schema_refuses_an_exit_with_two_triggering_reasons(
    open_long_call, load_schema
) -> None:
    service, _ = open_long_call
    run = service.monitor()
    payload = run.result.decisions[0].model_dump(mode="json")
    payload.update(
        {
            "decision": "EXIT",
            "reason_codes": ["MAX_LOSS_REACHED", "TAKE_PROFIT_REACHED"],
            "triggering_policy": "MAX_LOSS",
            "quantity": 2,
        }
    )

    with pytest.raises(ValidationError):
        _validate(load_schema("exit_decision_record"), payload)


def test_the_schema_refuses_a_wait_carrying_a_trigger_reason(open_long_call, load_schema) -> None:
    service, _ = open_long_call
    run = service.monitor()
    payload = run.result.decisions[0].model_dump(mode="json")
    payload["reason_codes"] = ["THESIS_INVALIDATED"]

    with pytest.raises(ValidationError):
        _validate(load_schema("exit_decision_record"), payload)


def test_the_schema_refuses_a_partial_structure_exit(open_long_call, load_schema) -> None:
    service, _ = open_long_call
    run = service.monitor()
    payload = run.result.decisions[0].model_dump(mode="json")
    payload["close_whole_strategy"] = False

    with pytest.raises(ValidationError):
        _validate(load_schema("exit_decision_record"), payload)


def test_the_schema_refuses_an_evaluation_that_submitted_an_order(
    open_long_call, load_schema
) -> None:
    service, _ = open_long_call
    run = service.monitor()
    payload = run.result.evaluations[0].model_dump(mode="json")
    payload["orders_submitted"] = 1

    with pytest.raises(ValidationError):
        _validate(load_schema("exit_evaluation"), payload)


def test_the_schema_refuses_an_unauthorised_exit_request(load_schema) -> None:
    """``exit_authorized`` is typed ``const: true``: there is no shape in which
    deciding to close a position and closing it are the same call."""
    request = ExitRequest(
        exit_request_id="exit-req-1",
        position_id="strategypos-1",
        decision_id="exitdec-1",
        evaluation_id="exiteval-1",
        created_at=NOW,
        exit_authorized=True,
        underlying="NVDA",
        strategy=StrategyType.LONG_CALL,
        quantity=2,
        exit_reason=ExitReasonCode.MAX_LOSS_REACHED,
        triggering_policy=ExitPolicyKind.MAX_LOSS,
        reference_quote=Decimal("2.00"),
        quote_field=ExitQuoteField.BID,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        trading_mode=TradingMode.PAPER,
        entry_execution_id="execution-entry-1",
        allocation_id="allocation-1",
        campaign_id="campaign-001",
        opportunity_id="opportunity-1",
        policy_version="1.0.0",
        versions=factories.versions(),
    )
    payload = request.model_dump(mode="json")
    payload["exit_authorized"] = False

    with pytest.raises(ValidationError):
        _validate(load_schema("exit_request"), payload)


def test_the_schema_refuses_an_unauthorised_run_that_submitted(open_long_call, load_schema) -> None:
    service, _ = open_long_call
    run = service.monitor()
    payload = run.result.model_dump(mode="json")
    payload["orders_submitted"] = 1

    with pytest.raises(ValidationError):
        _validate(load_schema("exit_run"), payload)


def test_the_schema_refuses_a_closed_position_that_still_holds_contracts(
    open_long_call, load_schema
) -> None:
    service, _ = open_long_call
    service.monitor()
    lifecycle = service.lifecycle(service.open_positions()[0].position_id)
    assert lifecycle is not None
    payload = lifecycle.model_dump(mode="json")
    payload.update({"state": "CLOSED", "open_quantity": 2})

    with pytest.raises(ValidationError):
        _validate(load_schema("position_lifecycle_snapshot"), payload)


def test_the_schema_refuses_an_inactive_trail_carrying_a_level(load_schema) -> None:
    payload = factories.trailing_record().model_dump(mode="json")
    payload["stop_quote"] = "5.00"

    with pytest.raises(ValidationError):
        _validate(load_schema("trailing_state"), payload)


# ---------------------------------------------------------------------------
# No dangling references
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "exit_evaluation",
        "exit_decision_record",
        "trailing_state",
        "position_lifecycle_snapshot",
        "exit_request",
        "exit_run",
    ],
)
def test_every_local_reference_resolves(name: str, load_schema: Callable[[str], Any]) -> None:
    """A dangling ``$ref`` makes a schema silently permissive."""
    schema = load_schema(name)
    definitions = set(schema.get("$defs", {}))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                assert reference.removeprefix("#/$defs/") in definitions, reference
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)


@pytest.mark.parametrize(
    "name",
    [
        "exit_evaluation",
        "exit_decision_record",
        "trailing_state",
        "position_lifecycle_snapshot",
        "exit_request",
        "exit_run",
    ],
)
def test_every_object_forbids_unknown_properties(
    name: str, load_schema: Callable[[str], Any]
) -> None:
    """An agent — or a later change — adding a field must fail loudly rather
    than have it silently dropped."""
    schema = load_schema(name)
    assert schema["additionalProperties"] is False
