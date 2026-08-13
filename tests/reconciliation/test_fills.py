"""Comparing broker fills against the execution ledger (brief section 33).

The asymmetry is the point. IBKR's execution list is session-scoped, so:

* a fill the broker reports that no execution explains is a real discrepancy;
* a fill *we* recorded that today's list does not contain is usually just an
  earlier session, and calling it a mismatch would raise a false alarm every
  morning.

The second becomes a genuine ``FILL_MISMATCH`` only when the configuration
explicitly claims the list is a complete history — which it ships not claiming.
"""

from __future__ import annotations

import pytest

from tests.positions.factories import (
    MASKED,
    NOW,
    broker_execution,
    execution_record,
)
from trading_system.domain.enums import (
    BrokerReadStatus,
    ExecutionState,
    ReconciliationFindingType,
)
from trading_system.positions.fills import ContractTerms, to_observed_fill
from trading_system.reconciliation.fills import compare_fills

pytestmark = pytest.mark.unit

TERMS = ContractTerms(multiplier=100)


def _fill(*, execution_id: str | None = "execution-1", broker_order_id: str = "ord-1"):
    return to_observed_fill(
        broker_execution(broker_order_id=broker_order_id),
        observed_at=NOW,
        account_reference=MASKED,
        terms=TERMS,
        execution_id=execution_id,
    )


def _compare(
    policy,
    *,
    fills=(),
    executions=(),
    status=BrokerReadStatus.OK,
    required=False,
    complete=False,
):
    return compare_fills(
        broker_fills=list(fills),
        executions=list(executions),
        fills_status=status,
        severity=policy.severity_of,
        broker="SIMULATOR",
        required=required,
        complete_history=complete,
        observed_at=NOW,
    )


def test_a_fill_an_execution_explains_is_agreement(policy) -> None:
    [finding] = _compare(policy, fills=[_fill()])
    assert finding.finding_type is ReconciliationFindingType.FILL_MATCH
    assert finding.agreement is True
    assert finding.execution_id == "execution-1"


def test_a_fill_no_execution_explains_is_an_orphan(policy) -> None:
    [finding] = _compare(policy, fills=[_fill(execution_id=None)])
    assert finding.finding_type is ReconciliationFindingType.ORPHAN_BROKER_FILL
    assert finding.observed_value == "BUY 2 @ 5.95"
    assert finding.expected_value is None


def test_an_orphan_fill_keeps_unknown_provenance_and_invents_nothing(policy) -> None:
    [finding] = _compare(policy, fills=[_fill(execution_id=None)])
    detail = (finding.detail or "").lower()
    assert "provenance unknown" in detail
    assert "no execution, allocation or strategy is invented" in detail


def test_a_recorded_fill_absent_from_a_session_list_is_not_a_mismatch(policy) -> None:
    """The false alarm this asymmetry exists to prevent."""
    record = execution_record(state=ExecutionState.FILLED, filled_quantity=2)
    findings = _compare(policy, fills=[], executions=[record], status=BrokerReadStatus.EMPTY)
    assert findings == []


def test_it_becomes_a_mismatch_only_when_the_history_is_claimed_complete(policy) -> None:
    record = execution_record(state=ExecutionState.FILLED, filled_quantity=2)
    [finding] = _compare(
        policy,
        fills=[],
        executions=[record],
        status=BrokerReadStatus.EMPTY,
        complete=True,
    )
    assert finding.finding_type is ReconciliationFindingType.FILL_MISMATCH
    assert finding.expected_value == "2 filled"
    assert finding.observed_value == "no broker fill"


def test_a_fill_mismatch_says_it_rests_on_a_configured_claim(policy) -> None:
    record = execution_record(state=ExecutionState.FILLED, filled_quantity=2)
    [finding] = _compare(
        policy, fills=[], executions=[record], status=BrokerReadStatus.EMPTY, complete=True
    )
    assert "complete history" in (finding.detail or "")


def test_an_unreadable_fill_list_is_silent_unless_it_was_required(policy) -> None:
    assert _compare(policy, status=BrokerReadStatus.UNAVAILABLE) == []


def test_an_unreadable_fill_list_that_was_required_is_reported(policy) -> None:
    [finding] = _compare(policy, status=BrokerReadStatus.UNAVAILABLE, required=True)
    assert finding.finding_type is ReconciliationFindingType.BROKER_DATA_UNAVAILABLE
    assert "cannot be read as 'the account has not traded'" in (finding.detail or "")


def test_the_comparison_is_deterministic(policy) -> None:
    fills = [
        _fill(broker_order_id="ord-1"),
        to_observed_fill(
            broker_execution(execution_id="exec-2", broker_order_id="ord-2"),
            observed_at=NOW,
            account_reference=MASKED,
            terms=TERMS,
            execution_id="execution-2",
        ),
    ]
    first = _compare(policy, fills=fills)
    second = _compare(policy, fills=list(reversed(fills)))
    assert [f.finding_id for f in first] == [f.finding_id for f in second]


def test_a_fill_finding_records_both_clocks(policy) -> None:
    [finding] = _compare(policy, fills=[_fill()])
    assert finding.observed_at is not None
    assert finding.broker_timestamp is not None
