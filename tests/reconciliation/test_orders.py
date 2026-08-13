"""Comparing executions against the broker's open orders (brief sections 32, 90-91).

The serious one is ``FAILED_EXECUTION_HAS_BROKER_ORDER``. ``FAILED`` means the
attempt provably never left the process — that is the whole distinction between
``FAILED`` and ``UNKNOWN`` — so an order at the broker for a ``FAILED``
execution means one of the two records is wrong about something that moved
money. It is reported, and the execution is never quietly relabelled.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.positions.factories import NOW, broker_order, execution_record
from trading_system.domain.enums import (
    BrokerReadStatus,
    ExecutionState,
    OrderStatus,
    ReconciliationFindingType,
)
from trading_system.reconciliation.orders import compare_orders

pytestmark = pytest.mark.unit


def _compare(policy, *, executions=(), orders=(), status=BrokerReadStatus.OK):
    return compare_orders(
        executions=list(executions),
        orders=list(orders),
        orders_status=status,
        severity=policy.severity_of,
        broker="SIMULATOR",
        observed_at=NOW,
    )


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------
def test_a_submitted_execution_with_an_open_order_matches(policy) -> None:
    record = execution_record(state=ExecutionState.SUBMITTED, filled_quantity=0)
    record = record.model_copy(update={"broker_status": OrderStatus.SUBMITTED})
    [finding] = _compare(policy, executions=[record], orders=[broker_order()])
    assert finding.finding_type is ReconciliationFindingType.ORDER_MATCH
    assert finding.agreement is True


# ---------------------------------------------------------------------------
# Disagreement
# ---------------------------------------------------------------------------
def test_a_submitted_execution_the_broker_does_not_report_is_a_question(policy) -> None:
    """Absence from the open list is not evidence that nothing was sent."""
    record = execution_record(state=ExecutionState.SUBMITTED, filled_quantity=0)
    [finding] = _compare(policy, executions=[record], orders=[])
    assert finding.finding_type is ReconciliationFindingType.EXPECTED_ORDER_MISSING
    assert "filled order, a cancelled order and an order that never existed" in (
        finding.detail or ""
    )


def test_an_order_state_disagreement_reports_both_sides(policy) -> None:
    record = execution_record(state=ExecutionState.SUBMITTED, filled_quantity=0)
    record = record.model_copy(update={"broker_status": OrderStatus.SUBMITTED})
    [finding] = _compare(
        policy,
        executions=[record],
        orders=[broker_order(status=OrderStatus.PARTIALLY_FILLED, filled=Decimal("1"))],
    )
    assert finding.finding_type is ReconciliationFindingType.ORDER_STATE_MISMATCH
    assert finding.expected_value is not None
    assert finding.observed_value is not None


def test_an_order_the_broker_has_that_no_execution_names_is_an_orphan(policy) -> None:
    [finding] = _compare(policy, executions=[], orders=[broker_order(broker_order_id="theirs")])
    assert finding.finding_type is ReconciliationFindingType.ORPHAN_BROKER_ORDER
    assert finding.broker_order_id == "theirs"


def test_an_orphan_order_is_never_cancelled_automatically(policy) -> None:
    [finding] = _compare(policy, executions=[], orders=[broker_order(broker_order_id="theirs")])
    assert "not cancelled automatically" in (finding.detail or "")
    assert "Nothing here cancels it" in (finding.recommended_action or "")


def test_a_terminal_execution_with_a_still_open_order_is_a_mismatch(policy) -> None:
    record = execution_record(state=ExecutionState.CANCELLED, filled_quantity=0)
    [finding] = _compare(policy, executions=[record], orders=[broker_order()])
    assert finding.finding_type is ReconciliationFindingType.ORDER_STATE_MISMATCH


# ---------------------------------------------------------------------------
# The FAILED invariant (brief section 91)
# ---------------------------------------------------------------------------
def test_a_failed_execution_with_a_broker_order_is_a_critical_violation(policy) -> None:
    record = execution_record(state=ExecutionState.FAILED, filled_quantity=0)
    record = record.model_copy(update={"broker_order_id": "ord-1"})

    [finding] = _compare(policy, executions=[record], orders=[broker_order()])

    assert finding.finding_type is ReconciliationFindingType.FAILED_EXECUTION_HAS_BROKER_ORDER
    assert finding.severity.value == "CRITICAL"


def test_a_failed_execution_is_never_relabelled_as_submitted(policy) -> None:
    record = execution_record(state=ExecutionState.FAILED, filled_quantity=0)
    record = record.model_copy(update={"broker_order_id": "ord-1"})

    [finding] = _compare(policy, executions=[record], orders=[broker_order()])

    assert "NOT relabelled" in (finding.detail or "")
    assert record.state is ExecutionState.FAILED


def test_a_failed_execution_with_no_broker_order_produces_no_finding(policy) -> None:
    """The ordinary case: FAILED means nothing was sent, and nothing is there."""
    record = execution_record(state=ExecutionState.FAILED, filled_quantity=0)
    assert _compare(policy, executions=[record], orders=[]) == []


# ---------------------------------------------------------------------------
# UNKNOWN is not compared here
# ---------------------------------------------------------------------------
def test_an_unknown_execution_is_not_reported_as_an_order_mismatch(policy) -> None:
    """It is a question, and it is answered by resolution rather than comparison."""
    record = execution_record(state=ExecutionState.UNKNOWN, filled_quantity=0)
    findings = _compare(policy, executions=[record], orders=[])
    assert findings == []


# ---------------------------------------------------------------------------
# Unreadable orders
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status", [BrokerReadStatus.UNAVAILABLE, BrokerReadStatus.TIMEOUT, BrokerReadStatus.MALFORMED]
)
def test_an_unreadable_order_list_compares_nothing(policy, status) -> None:
    record = execution_record(state=ExecutionState.SUBMITTED, filled_quantity=0)
    [finding] = _compare(policy, executions=[record], orders=[], status=status)
    assert finding.finding_type is ReconciliationFindingType.BROKER_DATA_UNAVAILABLE
    assert "not 'the broker has no open orders'" in (finding.detail or "")


def test_an_empty_order_list_is_a_real_answer(policy) -> None:
    record = execution_record(state=ExecutionState.SUBMITTED, filled_quantity=0)
    [finding] = _compare(policy, executions=[record], orders=[], status=BrokerReadStatus.EMPTY)
    assert finding.finding_type is ReconciliationFindingType.EXPECTED_ORDER_MISSING


def test_the_comparison_is_deterministic(policy) -> None:
    executions = [
        execution_record(execution_id="execution-b", state=ExecutionState.SUBMITTED),
        execution_record(execution_id="execution-a", state=ExecutionState.SUBMITTED),
    ]
    first = _compare(policy, executions=executions, orders=[])
    second = _compare(policy, executions=list(reversed(executions)), orders=[])
    assert [f.finding_id for f in first] == [f.finding_id for f in second]
