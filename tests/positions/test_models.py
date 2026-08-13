"""The position models, and what they refuse to represent (brief sections 5-7, 44-49).

The claims under test are the ones a plausible-looking change would break:

* a failed broker read cannot wear the shape of an empty account;
* identity comes from the broker's contract id, and a weaker key says so;
* an unavailable value stays ``None`` and never becomes zero;
* the three price units are kept apart, and a conversion that needs a
  multiplier nobody reported yields ``None`` rather than assuming 100.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError

from tests.positions.factories import (
    ACCOUNT,
    CALL_CONTRACT_ID,
    EXPIRATION,
    MASKED,
    NOW,
    option_position,
    stock_position,
)
from trading_system.domain.enums import (
    AcquisitionProvenance,
    BrokerReadStatus,
    OptionRight,
    OrderSide,
    SecurityType,
    StructureStatus,
    TradingMode,
)
from trading_system.positions.models import (
    BrokerPositionSnapshot,
    ExpectedPosition,
    ObservedFill,
    StrategyLegPosition,
    StrategyPosition,
    contract_key,
    fill_identifier,
    mask_account,
    position_identifier,
)
from trading_system.positions.snapshot import (
    build_position_snapshot,
    to_observed_position,
    unavailable_snapshot,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Account privacy (brief section 31)
# ---------------------------------------------------------------------------
def test_an_account_number_is_masked_to_its_last_characters() -> None:
    assert mask_account("DU1234567") == MASKED


def test_a_short_account_number_is_masked_entirely() -> None:
    """Never leak the whole thing just because it is short."""
    assert mask_account("DU12") == "****"


def test_an_absent_account_is_named_as_unknown_not_as_an_empty_string() -> None:
    assert mask_account(None) == "(unknown)"
    assert mask_account("   ") == "(unknown)"


def test_no_position_artifact_stores_the_full_account_number() -> None:
    snapshot = build_position_snapshot(
        [option_position()],
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
    )
    payload = snapshot.model_dump_json()
    assert ACCOUNT not in payload
    assert MASKED in payload


# ---------------------------------------------------------------------------
# Identity (brief section 6)
# ---------------------------------------------------------------------------
def test_the_broker_contract_id_is_the_identity_when_there_is_one() -> None:
    key = contract_key(
        contract_id=CALL_CONTRACT_ID,
        symbol="NVDA",
        security_type=SecurityType.OPTION,
        expiration=EXPIRATION,
        strike=Decimal("180"),
        right=OptionRight.CALL,
    )
    assert key == f"cid:{CALL_CONTRACT_ID}"


def test_two_contracts_sharing_human_readable_terms_do_not_collide() -> None:
    """The case this exists for: an adjusted option after a corporate action."""
    original = contract_key(
        contract_id=1,
        symbol="NVDA",
        security_type=SecurityType.OPTION,
        expiration=EXPIRATION,
        strike=Decimal("180"),
        right=OptionRight.CALL,
    )
    adjusted = contract_key(
        contract_id=2,
        symbol="NVDA",
        security_type=SecurityType.OPTION,
        expiration=EXPIRATION,
        strike=Decimal("180"),
        right=OptionRight.CALL,
    )
    assert original != adjusted


def test_the_fallback_key_can_never_be_mistaken_for_a_contract_id_key() -> None:
    fallback = contract_key(
        contract_id=None,
        symbol="NVDA",
        security_type=SecurityType.OPTION,
        expiration=EXPIRATION,
        strike=Decimal("180"),
        right=OptionRight.CALL,
    )
    assert fallback.startswith("sym:")


def test_a_position_keyed_on_the_weaker_form_records_that_it_was() -> None:
    position = to_observed_position(
        option_position(contract_id=None), observed_at=NOW, account_reference=MASKED
    )
    assert position.identified_by_contract_id is False
    assert position.key.startswith("sym:")


def test_the_same_holding_keeps_one_id_across_snapshots() -> None:
    """Which is what lets a per-contract history be assembled at all."""
    first = to_observed_position(option_position(), observed_at=NOW, account_reference=MASKED)
    later = to_observed_position(
        option_position(as_of=datetime(2026, 8, 11, tzinfo=UTC), quantity=Decimal("3")),
        observed_at=datetime(2026, 8, 11, tzinfo=UTC),
        account_reference=MASKED,
    )
    assert first.position_id == later.position_id


def test_an_expected_and_an_observed_position_share_an_id_for_one_instrument() -> None:
    """The property that makes reconciliation a lookup rather than a fuzzy match."""
    observed = to_observed_position(option_position(), observed_at=NOW, account_reference=MASKED)
    derived = position_identifier(account_reference=MASKED, key=observed.key)
    assert derived == observed.position_id


# ---------------------------------------------------------------------------
# A failed read is not an empty account (brief sections 54-55)
# ---------------------------------------------------------------------------
def test_an_unavailable_snapshot_is_not_usable_and_holds_nothing() -> None:
    snapshot = unavailable_snapshot(
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
        status=BrokerReadStatus.UNAVAILABLE,
        detail="the gateway refused the connection",
    )
    assert snapshot.usable is False
    assert snapshot.positions == []
    assert snapshot.read_status is BrokerReadStatus.UNAVAILABLE


def test_an_empty_broker_answer_is_usable_and_says_so() -> None:
    snapshot = build_position_snapshot(
        [],
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
    )
    assert snapshot.read_status is BrokerReadStatus.EMPTY
    assert snapshot.usable is True


def test_a_failed_read_cannot_be_constructed_carrying_positions() -> None:
    with pytest.raises(ValidationError, match="failed read must not carry data"):
        BrokerPositionSnapshot(
            snapshot_id="positions-1",
            account_reference=MASKED,
            broker="SIMULATOR",
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
            read_status=BrokerReadStatus.UNAVAILABLE,
            positions=[
                to_observed_position(option_position(), observed_at=NOW, account_reference=MASKED)
            ],
            content_hash="abc",
            detail="unreachable",
        )


def test_a_failed_read_must_say_why() -> None:
    with pytest.raises(ValidationError, match="different facts"):
        BrokerPositionSnapshot(
            snapshot_id="positions-1",
            account_reference=MASKED,
            broker="SIMULATOR",
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
            read_status=BrokerReadStatus.TIMEOUT,
            content_hash="abc",
        )


def test_the_unavailable_constructor_refuses_to_record_a_successful_read() -> None:
    with pytest.raises(ValueError, match="claims the broker answered"):
        unavailable_snapshot(
            broker="SIMULATOR",
            account_id=ACCOUNT,
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
            status=BrokerReadStatus.EMPTY,
            detail="nothing",
        )


def test_a_snapshot_marked_ok_must_carry_positions() -> None:
    with pytest.raises(ValidationError, match="An account that genuinely"):
        BrokerPositionSnapshot(
            snapshot_id="positions-1",
            account_reference=MASKED,
            broker="SIMULATOR",
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
            read_status=BrokerReadStatus.OK,
            content_hash="abc",
        )


# ---------------------------------------------------------------------------
# Nothing is invented (brief sections 47-49)
# ---------------------------------------------------------------------------
def test_a_missing_market_value_stays_none() -> None:
    position = to_observed_position(
        option_position(market_value=None, unrealized_pnl=None),
        observed_at=NOW,
        account_reference=MASKED,
    )
    assert position.market_value is None
    assert position.unrealized_pnl is None


def test_a_missing_average_cost_stays_none() -> None:
    position = to_observed_position(
        option_position(average_cost=None), observed_at=NOW, account_reference=MASKED
    )
    assert position.average_cost is None


def test_a_snapshot_records_the_brokers_own_submitted_order_count() -> None:
    snapshot = build_position_snapshot(
        [option_position()],
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
        orders_submitted=0,
    )
    assert snapshot.orders_submitted == 0


def test_a_snapshot_that_claimed_a_submitted_order_fails_to_construct() -> None:
    with pytest.raises(ValidationError, match="read-only capture"):
        build_position_snapshot(
            [option_position()],
            broker="SIMULATOR",
            account_id=ACCOUNT,
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
            orders_submitted=1,
        )


# ---------------------------------------------------------------------------
# Position invariants (brief section 44)
# ---------------------------------------------------------------------------
def test_a_non_finite_quantity_is_refused_at_the_type() -> None:
    """A position size that is not a number fails as early as it possibly can."""
    with pytest.raises(ValidationError, match="finite"):
        option_position(quantity=Decimal("NaN"))


def test_an_option_position_without_its_terms_is_refused_by_the_broker_model() -> None:
    """The Milestone 2 model already refuses this; the M9 model inherits the rule."""
    with pytest.raises(ValidationError, match="missing"):
        option_position(right=_no_right())


def test_a_short_position_keeps_its_sign() -> None:
    position = to_observed_position(
        option_position(quantity=Decimal("-2")), observed_at=NOW, account_reference=MASKED
    )
    assert position.is_short
    assert position.quantity == Decimal("-2")


def test_a_snapshot_refuses_two_records_for_one_instrument() -> None:
    with pytest.raises(ValidationError, match="same instrument twice"):
        build_position_snapshot(
            [option_position(), option_position(quantity=Decimal("3"))],
            broker="SIMULATOR",
            account_id=ACCOUNT,
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
        )


# ---------------------------------------------------------------------------
# Units (brief section 46)
# ---------------------------------------------------------------------------
def test_a_fills_gross_cost_includes_the_multiplier() -> None:
    fill = _fill(price=Decimal("6.05"), quantity=Decimal("2"), multiplier=100)
    assert fill.price == Decimal("6.05")
    assert fill.gross_cost == Decimal("1210.00")


def test_a_fill_with_no_multiplier_reports_no_money_rather_than_assuming_100() -> None:
    fill = _fill(price=Decimal("6.05"), quantity=Decimal("2"), multiplier=None)
    assert fill.gross_cost is None


def test_a_fill_with_no_commission_reports_no_net_cost() -> None:
    fill = _fill(commission=None)
    assert fill.commission is None
    assert fill.net_cost is None


def test_direction_lives_in_side_not_in_a_signed_quantity() -> None:
    buy = _fill(side=OrderSide.BUY)
    sell = _fill(side=OrderSide.SELL)
    assert buy.quantity > 0 and sell.quantity > 0
    assert buy.signed_quantity == Decimal("2")
    assert sell.signed_quantity == Decimal("-2")


# ---------------------------------------------------------------------------
# The expected ledger
# ---------------------------------------------------------------------------
def test_an_expected_position_must_balance_its_own_arithmetic() -> None:
    with pytest.raises(ValidationError, match="net quantity"):
        ExpectedPosition(
            position_id="position-1",
            account_reference=MASKED,
            key="cid:1",
            as_of=NOW,
            underlying="NVDA",
            asset_class=SecurityType.OPTION,
            symbol="NVDA",
            quantity=Decimal("5"),
            bought_quantity=Decimal("2"),
            sold_quantity=Decimal("0"),
            fill_ids=["fill-1"],
        )


def test_an_expected_position_cannot_claim_contracts_with_no_fill_behind_it() -> None:
    with pytest.raises(ValidationError, match="no contributing fill"):
        ExpectedPosition(
            position_id="position-1",
            account_reference=MASKED,
            key="cid:1",
            as_of=NOW,
            underlying="NVDA",
            asset_class=SecurityType.OPTION,
            symbol="NVDA",
            quantity=Decimal("2"),
            bought_quantity=Decimal("2"),
        )


# ---------------------------------------------------------------------------
# Structures (brief sections 61-62)
# ---------------------------------------------------------------------------
def test_a_structure_recorded_complete_must_actually_hold_every_leg() -> None:
    with pytest.raises(ValidationError, match="recorded COMPLETE"):
        StrategyPosition(
            strategy_position_id="strategypos-1",
            account_reference=MASKED,
            as_of=NOW,
            underlying="NVDA",
            strategy="LONG_STRADDLE",
            status=StructureStatus.COMPLETE,
            authorized_quantity=1,
            filled_quantity=Decimal("1"),
            legs=[
                StrategyLegPosition(
                    leg_index=0,
                    key="cid:1",
                    underlying="NVDA",
                    expected_quantity=Decimal("1"),
                    observed_quantity=Decimal("1"),
                ),
                StrategyLegPosition(
                    leg_index=1,
                    key="cid:2",
                    underlying="NVDA",
                    expected_quantity=Decimal("1"),
                    observed_quantity=Decimal("0"),
                ),
            ],
            opportunity_id="opportunity-1",
        )


def test_a_structure_recorded_partial_must_actually_disagree_with_itself() -> None:
    with pytest.raises(ValidationError, match="recorded PARTIAL"):
        StrategyPosition(
            strategy_position_id="strategypos-1",
            account_reference=MASKED,
            as_of=NOW,
            underlying="NVDA",
            strategy="LONG_STRADDLE",
            status=StructureStatus.PARTIAL,
            authorized_quantity=1,
            filled_quantity=Decimal("1"),
            legs=[
                StrategyLegPosition(
                    leg_index=0,
                    key="cid:1",
                    underlying="NVDA",
                    expected_quantity=Decimal("1"),
                    observed_quantity=Decimal("1"),
                ),
            ],
            opportunity_id="opportunity-1",
        )


def test_a_missing_structure_cannot_be_projected_onto_the_milestone_1_boundary() -> None:
    """PositionState has no member for "the broker does not report this"."""
    structure = StrategyPosition(
        strategy_position_id="strategypos-1",
        account_reference=MASKED,
        as_of=NOW,
        underlying="NVDA",
        strategy="LONG_CALL",
        status=StructureStatus.MISSING,
        authorized_quantity=1,
        filled_quantity=Decimal("1"),
        legs=[
            StrategyLegPosition(
                leg_index=0,
                key="cid:1",
                underlying="NVDA",
                expected_quantity=Decimal("1"),
                observed_quantity=Decimal("0"),
            )
        ],
        opportunity_id="opportunity-1",
    )
    with pytest.raises(ValueError, match="reconciliation finding, not a position"):
        structure.to_position_snapshot(legs=[], source="SIMULATOR")


def test_a_snapshot_stamps_simulated_data_as_simulated() -> None:
    """Simulated holdings must never be readable as a live broker position."""
    snapshot = build_position_snapshot(
        [stock_position()],
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
    )
    assert snapshot.simulated is True
    assert all(position.simulated for position in snapshot.positions)


def _no_right() -> OptionRight:
    """An option row that arrived without its right.

    Typed as an ``OptionRight`` so the test reads as the scenario it describes;
    the value is deliberately ``None``, which is how a broker row missing its
    right actually arrives.
    """
    return cast(OptionRight, None)


def _fill(
    *,
    price: Decimal = Decimal("5.95"),
    quantity: Decimal = Decimal("2"),
    multiplier: int | None = 100,
    commission: Decimal | None = Decimal("1.30"),
    side: OrderSide = OrderSide.BUY,
) -> ObservedFill:
    return ObservedFill(
        fill_id=fill_identifier(broker_execution_id="exec-1"),
        account_reference=MASKED,
        key="cid:1",
        broker_execution_id="exec-1",
        underlying="NVDA",
        symbol="NVDA",
        asset_class=SecurityType.OPTION,
        contract_id=1,
        multiplier=multiplier,
        side=side,
        quantity=quantity,
        price=price,
        commission=commission,
        executed_at=NOW,
        observed_at=NOW,
        broker_source="SIMULATOR",
        provenance=AcquisitionProvenance.UNKNOWN,
    )
