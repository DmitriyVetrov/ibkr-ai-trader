"""LONG_STRANGLE: bought out-of-the-money call and put, strikes apart.

Specification: [`skills/strategies/strangle.md`](../../skills/strategies/strangle.md).
Policy: ``config/strategies/long_strangle.yaml``.
"""

from __future__ import annotations

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

from .conftest import REFERENCE

pytestmark = pytest.mark.unit

STRATEGY = StrategyType.LONG_STRANGLE


def _rejections(result) -> set[str]:
    return {rejection.reason.value for rejection in result.rejected_candidates}


# ---------------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------------
def test_it_answers_the_direction_uncertain_hypotheses(registry) -> None:
    specification = registry.require(STRATEGY)

    assert set(specification.applicable_hypotheses) == {MarketHypothesis.A, MarketHypothesis.D}
    assert specification.directional_view is Direction.UNCERTAIN


def test_it_is_a_bought_call_and_a_bought_put_on_different_strikes(registry) -> None:
    specification = registry.require(STRATEGY)

    assert [leg.right for leg in specification.legs] == [OptionRight.CALL, OptionRight.PUT]
    assert all(leg.action is LegAction.BUY for leg in specification.legs)
    assert specification.strike_relationship is StrikeRelationship.CALL_ABOVE_PUT


def test_both_legs_are_placed_out_of_the_money_by_one_configured_offset(registry) -> None:
    """One number serves both legs: the direction comes from the right."""
    specification = registry.require(STRATEGY)

    assert all(leg.strike_policy is StrikeSelectionPolicy.OTM_PERCENT for leg in specification.legs)
    offsets = {leg.strike_offset_pct for leg in specification.legs}
    assert len(offsets) == 1
    assert offsets.pop() > 0


def test_it_is_managed_as_one_position(registry) -> None:
    specification = registry.require(STRATEGY)

    assert specification.structure.single_position
    assert specification.exit_policy.allow_independent_leg_exit is False


# ---------------------------------------------------------------------------
# Multi-leg construction
# ---------------------------------------------------------------------------
def test_a_valid_entry_places_the_call_above_and_the_put_below(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    call = next(leg for leg in result.legs if leg.right is OptionRight.CALL)
    put = next(leg for leg in result.legs if leg.right is OptionRight.PUT)
    assert call.strike > put.strike
    assert call.strike >= REFERENCE >= put.strike
    assert call.expiration == put.expiration


def test_the_strikes_are_the_listed_ones_nearest_each_offset_target(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)

    call = next(leg for leg in result.legs if leg.right is OptionRight.CALL)
    put = next(leg for leg in result.legs if leg.right is OptionRight.PUT)
    assert call.strike == Decimal("190"), "180 x 1.05 = 189"
    assert put.strike == Decimal("170"), "180 x 0.95 = 171"
    assert call.target_strike is not None and put.target_strike is not None


def test_a_leg_on_the_wrong_side_of_the_reference_is_refused(priced_chain, select) -> None:
    """An out-of-the-money call below spot is an in-the-money call."""
    priced_chain()

    result = select(strategy=STRATEGY)

    assert ContractRejectionReason.STRIKE_POLICY_NOT_SATISFIED.value in _rejections(result)


def test_a_chain_that_cannot_separate_the_strikes_is_refused(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """A single-strike "strangle" is a straddle under another name."""
    single = [REFERENCE]
    store_underlying_quote()
    store_chain(strikes=single)
    store_option_quotes(strikes=single)

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.NO_VALID_CONTRACT
    assert ContractRejectionReason.INCOMPATIBLE_LEG.value in _rejections(result)
    assert result.legs == []


def test_a_missing_leg_invalidates_the_whole_structure(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(rights=[OptionRight.PUT])

    result = select(strategy=STRATEGY)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert result.legs == []


def test_illiquid_legs_mean_no_entry(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(open_interest=Decimal("100"), volume=Decimal("10"))

    result = select(strategy=STRATEGY)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert ContractRejectionReason.LOW_OPTION_LIQUIDITY.value in _rejections(result)


def test_its_strikes_are_further_apart_than_a_straddles(priced_chain, select) -> None:
    """The structural difference from a straddle, on the same chain.

    Deliberately not a claim about which costs less: that depends on real
    option prices, and a test fixture's synthetic premiums are not evidence
    about a market.
    """
    priced_chain()

    strangle = select(strategy=STRATEGY)
    straddle = select(strategy=StrategyType.LONG_STRADDLE)

    strangle_width = max(leg.strike for leg in strangle.legs) - min(
        leg.strike for leg in strangle.legs
    )
    straddle_width = max(leg.strike for leg in straddle.legs) - min(
        leg.strike for leg in straddle.legs
    )
    assert straddle_width == 0
    assert strangle_width > 0


def test_the_cost_covers_both_legs_at_the_ask(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)
    cost = result.cost

    assert cost is not None and cost.available
    assert cost.estimated_debit == sum(
        (leg.ask * Decimal(leg.multiplier) for leg in result.legs), Decimal(0)
    )
