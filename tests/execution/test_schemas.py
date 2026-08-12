"""The Milestone 8 schemas, against real artifacts (brief section 59).

Each stored artifact must validate against its hand-authored schema, and each
schema must reject the shapes the models refuse. Two layers saying the same
thing is deliberate: the model guards the process, the schema guards anything
that reads the stored file later — including tools that are not this codebase.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator, ValidationError

from trading_system.domain.enums import ExecutionRunStatus, ExecutionState, TradingMode
from trading_system.execution.models import ExecutionRunCounts, ExecutionRunResult

from .conftest import NOW

pytestmark = pytest.mark.contract


@pytest.fixture
def record(make_record):
    return make_record(state=ExecutionState.SUBMITTED, broker_order_id="b-1")


@pytest.fixture
def run(record, versions):
    return ExecutionRunResult(
        run_id="execrun-0001",
        campaign_id="campaign-001",
        as_of=NOW,
        generated_at=NOW,
        status=ExecutionRunStatus.SUCCESS,
        trading_mode=TradingMode.PAPER,
        broker="SIMULATOR",
        policy_version="2026.08.10-1",
        executions=[record],
        counts=ExecutionRunCounts(considered=1, submitted=1),
        orders_submitted=1,
        versions=versions,
    )


# ---------------------------------------------------------------------------
# Artifacts validate
# ---------------------------------------------------------------------------
def test_a_request_validates(make_request, load_schema) -> None:
    Draft202012Validator(load_schema("execution_request")).validate(
        make_request().model_dump(mode="json")
    )


def test_a_record_validates(record, load_schema) -> None:
    Draft202012Validator(load_schema("execution_record")).validate(record.model_dump(mode="json"))


def test_a_run_validates(run, load_schema) -> None:
    Draft202012Validator(load_schema("execution_run")).validate(run.model_dump(mode="json"))


def test_an_event_validates(record, load_schema) -> None:
    from trading_system.domain.enums import ExecutionEventType
    from trading_system.execution.models import ExecutionEvent

    event = ExecutionEvent(
        event_id="evt-1",
        execution_id=record.execution_id,
        sequence=1,
        event_type=ExecutionEventType.EXECUTION_SUBMITTED,
        state=ExecutionState.SUBMITTED,
        occurred_at=NOW,
        observed_at=NOW,
        source="SIMULATOR",
        broker_order_id="b-1",
    )
    Draft202012Validator(load_schema("execution_event")).validate(event.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# The schemas reject what the models reject
# ---------------------------------------------------------------------------
def test_the_request_schema_refuses_an_unauthorised_request(make_request, load_schema) -> None:
    """``execution_authorized`` is ``const: true``: the shape is inexpressible."""
    payload = make_request().model_dump(mode="json") | {"execution_authorized": False}

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_request")).validate(payload)


def test_the_request_schema_permits_only_limit_orders(make_request, load_schema) -> None:
    payload = make_request().model_dump(mode="json") | {"order_type": "MARKET"}

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_request")).validate(payload)


def test_the_record_schema_refuses_submitted_without_a_broker_order_id(record, load_schema) -> None:
    payload = record.model_dump(mode="json") | {"broker_order_id": None}

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_record")).validate(payload)


def test_the_record_schema_refuses_a_dry_run_that_submitted(record, load_schema) -> None:
    payload = record.model_dump(mode="json") | {
        "dry_run": True,
        "state": "VALIDATED",
        "broker_order_id": None,
        "orders_submitted": 1,
    }

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_record")).validate(payload)


def test_the_record_schema_refuses_a_dry_run_in_a_live_state(record, load_schema) -> None:
    payload = record.model_dump(mode="json") | {"dry_run": True, "broker_order_id": None}

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_record")).validate(payload)


def test_the_record_schema_requires_a_reason_on_a_refusal(record, load_schema) -> None:
    payload = record.model_dump(mode="json") | {
        "state": "REJECTED",
        "broker_order_id": None,
        "reason_codes": [],
    }

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_record")).validate(payload)


def test_the_record_schema_requires_a_contract_id_on_every_leg(record, load_schema) -> None:
    """Never re-derived, so never optional."""
    payload = record.model_dump(mode="json")
    del payload["legs"][0]["contract_id"]

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_record")).validate(payload)


def test_the_record_schema_requires_a_multiplier_on_every_leg(record, load_schema) -> None:
    payload = record.model_dump(mode="json")
    del payload["legs"][0]["multiplier"]

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_record")).validate(payload)


def test_the_run_schema_refuses_a_dry_run_that_submitted(run, load_schema) -> None:
    payload = run.model_dump(mode="json") | {"dry_run": True}

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_run")).validate(payload)


def test_the_schemas_reject_unknown_fields(record, run, make_request, load_schema) -> None:
    for name, artifact in (
        ("execution_record", record),
        ("execution_run", run),
        ("execution_request", make_request()),
    ):
        payload = artifact.model_dump(mode="json") | {"surprise": "value"}
        with pytest.raises(ValidationError):
            Draft202012Validator(load_schema(name)).validate(payload)


def test_money_is_a_string_everywhere(record, load_schema) -> None:
    """Exact decimals survive JSON; a binary float would not."""
    payload = record.model_dump(mode="json")

    assert isinstance(payload["capital_commitment"], str)
    assert isinstance(payload["reference_price"], str)
    assert isinstance(payload["submitted_price"], str)

    broken = payload | {"capital_commitment": 605.0}
    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("execution_record")).validate(broken)


def test_timestamps_serialise_as_utc(record) -> None:
    payload = record.model_dump(mode="json")
    assert payload["created_at"].endswith("Z") or "+00:00" in payload["created_at"]


# ---------------------------------------------------------------------------
# The vocabulary stays in step with the code
# ---------------------------------------------------------------------------
def test_the_schema_lists_every_execution_state(load_schema) -> None:
    schema = load_schema("execution_record")
    listed = set(schema["properties"]["state"]["enum"])

    assert listed == {state.value for state in ExecutionState}


def test_the_schema_lists_every_reason_code(load_schema) -> None:
    from trading_system.domain.enums import ExecutionReasonCode

    schema = load_schema("execution_record")
    listed = set(schema["$defs"]["execution_reason_code"]["enum"])

    assert listed == {code.value for code in ExecutionReasonCode}


def test_the_event_schema_lists_every_event_type(load_schema) -> None:
    from trading_system.domain.enums import ExecutionEventType

    schema = load_schema("execution_event")
    listed = set(schema["properties"]["event_type"]["enum"])

    assert listed == {event.value for event in ExecutionEventType}


def test_the_run_schema_lists_every_run_status(load_schema) -> None:
    schema = load_schema("execution_run")
    listed = set(schema["properties"]["status"]["enum"])

    assert listed == {status.value for status in ExecutionRunStatus}


def test_the_embedded_record_matches_the_standalone_schema(load_schema) -> None:
    """The run embeds the record so each file validates on its own.

    Two copies that drifted would let a stored run accept a record its own
    schema rejects.
    """
    standalone = load_schema("execution_record")
    embedded = load_schema("execution_run")["$defs"]["execution_record"]

    assert embedded["required"] == standalone["required"]
    assert set(embedded["properties"]) == set(standalone["properties"])
