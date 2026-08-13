"""Resolving ambiguous submissions by observation (brief sections 26, 78, 92).

Mandatory coverage, one test per row of the brief's table:

.. code-block:: text

    UNKNOWN + broker OPEN            -> SUBMITTED, reservation stays
    UNKNOWN + broker FILLED          -> FILLED, fills recorded, capital consumed
    UNKNOWN + broker CANCELLED       -> CANCELLED, capital released
    UNKNOWN + broker cannot say      -> stays UNKNOWN, capital stays locked

Nothing here is optimistic, and nothing here retries. Absence from a list of
*open* orders resolves nothing: a filled, a cancelled and a never-sent order
look identical from there.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.positions.factories import NOW, broker_execution, broker_order, execution_record
from trading_system.domain.enums import (
    ExecutionState,
    OrderStatus,
    ReconciliationFindingType,
)
from trading_system.reconciliation.unknown import resolve_unknown, unknown_findings

pytestmark = pytest.mark.unit


def _resolve(
    *,
    orders=(),
    fills=(),
    orders_readable: bool = True,
    fills_readable: bool = True,
    complete_history: bool = False,
    record=None,
):
    return resolve_unknown(
        record or execution_record(state=ExecutionState.UNKNOWN, quantity=2, filled_quantity=0),
        orders=list(orders),
        fills=list(fills),
        orders_readable=orders_readable,
        fills_readable=fills_readable,
        fills_are_complete_history=complete_history,
        observed_at=NOW,
        sequence=1,
        source="SIMULATOR",
    )


# ---------------------------------------------------------------------------
# Resolved from an open order
# ---------------------------------------------------------------------------
def test_an_open_order_resolves_an_unknown_to_submitted() -> None:
    resolution = _resolve(orders=[broker_order(status=OrderStatus.SUBMITTED)])
    assert resolution.resolved is True
    assert resolution.state is ExecutionState.SUBMITTED
    assert resolution.event is not None


def test_a_filled_order_resolves_an_unknown_to_filled() -> None:
    resolution = _resolve(
        orders=[broker_order(status=OrderStatus.FILLED, filled=Decimal("2"), quantity=Decimal("2"))]
    )
    assert resolution.resolved is True
    assert resolution.state is ExecutionState.FILLED
    assert resolution.event is not None
    assert resolution.event.filled_quantity == 2


def test_a_partly_filled_order_resolves_to_partially_filled() -> None:
    resolution = _resolve(
        orders=[
            broker_order(
                status=OrderStatus.PARTIALLY_FILLED, filled=Decimal("1"), quantity=Decimal("2")
            )
        ]
    )
    assert resolution.state is ExecutionState.PARTIALLY_FILLED
    assert resolution.event is not None
    assert resolution.event.filled_quantity == 1


def test_a_cancelled_order_resolves_to_cancelled() -> None:
    resolution = _resolve(orders=[broker_order(status=OrderStatus.CANCELLED)])
    assert resolution.resolved is True
    assert resolution.state is ExecutionState.CANCELLED


# ---------------------------------------------------------------------------
# Resolved from fills
# ---------------------------------------------------------------------------
def test_broker_fills_for_the_order_resolve_an_unknown() -> None:
    resolution = _resolve(
        fills=[
            broker_execution(execution_id="exec-1", quantity=Decimal("1")),
            broker_execution(execution_id="exec-2", quantity=Decimal("1")),
        ]
    )
    assert resolution.resolved is True
    assert resolution.state is ExecutionState.FILLED
    assert resolution.event is not None
    assert resolution.event.filled_quantity == 2
    assert len(resolution.event.fills) == 2


def test_fills_short_of_the_order_resolve_to_partially_filled() -> None:
    resolution = _resolve(fills=[broker_execution(quantity=Decimal("1"))])
    assert resolution.state is ExecutionState.PARTIALLY_FILLED
    assert resolution.event is not None
    assert resolution.event.filled_quantity == 1


def test_the_resolving_event_carries_the_brokers_average_price() -> None:
    resolution = _resolve(
        fills=[
            broker_execution(execution_id="exec-1", quantity=Decimal("1"), price=Decimal("6.00")),
            broker_execution(execution_id="exec-2", quantity=Decimal("1"), price=Decimal("5.00")),
        ]
    )
    assert resolution.event is not None
    assert resolution.event.average_fill_price == Decimal("5.5")


# ---------------------------------------------------------------------------
# Not resolved (brief section 92)
# ---------------------------------------------------------------------------
def test_absence_from_the_open_orders_alone_resolves_nothing() -> None:
    """A filled, a cancelled and a never-sent order all look like this."""
    resolution = _resolve(orders=[], fills=[])
    assert resolution.resolved is False
    assert resolution.state is ExecutionState.UNKNOWN
    assert "session-scoped" in resolution.detail


def test_an_unreadable_order_list_resolves_nothing() -> None:
    resolution = _resolve(orders_readable=False)
    assert resolution.resolved is False
    assert "unread list is not an empty one" in resolution.detail


def test_an_execution_with_no_broker_order_id_cannot_be_looked_up() -> None:
    record = execution_record(state=ExecutionState.UNKNOWN, filled_quantity=0)
    record = record.model_copy(update={"broker_order_id": None})
    resolution = _resolve(record=record, orders=[broker_order()])
    assert resolution.resolved is False
    assert "no broker order id" in resolution.detail


def test_absence_resolves_to_cancelled_only_when_the_history_is_complete() -> None:
    """A claim the shipped configuration deliberately does not make."""
    resolution = _resolve(orders=[], fills=[], complete_history=True)
    assert resolution.resolved is True
    assert resolution.state is ExecutionState.CANCELLED
    assert resolution.event is not None
    assert resolution.event.filled_quantity == 0


def test_an_unknown_is_matched_by_order_id_and_never_by_shape() -> None:
    """Two orders for the same contract on the same day are ordinary."""
    resolution = _resolve(orders=[broker_order(broker_order_id="someone-elses")])
    assert resolution.resolved is False


def test_resolving_a_settled_execution_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="resolves ambiguity"):
        _resolve(record=execution_record(state=ExecutionState.FILLED, filled_quantity=2))


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
def test_an_unresolved_unknown_is_reported_and_says_not_to_resubmit(policy) -> None:
    resolution = _resolve(orders=[], fills=[])
    [finding] = unknown_findings([resolution], severity=policy.severity_of)
    assert finding.finding_type is ReconciliationFindingType.UNKNOWN_EXECUTION_UNRESOLVED
    assert finding.severity.value == "CRITICAL"
    assert "Do NOT resubmit" in (finding.recommended_action or "")
    assert "stays committed" in (finding.recommended_action or "")


def test_a_resolved_unknown_is_reported_as_agreement(policy) -> None:
    resolution = _resolve(orders=[broker_order(status=OrderStatus.SUBMITTED)])
    [finding] = unknown_findings([resolution], severity=policy.severity_of)
    assert finding.finding_type is ReconciliationFindingType.UNKNOWN_EXECUTION_RESOLVED
    assert finding.agreement is True
    assert finding.expected_value == "UNKNOWN"
    assert finding.observed_value == "SUBMITTED"


@pytest.mark.parametrize(
    ("orders", "fills", "complete"),
    [
        ([broker_order(status=OrderStatus.SUBMITTED)], [], False),
        ([broker_order(status=OrderStatus.FILLED, filled=Decimal("2"))], [], False),
        ([broker_order(status=OrderStatus.CANCELLED)], [], False),
        ([], [broker_execution()], False),
        ([], [], True),
        ([], [], False),
    ],
)
def test_no_resolution_can_ever_mean_send_it_again(orders, fills, complete) -> None:
    """Every reachable outcome is a state the broker turned out to be in.

    ``SUBMISSION_PENDING`` is the state that would mean "send it": it is
    unreachable from here whatever the broker says, which is what makes
    resolution-by-observation structurally different from a retry.
    """
    resolution = _resolve(orders=orders, fills=fills, complete_history=complete)
    assert resolution.state is not ExecutionState.SUBMISSION_PENDING
    assert resolution.state in {
        ExecutionState.UNKNOWN,
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.FILLED,
        ExecutionState.CANCELLED,
    }
