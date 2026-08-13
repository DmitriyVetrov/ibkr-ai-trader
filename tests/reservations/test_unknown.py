"""An UNKNOWN execution never releases its capital (brief sections 22, 66, 78).

The single most important rule in the milestone. An execution whose outcome was
never learned may be a live order at the broker right now; releasing its
capital and letting the campaign authorise it again is exactly how one
intention becomes two positions.

Three ways it is enforced, all tested here:

* the lifecycle refuses to release while any attempt is unresolved;
* the configuration key that would permit it fails to load;
* the campaign's available capital stays reduced until the ambiguity is
  settled.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.positions.factories import execution_record, reservation
from trading_system.domain.enums import (
    ExecutionState,
    ReservationReasonCode,
    ReservationState,
)
from trading_system.infrastructure.settings import ReservationPolicyConfig
from trading_system.reservations.lifecycle import resolve_reservation

pytestmark = pytest.mark.unit

AUTHORIZED = Decimal("1190.00")


def _held():
    return reservation(authorized=AUTHORIZED, quantity=2)


def test_an_unknown_execution_keeps_its_capital(policy) -> None:
    outcome = resolve_reservation(
        _held(),
        [execution_record(state=ExecutionState.UNKNOWN, filled_quantity=0)],
        policy=policy,
    )
    assert outcome.state is ReservationState.UNKNOWN
    assert outcome.reason_code is ReservationReasonCode.EXECUTION_UNKNOWN
    assert outcome.released_delta == Decimal("0")


def test_an_unknown_reservation_says_why_it_is_held(policy) -> None:
    outcome = resolve_reservation(
        _held(), [execution_record(state=ExecutionState.UNKNOWN)], policy=policy
    )
    assert "may be live at the broker" in outcome.detail
    assert "elapsed time" in outcome.detail


def test_one_unknown_attempt_blocks_release_for_every_other_attempt(policy) -> None:
    """Even a sibling the broker definitely refused does not free the capital."""
    outcome = resolve_reservation(
        _held(),
        [
            execution_record(execution_id="execution-a", state=ExecutionState.REJECTED),
            execution_record(execution_id="execution-b", state=ExecutionState.UNKNOWN),
        ],
        policy=policy,
    )
    assert outcome.state is ReservationState.UNKNOWN
    assert outcome.released_delta == Decimal("0")


def test_an_unknown_attempt_alongside_a_partial_fill_consumes_but_never_releases(
    policy,
) -> None:
    outcome = resolve_reservation(
        _held(),
        [
            execution_record(
                execution_id="execution-a",
                state=ExecutionState.PARTIALLY_FILLED,
                quantity=2,
                filled_quantity=1,
                average_fill_price=Decimal("5.90"),
            ),
            execution_record(execution_id="execution-b", state=ExecutionState.UNKNOWN),
        ],
        policy=policy,
    )
    assert outcome.state is ReservationState.UNKNOWN
    assert outcome.consumed_delta == Decimal("590.00")
    assert outcome.released_delta == Decimal("0")


def test_resolving_an_unknown_to_filled_finally_consumes_the_capital(policy) -> None:
    """The whole point of resolving by observation: the capital can then move."""
    held = _held()
    unresolved = resolve_reservation(
        held, [execution_record(state=ExecutionState.UNKNOWN)], policy=policy
    )
    assert unresolved.state is ReservationState.UNKNOWN

    resolved = resolve_reservation(
        held,
        [
            execution_record(
                state=ExecutionState.FILLED, filled_quantity=2, average_fill_price=Decimal("5.95")
            )
        ],
        policy=policy,
    )
    assert resolved.state is ReservationState.CONSUMED
    assert resolved.consumed_delta == Decimal("1190.00")


def test_resolving_an_unknown_to_cancelled_finally_releases_the_capital(policy) -> None:
    held = _held()
    resolved = resolve_reservation(
        held,
        [execution_record(state=ExecutionState.CANCELLED, filled_quantity=0)],
        policy=policy,
    )
    assert resolved.state is ReservationState.RELEASED
    assert resolved.released_delta == AUTHORIZED


def test_the_configuration_that_would_release_an_unknown_fails_to_load() -> None:
    """There is no supported way to switch this off."""
    with pytest.raises(ValidationError, match="one intention becomes two positions"):
        ReservationPolicyConfig(release_on_unknown=True)


def test_the_configuration_that_would_free_an_unexecuted_authorisation_fails_to_load() -> None:
    with pytest.raises(ValidationError, match="authorised twice"):
        ReservationPolicyConfig(release_when_never_executed=True)


def test_an_unknown_execution_keeps_constraining_the_campaign(service, execution_store) -> None:
    """Brief section 66: available budget does not spring back to the full envelope."""
    service.sync()
    [held] = service.all()
    execution_store.seed(
        execution_record(
            allocation_id=held.allocation_id,
            state=ExecutionState.UNKNOWN,
            filled_quantity=0,
        )
    )

    service.apply_executions()
    capital = service.capital()

    assert capital.unknown_count == 1
    assert capital.locked_by_unknown == held.authorized_amount
    assert capital.available == capital.allocatable - held.authorized_amount
    assert capital.constrained_by_uncertainty is True
