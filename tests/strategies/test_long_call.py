"""LONG_CALL: one bought call for a predominantly bullish outlook.

Specification: [`skills/strategies/long_call.md`](../../skills/strategies/long_call.md).
Policy: ``config/strategies/long_call.yaml``. Structure:
``src/trading_system/strategies/long_call.py``.
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

from .conftest import NEAR_TARGET, TOO_FAR, TOO_NEAR

pytestmark = pytest.mark.unit

STRATEGY = StrategyType.LONG_CALL


# ---------------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------------
def test_it_answers_only_the_bullish_hypothesis(registry) -> None:
    specification = registry.require(STRATEGY)

    assert specification.applicable_hypotheses == (MarketHypothesis.B,)
    assert specification.directional_view is Direction.BULLISH


def test_it_is_one_bought_call(registry) -> None:
    (leg,) = registry.require(STRATEGY).legs

    assert leg.action is LegAction.BUY
    assert leg.right is OptionRight.CALL
    assert leg.ratio == 1


def test_its_strike_policy_targets_a_positive_delta(registry) -> None:
    (leg,) = registry.require(STRATEGY).legs

    assert leg.strike_policy is StrikeSelectionPolicy.TARGET_DELTA
    assert leg.target_delta is not None and leg.target_delta > 0


def test_it_requires_a_delta_and_a_two_sided_quote(registry) -> None:
    required = {field.value for field in registry.require(STRATEGY).required_option_fields}

    assert {"CONTRACT_ID", "BID", "ASK", "DELTA"} <= required


# ---------------------------------------------------------------------------
# Valid entry
# ---------------------------------------------------------------------------
def test_a_valid_entry_selects_one_call(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert len(result.legs) == 1
    assert result.legs[0].right is OptionRight.CALL
    assert result.legs[0].action is LegAction.BUY


def test_the_selected_strike_is_the_closest_to_the_target_delta(priced_chain, select) -> None:
    priced_chain()
    leg = select(strategy=STRATEGY).legs[0]

    assert leg.strike_policy is StrikeSelectionPolicy.TARGET_DELTA
    assert leg.delta is not None
    assert "TARGET_DELTA" in leg.selection_reason


# ---------------------------------------------------------------------------
# DTE boundaries
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("expiration", "reason"),
    [(TOO_NEAR, "DTE 11"), (TOO_FAR, "DTE 39")],
    ids=["below-minimum", "above-maximum"],
)
def test_an_expiration_outside_the_window_is_refused(
    store_underlying_quote, store_chain, store_option_quotes, select, expiration, reason
) -> None:
    store_underlying_quote()
    store_chain(expirations=[expiration])
    store_option_quotes(expirations=[expiration])

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.NO_VALID_EXPIRATION
    assert any(reason in text for text in result.reasons)


def test_an_expiration_inside_the_window_is_accepted(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)

    assert result.expiration == NEAR_TARGET
    assert 14 <= (result.dte or 0) <= 30


# ---------------------------------------------------------------------------
# Invalid entry
# ---------------------------------------------------------------------------
def test_no_delta_means_no_entry(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """A delta is never approximated; without one the entry is unavailable."""
    store_underlying_quote()
    store_chain()
    store_option_quotes(delta=None)

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert not result.legs


def test_illiquid_contracts_mean_no_entry(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(open_interest=Decimal("5"), volume=Decimal("2"))

    result = select(strategy=STRATEGY)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert ContractRejectionReason.LOW_OPTION_LIQUIDITY.value in {
        rejection.reason.value for rejection in result.rejected_candidates
    }


def test_implied_volatility_above_the_strategy_band_means_no_entry(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(implied_volatility=Decimal("2.00"))

    result = select(strategy=STRATEGY)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS


def test_a_premium_above_the_strategy_ceiling_means_no_entry(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(bid=Decimal("40.00"), ask=Decimal("40.20"))

    result = select(strategy=STRATEGY)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS


# ---------------------------------------------------------------------------
# Exit policy travels with the strategy
# ---------------------------------------------------------------------------
def test_its_exit_policy_is_defined_and_closes_before_expiry(registry) -> None:
    """Consumed by Milestone 9; carried here so it cannot go missing."""
    policy = registry.require(STRATEGY).exit_policy

    assert policy.trailing_stop_pct > 0
    assert policy.max_loss_pct > 0
    assert policy.close_at_dte > 0
    assert policy.allow_independent_leg_exit is False
