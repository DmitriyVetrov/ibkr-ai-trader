"""LONG_STRADDLE: a bought call and put on one strike, for a large move.

Specification: [`skills/strategies/straddle.md`](../../skills/strategies/straddle.md).
Policy: ``config/strategies/long_straddle.yaml``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    ContractRejectionReason,
    ContractSelectionStatus,
    Direction,
    LegAction,
    MarketHypothesis,
    OptionRight,
    StrategyType,
    StrikeSelectionPolicy,
)
from trading_system.strategies.base import StrikeRelationship

from .conftest import FURTHER, NEAR_TARGET, REFERENCE

pytestmark = pytest.mark.unit

STRATEGY = StrategyType.LONG_STRADDLE


# ---------------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------------
def test_it_answers_the_direction_uncertain_hypotheses(registry) -> None:
    specification = registry.require(STRATEGY)

    assert set(specification.applicable_hypotheses) == {MarketHypothesis.A, MarketHypothesis.D}
    assert specification.directional_view is Direction.UNCERTAIN


def test_it_is_a_bought_call_and_a_bought_put(registry) -> None:
    legs = registry.require(STRATEGY).legs

    assert [leg.right for leg in legs] == [OptionRight.CALL, OptionRight.PUT]
    assert all(leg.action is LegAction.BUY for leg in legs)


def test_both_legs_are_at_the_money_on_one_strike(registry) -> None:
    specification = registry.require(STRATEGY)

    assert specification.strike_relationship is StrikeRelationship.SAME
    assert all(leg.strike_policy is StrikeSelectionPolicy.ATM for leg in specification.legs)


def test_it_is_managed_as_one_position(registry) -> None:
    """Specification section 17A: the trailing stop applies to the structure."""
    specification = registry.require(STRATEGY)

    assert specification.structure.single_position
    assert specification.exit_policy.allow_independent_leg_exit is False


def test_its_liquidity_floors_are_tighter_than_a_directional_strategy(registry) -> None:
    """Two legs pay two spreads in and two out."""
    straddle = registry.require(STRATEGY)
    call = registry.require(StrategyType.LONG_CALL)

    assert straddle.min_open_interest > call.min_open_interest
    assert straddle.max_bid_ask_spread_pct < call.max_bid_ask_spread_pct


# ---------------------------------------------------------------------------
# Multi-leg construction
# ---------------------------------------------------------------------------
def test_a_valid_entry_builds_two_legs_on_one_strike(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert len(result.legs) == 2
    assert {leg.right for leg in result.legs} == {OptionRight.CALL, OptionRight.PUT}
    assert len({leg.strike for leg in result.legs}) == 1
    assert len({leg.expiration for leg in result.legs}) == 1
    assert len({leg.multiplier for leg in result.legs}) == 1
    assert len({leg.trading_class for leg in result.legs}) == 1


def test_the_shared_strike_is_the_one_nearest_the_reference_price(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)

    assert result.legs[0].strike == REFERENCE
    assert result.reference_price == REFERENCE


def test_a_strike_usable_by_only_one_leg_is_not_selected(
    store_underlying_quote, store_chain, build_option_quotes, store_quote_records, select
) -> None:
    """The shared strike is one decision, not two that happen to agree."""
    store_underlying_quote()
    store_chain()
    store_quote_records(
        [
            *build_option_quotes(rights=[OptionRight.CALL]),
            *build_option_quotes(
                rights=[OptionRight.PUT], strikes=[Decimal("175"), Decimal("185")]
            ),
        ]
    )

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert len({leg.strike for leg in result.legs}) == 1
    assert result.legs[0].strike in {Decimal("175"), Decimal("185")}


def test_a_missing_leg_invalidates_the_whole_structure(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(rights=[OptionRight.CALL])

    result = select(strategy=STRATEGY)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert result.legs == [], "a partially filled straddle is a directional position"


def test_no_reference_price_means_no_at_the_money_strike(
    store_chain, store_option_quotes, select
) -> None:
    store_chain()
    store_option_quotes()

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE


def test_a_chain_too_coarse_for_the_money_is_refused(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    # 11% either side of the reference: inside the price band, far outside the
    # configured 5% strike-distance ceiling.
    distant = [Decimal("160"), Decimal("200")]
    store_underlying_quote()
    store_chain(strikes=distant)
    store_option_quotes(strikes=distant)

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.NO_VALID_STRIKE
    assert ContractRejectionReason.STRIKE_POLICY_NOT_SATISFIED.value in {
        rejection.reason.value for rejection in result.rejected_candidates
    }


# ---------------------------------------------------------------------------
# Event alignment
# ---------------------------------------------------------------------------
def test_it_aligns_its_expiration_to_the_research_event(
    priced_chain, select, selection_now
) -> None:
    priced_chain()
    event = selection_now + timedelta(days=20)

    result = select(strategy=STRATEGY, event_time=event)

    assert result.expiration == FURTHER
    assert result.expiration > event.date()


def test_without_an_event_it_falls_back_and_records_that_it_did(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY, event_time=None)

    assert result.expiration == NEAR_TARGET
    assert any("names no event" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
def test_the_cost_covers_both_legs_at_the_ask(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)
    cost = result.cost

    assert cost is not None and cost.available
    assert cost.estimated_debit == sum(
        (leg.ask * Decimal(leg.multiplier) for leg in result.legs), Decimal(0)
    )
