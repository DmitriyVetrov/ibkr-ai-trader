"""The deterministic reconciliation engine (brief sections 27-29, 35, 63, 77).

The engine composes the comparisons into one immutable result. What is asserted
here is the composition itself:

* a status that agrees with the findings, and a ``MATCH`` that requires the
  broker to have actually been read;
* ``orders_submitted`` and ``corrective_orders`` structurally zero;
* severity taken from configuration rather than decided in code;
* a content hash that ignores observation clocks, so the same comparison twice
  is recognisable as the same comparison.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.positions.factories import (
    EXPIRATION,
    MASKED,
    NOW,
    broker_order,
    execution_record,
    option_position,
    reservation,
    stock_position,
)
from trading_system.domain.enums import (
    AcquisitionProvenance,
    BrokerReadStatus,
    ExecutionState,
    OptionRight,
    ReconciliationFindingType,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    ReservationEventType,
    ReservationReasonCode,
    ReservationState,
    SecurityType,
)
from trading_system.positions.models import ExpectedPosition, position_identifier
from trading_system.reconciliation.models import ReconciliationResult
from trading_system.reservations.lifecycle import ReservationOutcome

pytestmark = pytest.mark.unit


def _expected(quantity: Decimal = Decimal("2")) -> ExpectedPosition:
    key = "cid:100001"
    return ExpectedPosition(
        position_id=position_identifier(account_reference=MASKED, key=key),
        account_reference=MASKED,
        key=key,
        as_of=NOW,
        underlying="NVDA",
        asset_class=SecurityType.OPTION,
        symbol="NVDA",
        contract_id=100001,
        expiration=EXPIRATION,
        strike=Decimal("180.00"),
        right=OptionRight.CALL,
        multiplier=100,
        currency="EUR",
        quantity=quantity,
        bought_quantity=quantity,
        fill_ids=["fill-1"],
        execution_ids=["execution-1"],
        provenance=AcquisitionProvenance.SYSTEM_EXECUTION,
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def test_agreement_is_a_match(engine, inputs_for, snapshot_of) -> None:
    result = engine.reconcile(
        inputs_for(snapshot=snapshot_of([option_position()]), expected=(_expected(),))
    )
    assert result.status is ReconciliationRunStatus.MATCH
    assert result.matched is True
    assert result.blocks_new_executions is False


def test_a_disagreement_is_a_mismatch(engine, inputs_for, snapshot_of) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([stock_position()]), expected=()))
    assert result.status is ReconciliationRunStatus.MISMATCH
    assert result.blocks_new_executions is True


def test_an_unreadable_broker_is_never_a_match(engine, inputs_for, unreadable_snapshot) -> None:
    """Agreeing with an absence of data is not agreement."""
    result = engine.reconcile(
        inputs_for(
            snapshot=unreadable_snapshot,
            account_read=BrokerReadStatus.UNAVAILABLE,
            expected=(),
        )
    )
    assert result.status is ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE
    assert result.blocks_new_executions is True


def test_a_match_cannot_be_constructed_over_an_unread_broker(engine, inputs_for) -> None:
    result = engine.reconcile(inputs_for())
    assert result.status is ReconciliationRunStatus.MATCH

    with pytest.raises(ValidationError, match="MATCH requires"):
        ReconciliationResult.model_validate(
            result.model_dump() | {"positions_read": BrokerReadStatus.UNAVAILABLE.value}
        )


def test_an_unreadable_internal_ledger_is_its_own_status(engine, inputs_for, snapshot_of) -> None:
    """Our fault is not the broker's fault, and neither is a match."""
    result = engine.reconcile(
        inputs_for(
            snapshot=snapshot_of([]),
            internal_failures=("the execution ledger could not be read",),
        )
    )
    assert result.status is ReconciliationRunStatus.INTERNAL_DATA_UNAVAILABLE
    assert result.by_type(ReconciliationFindingType.INTERNAL_DATA_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Reconciliation never trades (brief section 28)
# ---------------------------------------------------------------------------
def test_a_result_reports_zero_submitted_and_zero_corrective_orders(
    engine, inputs_for, snapshot_of
) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    assert result.orders_submitted == 0
    assert result.corrective_orders == 0


def test_a_result_claiming_a_submitted_order_fails_to_construct(
    engine, inputs_for, snapshot_of
) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    with pytest.raises(ValidationError, match="must never place one"):
        ReconciliationResult.model_validate(result.model_dump() | {"orders_submitted": 1})


def test_a_result_claiming_a_corrective_order_fails_to_construct(
    engine, inputs_for, snapshot_of
) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    with pytest.raises(ValidationError, match="corrective order"):
        ReconciliationResult.model_validate(result.model_dump() | {"corrective_orders": 1})


def test_no_finding_ever_recommends_a_trade(engine, inputs_for, snapshot_of) -> None:
    """ACTION REQUIRED, never AUTO-SELL or AUTO-BUY (brief section 94)."""
    result = engine.reconcile(
        inputs_for(snapshot=snapshot_of([stock_position()]), expected=(_expected(),))
    )
    for finding in result.findings:
        text = f"{finding.summary} {finding.detail or ''} {finding.recommended_action or ''}"
        assert "AUTO-SELL" not in text.upper()
        assert "AUTO-BUY" not in text.upper()
    assert any("ACTION REQUIRED" in (f.recommended_action or "") for f in result.disagreements)


# ---------------------------------------------------------------------------
# Severity comes from configuration (brief section 63)
# ---------------------------------------------------------------------------
def test_severity_is_read_from_configuration(engine, policy) -> None:
    assert engine.severity_of(
        ReconciliationFindingType.EXPECTED_POSITION_MISSING
    ) is policy.severity_of(ReconciliationFindingType.EXPECTED_POSITION_MISSING)


def test_every_finding_type_has_a_configured_severity(policy) -> None:
    for finding in ReconciliationFindingType:
        assert isinstance(policy.severity_of(finding), ReconciliationSeverity)


def test_a_critical_finding_is_counted_as_critical(engine, inputs_for, snapshot_of) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([]), expected=(_expected(),)))
    assert result.counts.critical >= 1
    assert result.critical_findings


# ---------------------------------------------------------------------------
# Content identity
# ---------------------------------------------------------------------------
def test_the_same_comparison_produces_the_same_id(engine, inputs_for, snapshot_of) -> None:
    first = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    second = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    assert first.reconciliation_id == second.reconciliation_id
    assert first.content_hash == second.content_hash


def test_a_different_broker_state_produces_a_different_id(engine, inputs_for, snapshot_of) -> None:
    first = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    second = engine.reconcile(
        inputs_for(snapshot=snapshot_of([option_position(quantity=Decimal("9"))]))
    )
    assert first.reconciliation_id != second.reconciliation_id


def test_the_content_hash_excludes_the_observation_clock(engine, inputs_for, snapshot_of) -> None:
    from datetime import timedelta

    first = engine.reconcile(inputs_for(snapshot=snapshot_of([option_position()])))
    later = engine.reconcile(
        inputs_for(
            snapshot=snapshot_of([option_position()]),
            observed_at=NOW + timedelta(minutes=5),
        )
    )
    assert first.content_hash == later.content_hash


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
def test_the_result_names_everything_it_compared(engine, inputs_for, snapshot_of) -> None:
    record = execution_record(state=ExecutionState.SUBMITTED, filled_quantity=0)
    held = reservation()
    result = engine.reconcile(
        inputs_for(
            snapshot=snapshot_of([option_position()]),
            expected=(_expected(),),
            executions=(record,),
            orders=(broker_order(),),
            orders_read=BrokerReadStatus.OK,
            reservations=(
                (
                    held,
                    ReservationOutcome(
                        state=ReservationState.RESERVED,
                        reason_code=ReservationReasonCode.NOT_EXECUTED,
                        event_type=ReservationEventType.RESERVATION_OBSERVED,
                    ),
                ),
            ),
        )
    )
    assert result.execution_ids == [record.execution_id]
    assert result.reservation_ids == [held.reservation_id]
    assert result.position_snapshot_id is not None
    assert result.expected_position_count == 1
    assert result.broker_position_count == 1


def test_the_result_projects_onto_the_milestone_1_boundary(engine, inputs_for, snapshot_of) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([stock_position()]), expected=()))
    report = result.to_reconciliation_report()
    assert report.status.value == "MISMATCH"
    assert report.discrepancies
    assert report.blocks_new_executions is True


def test_an_internal_failure_cannot_be_projected_onto_the_milestone_1_boundary(
    engine, inputs_for, snapshot_of
) -> None:
    """Our own fault is not a broker failure and is not a match."""
    result = engine.reconcile(
        inputs_for(snapshot=snapshot_of([]), internal_failures=("store unreadable",))
    )
    with pytest.raises(ValueError, match="no Milestone 1 equivalent"):
        result.to_reconciliation_report()


def test_an_agreement_has_no_milestone_1_discrepancy(engine, inputs_for, snapshot_of) -> None:
    result = engine.reconcile(inputs_for(snapshot=snapshot_of([])))
    [finding] = result.findings
    with pytest.raises(ValueError, match="records agreement"):
        finding.to_discrepancy()
