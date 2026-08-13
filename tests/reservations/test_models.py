"""The reservation model, and what it refuses to represent (brief sections 19, 41-43).

The claims under test:

* ``RESERVED`` is not ``INVESTED`` and ``UNKNOWN`` is not ``RELEASED``;
* the accounting balances exactly, in decimal, on every record;
* consuming past the authorisation requires recorded evidence;
* the authorised figure and the executed figure are both kept.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.positions.factories import NOW, reservation
from trading_system.domain.enums import (
    ReservationEventType,
    ReservationReasonCode,
    ReservationState,
    StrategyType,
)
from trading_system.reservations.models import (
    CampaignCapital,
    Reservation,
    ReservationEvent,
    reservation_identifier,
)

pytestmark = pytest.mark.unit


def _reservation(**kwargs) -> Reservation:
    return reservation(**kwargs)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_one_authorisation_gives_one_reservation_id() -> None:
    """Replaying the allocation ledger must not create a second reservation."""
    first = reservation_identifier(
        campaign_id="campaign-001", allocation_id="allocation-1", opportunity_id="opportunity-1"
    )
    second = reservation_identifier(
        campaign_id="campaign-001", allocation_id="allocation-1", opportunity_id="opportunity-1"
    )
    assert first == second


def test_a_different_authorisation_gives_a_different_reservation() -> None:
    first = reservation_identifier(
        campaign_id="campaign-001", allocation_id="allocation-1", opportunity_id="opportunity-1"
    )
    second = reservation_identifier(
        campaign_id="campaign-001", allocation_id="allocation-2", opportunity_id="opportunity-2"
    )
    assert first != second


# ---------------------------------------------------------------------------
# The accounting identity (brief section 43)
# ---------------------------------------------------------------------------
def test_a_fresh_reservation_holds_everything_it_was_authorised() -> None:
    held = _reservation(authorized=Decimal("1190.00"))
    assert held.remaining_amount == Decimal("1190.00")
    assert held.consumed_amount == Decimal("0")
    assert held.released_amount == Decimal("0")
    assert held.committed_amount == Decimal("1190.00")


def test_capital_that_is_neither_spent_returned_nor_committed_is_refused() -> None:
    with pytest.raises(ValidationError, match="lost track of"):
        Reservation(
            reservation_id="reservation-1",
            campaign_id="campaign-001",
            allocation_id="allocation-1",
            opportunity_id="opportunity-1",
            symbol="NVDA",
            strategy=StrategyType.LONG_CALL,
            currency="EUR",
            authorized_amount=Decimal("1000.00"),
            authorized_quantity=2,
            authorized_at=NOW,
            consumed_amount=Decimal("100.00"),
            released_amount=Decimal("0"),
            remaining_amount=Decimal("100.00"),
            created_at=NOW,
            updated_at=NOW,
        )


def test_consuming_past_the_authorisation_needs_recorded_evidence() -> None:
    with pytest.raises(ValidationError, match="broker correction"):
        Reservation(
            reservation_id="reservation-1",
            campaign_id="campaign-001",
            allocation_id="allocation-1",
            opportunity_id="opportunity-1",
            symbol="NVDA",
            strategy=StrategyType.LONG_CALL,
            currency="EUR",
            authorized_amount=Decimal("1000.00"),
            authorized_quantity=2,
            authorized_at=NOW,
            consumed_amount=Decimal("1100.00"),
            over_authorized_amount=Decimal("100.00"),
            remaining_amount=Decimal("0"),
            state=ReservationState.CONSUMED,
            created_at=NOW,
            updated_at=NOW,
        )


def test_an_overrun_with_a_broker_correction_is_accepted_and_visible() -> None:
    held = Reservation(
        reservation_id="reservation-1",
        campaign_id="campaign-001",
        allocation_id="allocation-1",
        opportunity_id="opportunity-1",
        symbol="NVDA",
        strategy=StrategyType.LONG_CALL,
        currency="EUR",
        authorized_amount=Decimal("1000.00"),
        authorized_quantity=2,
        authorized_at=NOW,
        consumed_amount=Decimal("1100.00"),
        over_authorized_amount=Decimal("100.00"),
        remaining_amount=Decimal("0"),
        state=ReservationState.CONSUMED,
        reason_codes=[ReservationReasonCode.BROKER_CORRECTION],
        created_at=NOW,
        updated_at=NOW,
    )
    assert held.over_authorized_amount == Decimal("100.00")


# ---------------------------------------------------------------------------
# State agreement (brief section 19)
# ---------------------------------------------------------------------------
def test_reserved_means_nothing_has_moved() -> None:
    with pytest.raises(ValidationError, match="is RESERVED but"):
        Reservation(
            reservation_id="reservation-1",
            campaign_id="campaign-001",
            allocation_id="allocation-1",
            opportunity_id="opportunity-1",
            symbol="NVDA",
            strategy=StrategyType.LONG_CALL,
            currency="EUR",
            authorized_amount=Decimal("1000.00"),
            authorized_quantity=2,
            authorized_at=NOW,
            consumed_amount=Decimal("400.00"),
            remaining_amount=Decimal("600.00"),
            state=ReservationState.RESERVED,
            created_at=NOW,
            updated_at=NOW,
        )


def test_released_is_a_claim_that_nothing_was_spent() -> None:
    with pytest.raises(ValidationError, match="is RELEASED but"):
        Reservation(
            reservation_id="reservation-1",
            campaign_id="campaign-001",
            allocation_id="allocation-1",
            opportunity_id="opportunity-1",
            symbol="NVDA",
            strategy=StrategyType.LONG_CALL,
            currency="EUR",
            authorized_amount=Decimal("1000.00"),
            authorized_quantity=2,
            authorized_at=NOW,
            consumed_amount=Decimal("400.00"),
            released_amount=Decimal("600.00"),
            remaining_amount=Decimal("0"),
            state=ReservationState.RELEASED,
            created_at=NOW,
            updated_at=NOW,
        )


def test_unknown_must_still_hold_capital() -> None:
    """The failure this exists to prevent: an ambiguous order with a spent budget."""
    with pytest.raises(ValidationError, match="holds no committed capital"):
        Reservation(
            reservation_id="reservation-1",
            campaign_id="campaign-001",
            allocation_id="allocation-1",
            opportunity_id="opportunity-1",
            symbol="NVDA",
            strategy=StrategyType.LONG_CALL,
            currency="EUR",
            authorized_amount=Decimal("1000.00"),
            authorized_quantity=2,
            authorized_at=NOW,
            released_amount=Decimal("1000.00"),
            remaining_amount=Decimal("0"),
            state=ReservationState.UNKNOWN,
            reason_codes=[ReservationReasonCode.EXECUTION_UNKNOWN],
            created_at=NOW,
            updated_at=NOW,
        )


def test_unknown_must_say_so_in_its_reason_codes() -> None:
    with pytest.raises(ValidationError, match="without saying so"):
        Reservation(
            reservation_id="reservation-1",
            campaign_id="campaign-001",
            allocation_id="allocation-1",
            opportunity_id="opportunity-1",
            symbol="NVDA",
            strategy=StrategyType.LONG_CALL,
            currency="EUR",
            authorized_amount=Decimal("1000.00"),
            authorized_quantity=2,
            authorized_at=NOW,
            remaining_amount=Decimal("1000.00"),
            state=ReservationState.UNKNOWN,
            created_at=NOW,
            updated_at=NOW,
        )


def test_committed_capital_includes_what_is_unresolved() -> None:
    held = _reservation(authorized=Decimal("1000.00")).model_copy(
        update={
            "state": ReservationState.UNKNOWN,
            "reason_codes": [ReservationReasonCode.EXECUTION_UNKNOWN],
        }
    )
    assert held.committed is True
    assert held.locked_by_uncertainty is True
    assert held.committed_amount == Decimal("1000.00")


# ---------------------------------------------------------------------------
# Folding events
# ---------------------------------------------------------------------------
def test_an_event_moves_money_by_deltas_not_by_overwriting() -> None:
    held = _reservation(authorized=Decimal("1000.00"))
    consumed = held.with_event(
        ReservationEvent(
            event_id="resevt-1",
            reservation_id=held.reservation_id,
            sequence=0,
            event_type=ReservationEventType.RESERVATION_PARTIALLY_CONSUMED,
            state=ReservationState.PARTIALLY_CONSUMED,
            occurred_at=NOW,
            observed_at=NOW,
            source="test",
            consumed_delta=Decimal("400.00"),
            reason_code=ReservationReasonCode.PARTIALLY_FILLED,
        )
    )
    assert consumed.consumed_amount == Decimal("400.00")
    assert consumed.remaining_amount == Decimal("600.00")
    assert consumed.authorized_amount == Decimal("1000.00")


def test_an_event_for_another_reservation_is_refused() -> None:
    held = _reservation()
    with pytest.raises(ValueError, match="belongs to reservation"):
        held.with_event(
            ReservationEvent(
                event_id="resevt-1",
                reservation_id="reservation-elsewhere",
                sequence=0,
                event_type=ReservationEventType.RESERVATION_OBSERVED,
                state=ReservationState.RESERVED,
                occurred_at=NOW,
                observed_at=NOW,
                source="test",
            )
        )


def test_folding_an_event_that_would_break_the_accounting_raises() -> None:
    held = _reservation(authorized=Decimal("1000.00"))
    with pytest.raises(ValidationError):
        held.with_event(
            ReservationEvent(
                event_id="resevt-1",
                reservation_id=held.reservation_id,
                sequence=0,
                event_type=ReservationEventType.RESERVATION_CONSUMED,
                state=ReservationState.CONSUMED,
                occurred_at=NOW,
                observed_at=NOW,
                source="test",
                consumed_delta=Decimal("5000.00"),
                reason_code=ReservationReasonCode.FILLED,
            )
        )


# ---------------------------------------------------------------------------
# The campaign view
# ---------------------------------------------------------------------------
def test_available_is_the_budget_less_the_reserve_less_what_is_committed() -> None:
    capital = CampaignCapital(
        campaign_id="campaign-001",
        as_of=NOW,
        currency="EUR",
        budget=Decimal("5000.00"),
        reserve=Decimal("1000.00"),
        committed_total=Decimal("1190.00"),
        available=Decimal("2810.00"),
    )
    assert capital.available == Decimal("2810.00")
    assert capital.allocatable == Decimal("4000.00")


def test_a_campaign_view_that_does_not_add_up_is_refused() -> None:
    with pytest.raises(ValidationError, match="is not budget"):
        CampaignCapital(
            campaign_id="campaign-001",
            as_of=NOW,
            currency="EUR",
            budget=Decimal("5000.00"),
            reserve=Decimal("1000.00"),
            committed_total=Decimal("1190.00"),
            available=Decimal("4000.00"),
        )


def test_capital_locked_by_uncertainty_is_reported_separately() -> None:
    """The figure an operator most needs, and most easily mistakes for available."""
    capital = CampaignCapital(
        campaign_id="campaign-001",
        as_of=NOW,
        currency="EUR",
        budget=Decimal("5000.00"),
        reserve=Decimal("1000.00"),
        committed_total=Decimal("1000.00"),
        locked_by_unknown=Decimal("1000.00"),
        available=Decimal("3000.00"),
        unknown_count=1,
    )
    assert capital.constrained_by_uncertainty is True
