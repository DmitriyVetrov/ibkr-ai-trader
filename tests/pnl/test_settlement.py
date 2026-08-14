"""When committed capital may return to the campaign, and when it may not.

The single most damaging thing this milestone could do is return capital on
weaker evidence than broker-confirmed closure. Every test here is about that:

* a confirmed closure settles, exactly once;
* an ``UNKNOWN`` execution never settles, under any configuration;
* a partial exit settles only its matched fraction;
* a critical reconciliation finding stops it;
* a result nobody could compute stops it;
* running settlement twice moves capital once.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.pnl import factories
from tests.pnl.factories import EXIT_AT
from tests.pnl.test_calculator import compute
from trading_system.domain.enums import (
    ReservationReasonCode,
    ReservationState,
    SettlementBlockReason,
    SettlementStatus,
)
from trading_system.infrastructure.settings import PnLSettlementConfig
from trading_system.pnl.settlement import (
    SettlementInputs,
    build_settlement,
    settle,
    settlement_event,
)

pytestmark = pytest.mark.unit


def inputs(**overrides):
    """A settlement decision with every condition satisfied, then overridden."""
    fields = {
        "reservation": factories.reservation(),
        "position_id": factories.POSITION,
        "closure_confirmed": True,
        "broker_read_usable": True,
        "execution_unknown": False,
        "realized": compute(factories.entry_fills(), factories.exit_fills()),
        "reconciliation_findings": (),
        "policy": PnLSettlementConfig(),
    }
    fields.update(overrides)
    return SettlementInputs(**fields)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_confirmed_closure_returns_the_committed_capital() -> None:
    outcome = settle(inputs())

    assert outcome.status is SettlementStatus.SETTLED
    assert outcome.settled_delta == Decimal("1210.00")
    assert outcome.state is ReservationState.SETTLED
    assert outcome.reason_code is ReservationReasonCode.POSITION_CLOSED_CONFIRMED


def test_the_settlement_records_both_sides_of_the_movement() -> None:
    reservation = factories.reservation()
    outcome = settle(inputs(reservation=reservation))
    settlement = build_settlement(
        reservation,
        outcome,
        position_id=factories.POSITION,
        pnl_id="pnl-1",
        settled_at=EXIT_AT,
    )

    assert settlement.committed_before == Decimal("1210.00")
    assert settlement.settled_amount == Decimal("1210.00")
    assert settlement.committed_after == Decimal("0")
    assert settlement.realized_pnl == Decimal("397.00")


def test_the_reservation_stops_committing_capital_once_settled() -> None:
    """The whole point: available campaign capital actually goes back up."""
    reservation = factories.reservation()
    assert reservation.committed_amount == Decimal("1210.00")

    outcome = settle(inputs(reservation=reservation))
    settlement = build_settlement(
        reservation,
        outcome,
        position_id=factories.POSITION,
        pnl_id="pnl-1",
        settled_at=EXIT_AT,
    )
    event = settlement_event(
        reservation, outcome, settlement, sequence=0, occurred_at=EXIT_AT, observed_at=EXIT_AT
    )
    settled = reservation.with_event(event)

    assert settled.state is ReservationState.SETTLED
    assert settled.committed_amount == Decimal("0")
    # The consumption is NOT erased. What was spent stays recorded as spent.
    assert settled.consumed_amount == Decimal("1210.00")
    assert settled.settled_amount == Decimal("1210.00")
    assert settled.realized_pnl == Decimal("397.00")


# ---------------------------------------------------------------------------
# UNKNOWN never releases capital
# ---------------------------------------------------------------------------
def test_an_unknown_execution_never_settles() -> None:
    outcome = settle(inputs(execution_unknown=True))

    assert outcome.status is SettlementStatus.BLOCKED
    assert outcome.block_reason is SettlementBlockReason.EXECUTION_UNKNOWN
    assert outcome.settled_delta == Decimal("0")


def test_an_unknown_reservation_never_settles() -> None:
    unknown = factories.reservation(
        consumed=Decimal("0"), authorized=Decimal("1210.00"), state="UNKNOWN"
    )
    outcome = settle(inputs(reservation=unknown))

    assert outcome.status is SettlementStatus.BLOCKED
    assert outcome.block_reason is SettlementBlockReason.EXECUTION_UNKNOWN


def test_no_configuration_permits_releasing_an_unknown_execution() -> None:
    """``release_on_unknown: true`` fails to load, exactly as in reconciliation."""
    with pytest.raises(ValueError, match="release_on_unknown"):
        PnLSettlementConfig(release_on_unknown=True)


def test_the_reservation_model_refuses_a_settled_unknown() -> None:
    """Enforced in the ledger as well as in the decision.

    Two refusals for one irreversible movement, deliberately: the settlement
    engine declines to produce the outcome, and the record itself cannot be
    constructed even if something else tried to write one.
    """
    # An unresolved execution that DID partly fill: capital was genuinely spent,
    # so the "nothing settles that was never spent" bound does not apply and
    # the UNKNOWN rule is the one under test.
    partly_filled_but_unresolved = factories.reservation(
        authorized=Decimal("1210.00"), consumed=Decimal("605.00"), state="UNKNOWN"
    )
    payload = partly_filled_but_unresolved.model_dump() | {
        "settled_amount": Decimal("100.00"),
        "settled_at": EXIT_AT,
    }

    with pytest.raises(ValueError, match="UNKNOWN"):
        type(partly_filled_but_unresolved).model_validate(payload)


def test_a_settlement_larger_than_the_consumption_is_refused() -> None:
    """Returning more than went out would grow the envelope by an unauthorised
    amount, and the campaign would fund a trade against capital it never had."""
    reservation = factories.reservation(
        authorized=Decimal("1210.00"), consumed=Decimal("605.00"), state="PARTIALLY_CONSUMED"
    )
    payload = reservation.model_dump() | {
        "settled_amount": Decimal("1000.00"),
        "settled_at": EXIT_AT,
    }

    with pytest.raises(ValueError, match="Only spent capital can come back"):
        type(reservation).model_validate(payload)


# ---------------------------------------------------------------------------
# Closure has to be confirmed
# ---------------------------------------------------------------------------
def test_an_unconfirmed_closure_does_not_settle() -> None:
    outcome = settle(inputs(closure_confirmed=False))

    assert outcome.status is SettlementStatus.BLOCKED
    assert outcome.block_reason is SettlementBlockReason.CLOSURE_NOT_CONFIRMED


def test_a_failed_broker_read_does_not_settle() -> None:
    """'We could not look' is not 'there is nothing there'."""
    outcome = settle(inputs(broker_read_usable=False))

    assert outcome.status is SettlementStatus.BLOCKED
    assert outcome.block_reason is SettlementBlockReason.CLOSURE_NOT_CONFIRMED


def test_no_configuration_permits_settling_an_unconfirmed_closure() -> None:
    with pytest.raises(ValueError, match="require_broker_confirmed_closure"):
        PnLSettlementConfig(require_broker_confirmed_closure=False)


# ---------------------------------------------------------------------------
# The other refusals
# ---------------------------------------------------------------------------
def test_an_unavailable_result_does_not_settle() -> None:
    """What came back is not a known quantity, so nothing is credited."""
    unavailable = compute(factories.entry_fills(multiplier=None), factories.exit_fills())
    outcome = settle(inputs(realized=unavailable))

    assert outcome.status is SettlementStatus.BLOCKED
    assert outcome.block_reason is SettlementBlockReason.PNL_UNAVAILABLE


def test_a_critical_reconciliation_finding_blocks_settlement() -> None:
    outcome = settle(inputs(reconciliation_findings=("POSITION_QUANTITY_MISMATCH",)))

    assert outcome.status is SettlementStatus.BLOCKED
    assert outcome.block_reason is SettlementBlockReason.RECONCILIATION_MISMATCH


def test_a_blocked_settlement_names_the_missing_evidence() -> None:
    """'Blocked' alone is not something an operator can act on."""
    for overrides in (
        {"execution_unknown": True},
        {"closure_confirmed": False},
        {"reconciliation_findings": ("POSITION_QUANTITY_MISMATCH",)},
    ):
        outcome = settle(inputs(**overrides))
        assert outcome.block_reason is not None
        assert len(outcome.detail) > 40


def test_a_reservation_that_consumed_nothing_has_nothing_to_settle() -> None:
    reserved = factories.reservation(
        consumed=Decimal("0"), authorized=Decimal("1210.00"), state="RESERVED"
    )
    outcome = settle(inputs(reservation=reserved))

    assert outcome.status is SettlementStatus.NOT_APPLICABLE
    assert outcome.settled_delta == Decimal("0")


# ---------------------------------------------------------------------------
# Partial closure
# ---------------------------------------------------------------------------
def test_a_partial_exit_settles_only_the_closed_fraction() -> None:
    """Two authorised, one closed: half the capital comes back."""
    partial = compute(factories.entry_fills(quantity=2), factories.exit_fills(quantity=1))
    outcome = settle(inputs(realized=partial))

    assert outcome.status is SettlementStatus.PARTIALLY_SETTLED
    assert outcome.settled_delta == Decimal("605.00")
    assert outcome.state is not ReservationState.SETTLED


def test_a_partial_settlement_leaves_the_rest_committed() -> None:
    reservation = factories.reservation()
    partial = compute(factories.entry_fills(quantity=2), factories.exit_fills(quantity=1))
    outcome = settle(inputs(reservation=reservation, realized=partial))
    settlement = build_settlement(
        reservation,
        outcome,
        position_id=factories.POSITION,
        pnl_id=partial.pnl_id,
        settled_at=EXIT_AT,
    )

    assert settlement.committed_after == Decimal("605.00")
    assert settlement.status is SettlementStatus.PARTIALLY_SETTLED


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_settling_an_already_settled_reservation_moves_nothing() -> None:
    reservation = factories.reservation()
    first = settle(inputs(reservation=reservation))
    settlement = build_settlement(
        reservation, first, position_id=factories.POSITION, pnl_id="pnl-1", settled_at=EXIT_AT
    )
    settled = reservation.with_event(
        settlement_event(
            reservation,
            first,
            settlement,
            sequence=0,
            occurred_at=EXIT_AT,
            observed_at=EXIT_AT,
        )
    )

    second = settle(inputs(reservation=settled))

    assert second.status is SettlementStatus.ALREADY_SETTLED
    assert second.settled_delta == Decimal("0")
    assert not second.moved


def test_the_same_evidence_derives_the_same_settlement_identity() -> None:
    """What makes the settlement job safe to run every fifteen minutes."""
    reservation = factories.reservation()
    outcome = settle(inputs(reservation=reservation))
    first = build_settlement(
        reservation, outcome, position_id=factories.POSITION, pnl_id="pnl-1", settled_at=EXIT_AT
    )
    second = build_settlement(
        reservation, outcome, position_id=factories.POSITION, pnl_id="pnl-1", settled_at=EXIT_AT
    )

    assert first.settlement_id == second.settlement_id


def test_a_replayed_settlement_event_has_the_same_id() -> None:
    """The reservation ledger recognises it and appends nothing."""
    reservation = factories.reservation()
    outcome = settle(inputs(reservation=reservation))
    settlement = build_settlement(
        reservation, outcome, position_id=factories.POSITION, pnl_id="pnl-1", settled_at=EXIT_AT
    )
    first = settlement_event(
        reservation, outcome, settlement, sequence=0, occurred_at=EXIT_AT, observed_at=EXIT_AT
    )
    second = settlement_event(
        reservation, outcome, settlement, sequence=0, occurred_at=EXIT_AT, observed_at=EXIT_AT
    )

    assert first.event_id == second.event_id


# ---------------------------------------------------------------------------
# What settlement is not
# ---------------------------------------------------------------------------
def test_a_settlement_never_returns_more_than_was_consumed() -> None:
    """The bound that stops a campaign quietly growing its own budget."""
    reservation = factories.reservation(authorized=Decimal("1210.00"), consumed=Decimal("1210.00"))
    outcome = settle(inputs(reservation=reservation))

    assert outcome.settled_delta <= reservation.consumed_amount


def test_realized_profit_is_not_added_to_the_campaign_envelope() -> None:
    """A winning trade does not silently grow the budget. Off by default."""
    assert PnLSettlementConfig().return_realized_pnl_to_campaign is False


def test_the_settlement_is_capital_not_proceeds() -> None:
    """1,210 went out and 1,610 came back; 1,210 returns and 400 is the result."""
    reservation = factories.reservation()
    outcome = settle(inputs(reservation=reservation))

    assert outcome.settled_delta == Decimal("1210.00")
    assert outcome.realized_pnl == Decimal("397.00")
