"""LONG_PUT: one bought put for a predominantly bearish outlook.

Specification: [`skills/strategies/long_put.md`](../../skills/strategies/long_put.md).
Policy: ``config/strategies/long_put.yaml``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    ContractSelectionStatus,
    Direction,
    LegAction,
    MarketHypothesis,
    OptionRight,
    StrategyType,
    StrikeSelectionPolicy,
)

pytestmark = pytest.mark.unit

STRATEGY = StrategyType.LONG_PUT


# ---------------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------------
def test_it_answers_only_the_bearish_hypothesis(registry) -> None:
    specification = registry.require(STRATEGY)

    assert specification.applicable_hypotheses == (MarketHypothesis.C,)
    assert specification.directional_view is Direction.BEARISH


def test_it_is_one_bought_put(registry) -> None:
    (leg,) = registry.require(STRATEGY).legs

    assert leg.action is LegAction.BUY
    assert leg.right is OptionRight.PUT


def test_its_target_delta_is_negative(registry) -> None:
    """Put deltas run from 0 to -1; the sign is validated, never inferred."""
    (leg,) = registry.require(STRATEGY).legs

    assert leg.strike_policy is StrikeSelectionPolicy.TARGET_DELTA
    assert leg.target_delta is not None and leg.target_delta < 0


def test_a_put_leg_targeting_a_positive_delta_is_refused(system_config) -> None:
    from trading_system.infrastructure.settings import StrategyConfig

    put = system_config.strategies["long_put"]

    with pytest.raises(ValueError, match="PUT with a positive delta"):
        StrategyConfig.model_validate({**put.model_dump(mode="python"), "target_delta": 0.60})


# ---------------------------------------------------------------------------
# Valid entry
# ---------------------------------------------------------------------------
def test_a_valid_entry_selects_one_put(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert len(result.legs) == 1
    assert result.legs[0].right is OptionRight.PUT
    assert result.legs[0].delta is not None and result.legs[0].delta < 0


def test_it_never_selects_a_call(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=STRATEGY)

    assert all(leg.right is not OptionRight.CALL for leg in result.legs)


# ---------------------------------------------------------------------------
# Invalid entry
# ---------------------------------------------------------------------------
def test_no_delta_means_no_entry(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(delta=None)

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE


def test_a_wide_spread_means_no_entry(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(bid=Decimal("1.00"), ask=Decimal("6.00"))

    result = select(strategy=STRATEGY)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS


def test_no_chain_means_no_entry(store_underlying_quote, select) -> None:
    store_underlying_quote()

    result = select(strategy=STRATEGY)

    assert result.selection_status is ContractSelectionStatus.OPTION_CHAIN_UNAVAILABLE
    assert not result.legs


# ---------------------------------------------------------------------------
# Exit policy
# ---------------------------------------------------------------------------
def test_its_exit_policy_matches_its_directional_sibling(registry) -> None:
    call = registry.require(StrategyType.LONG_CALL).exit_policy
    put = registry.require(STRATEGY).exit_policy

    assert put.close_at_dte == call.close_at_dte
    assert put.allow_independent_leg_exit is False
