"""The strategy registry (brief section 36).

The registry is the allow-list. Everything downstream — what the agent may
choose from, what the contract selector may resolve, what the risk engine will
later see — is whatever this resolved. So the properties worth asserting are
the ones that would let something untradeable through: a strategy with no
structure, a structure the configuration disagrees with, or a limit that quietly
widened the risk policy.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    Direction,
    ExpirationSelectionPolicy,
    LegAction,
    MarketHypothesis,
    StrategyType,
    StrikeSelectionPolicy,
)
from trading_system.infrastructure.settings import ConfigError, SystemConfig, load_config
from trading_system.strategies.base import StrikeRelationship
from trading_system.strategies.registry import StrategyRegistry, StrategyRegistryError

pytestmark = pytest.mark.unit


@pytest.fixture
def registry(system_config: SystemConfig) -> StrategyRegistry:
    return StrategyRegistry.from_config(system_config)


# ---------------------------------------------------------------------------
# Every strategy is completely specified
# ---------------------------------------------------------------------------
def test_the_shipped_strategies_are_all_registered(registry: StrategyRegistry) -> None:
    assert {s.strategy_id for s in registry.all()} == {
        StrategyType.LONG_CALL,
        StrategyType.LONG_PUT,
        StrategyType.LONG_STRADDLE,
        StrategyType.LONG_STRANGLE,
    }


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_strategy_has_a_version(registry: StrategyRegistry, strategy: StrategyType) -> None:
    assert registry.require(strategy).version


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_strategy_declares_its_hypotheses(
    registry: StrategyRegistry, strategy: StrategyType
) -> None:
    assert registry.require(strategy).applicable_hypotheses


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_strategy_defines_its_legs(
    registry: StrategyRegistry, strategy: StrategyType
) -> None:
    specification = registry.require(strategy)

    assert specification.legs
    assert len(specification.legs) == specification.structure.leg_count
    assert all(leg.action is LegAction.BUY for leg in specification.legs), (
        "every shipped strategy is long premium; nothing here sells an option"
    )


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_strategy_has_dte_constraints(
    registry: StrategyRegistry, strategy: StrategyType
) -> None:
    specification = registry.require(strategy)

    assert specification.dte_min > 0
    assert specification.dte_max >= specification.dte_min


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_leg_has_a_resolvable_strike_policy(
    registry: StrategyRegistry, strategy: StrategyType
) -> None:
    """A policy whose parameter is missing would have to be guessed at."""
    for leg in registry.require(strategy).legs:
        assert leg.strike_policy in set(StrikeSelectionPolicy)
        if leg.strike_policy is StrikeSelectionPolicy.TARGET_DELTA:
            assert leg.target_delta is not None
        if leg.strike_policy is StrikeSelectionPolicy.OTM_PERCENT:
            assert leg.strike_offset_pct is not None


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_strategy_has_an_expiration_policy(
    registry: StrategyRegistry, strategy: StrategyType
) -> None:
    specification = registry.require(strategy)

    assert specification.expiration_rule in set(ExpirationSelectionPolicy)
    if specification.expiration_rule is not ExpirationSelectionPolicy.NEAREST_VALID:
        assert specification.target_dte is not None


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_strategy_states_the_fields_it_requires(
    registry: StrategyRegistry, strategy: StrategyType
) -> None:
    specification = registry.require(strategy)

    assert specification.required_option_fields
    assert any(field.value == "CONTRACT_ID" for field in specification.required_option_fields)


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_strategy_has_liquidity_and_price_limits(
    registry: StrategyRegistry, strategy: StrategyType
) -> None:
    specification = registry.require(strategy)

    assert specification.min_open_interest > 0
    assert specification.min_daily_volume > 0
    assert specification.max_bid_ask_spread_pct > 0
    assert specification.min_option_price_eur <= specification.max_option_price_eur


@pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
def test_every_strategy_states_which_underlyings_it_may_be_written_on(
    registry: StrategyRegistry, strategy: StrategyType
) -> None:
    assert registry.require(strategy).allowed_underlying_types


# ---------------------------------------------------------------------------
# The hypothesis mapping is derived, not declared twice
# ---------------------------------------------------------------------------
def test_the_hypothesis_mapping_comes_from_the_strategies_themselves(
    registry: StrategyRegistry,
) -> None:
    mapping = registry.hypothesis_map()

    assert mapping[MarketHypothesis.B] == [StrategyType.LONG_CALL]
    assert mapping[MarketHypothesis.C] == [StrategyType.LONG_PUT]
    assert set(mapping[MarketHypothesis.A]) == {
        StrategyType.LONG_STRADDLE,
        StrategyType.LONG_STRANGLE,
    }
    assert set(mapping[MarketHypothesis.D]) == set(mapping[MarketHypothesis.A])


def test_hypothesis_e_maps_to_no_strategy_at_all(registry: StrategyRegistry) -> None:
    """Not by a special case: no strategy declares E, so none is eligible."""
    assert registry.for_hypothesis(MarketHypothesis.E) == []
    assert registry.options_for(MarketHypothesis.E) == []


def test_a_disabled_strategy_is_never_eligible(system_config: SystemConfig) -> None:
    disabled = system_config.strategies["long_call"].model_copy(update={"enabled": False})
    config = system_config.model_copy(
        update={"strategies": {**system_config.strategies, "long_call": disabled}}
    )

    registry = StrategyRegistry.from_config(config)

    assert registry.for_hypothesis(MarketHypothesis.B) == []
    assert registry.require(StrategyType.LONG_CALL).enabled is False


# ---------------------------------------------------------------------------
# Structure is code, policy is configuration
# ---------------------------------------------------------------------------
def test_structure_matches_what_each_strategy_actually_is(registry: StrategyRegistry) -> None:
    assert registry.require(StrategyType.LONG_CALL).directional_view is Direction.BULLISH
    assert registry.require(StrategyType.LONG_PUT).directional_view is Direction.BEARISH
    assert registry.require(StrategyType.LONG_STRADDLE).directional_view is Direction.UNCERTAIN
    assert (
        registry.require(StrategyType.LONG_STRADDLE).strike_relationship is StrikeRelationship.SAME
    )
    assert (
        registry.require(StrategyType.LONG_STRANGLE).strike_relationship
        is StrikeRelationship.CALL_ABOVE_PUT
    )


def test_a_configuration_that_changes_the_structure_is_refused(
    system_config: SystemConfig,
) -> None:
    """Configuration tunes a strategy; it does not redefine one."""
    call = system_config.strategies["long_call"]
    spread = call.model_copy(
        update={"legs": [*call.legs, call.legs[0].model_copy(update={"action": LegAction.SELL})]}
    )
    config = system_config.model_copy(
        update={"strategies": {**system_config.strategies, "long_call": spread}}
    )

    with pytest.raises(StrategyRegistryError, match="does not describe a LONG_CALL"):
        StrategyRegistry.from_config(config)


def test_a_strategy_with_no_structural_definition_is_refused(
    system_config: SystemConfig,
) -> None:
    """A configuration file is not a definition."""
    from trading_system.strategies import registry as registry_module

    call = system_config.strategies["long_call"]
    original = dict(registry_module.STRUCTURES)
    try:
        registry_module.STRUCTURES = {
            key: value for key, value in original.items() if key is not StrategyType.LONG_CALL
        }
        with pytest.raises(StrategyRegistryError, match="no structural definition"):
            StrategyRegistry.from_config(system_config)
    finally:
        registry_module.STRUCTURES = original
    assert call.strategy_type is StrategyType.LONG_CALL


def test_multi_leg_strategies_are_managed_as_one_position(registry: StrategyRegistry) -> None:
    """Specification section 17A, carried in code so a later stage cannot differ."""
    for strategy in (StrategyType.LONG_STRADDLE, StrategyType.LONG_STRANGLE):
        specification = registry.require(strategy)
        assert specification.is_multi_leg
        assert specification.structure.single_position
        assert specification.exit_policy.allow_independent_leg_exit is False


# ---------------------------------------------------------------------------
# A strategy may narrow a global limit. It may never widen one.
# ---------------------------------------------------------------------------
def test_a_strategy_cannot_widen_the_global_dte_window(system_config: SystemConfig) -> None:
    wide = system_config.strategies["long_call"].model_copy(update={"dte_max": 60})
    config = system_config.model_copy(
        update={"strategies": {**system_config.strategies, "long_call": wide}}
    )

    with pytest.raises(StrategyRegistryError, match="widens a global risk limit"):
        StrategyRegistry.from_config(config)


def test_a_strategy_cannot_lower_the_global_liquidity_floor(
    system_config: SystemConfig,
) -> None:
    call = system_config.strategies["long_call"]
    thin = call.model_copy(
        update={"liquidity": call.liquidity.model_copy(update={"min_open_interest": 1})}
    )
    config = system_config.model_copy(
        update={"strategies": {**system_config.strategies, "long_call": thin}}
    )

    with pytest.raises(StrategyRegistryError, match="min_open_interest"):
        StrategyRegistry.from_config(config)


def test_a_strategy_cannot_raise_the_global_spread_ceiling(system_config: SystemConfig) -> None:
    call = system_config.strategies["long_call"]
    loose = call.model_copy(
        update={"liquidity": call.liquidity.model_copy(update={"max_bid_ask_spread_pct": 50.0})}
    )
    config = system_config.model_copy(
        update={"strategies": {**system_config.strategies, "long_call": loose}}
    )

    with pytest.raises(StrategyRegistryError, match="max_bid_ask_spread_pct"):
        StrategyRegistry.from_config(config)


def test_a_strategy_cannot_raise_the_global_price_ceiling(system_config: SystemConfig) -> None:
    expensive = system_config.strategies["long_call"].model_copy(
        update={"max_option_price_eur": Decimal("500.00")}
    )
    config = system_config.model_copy(
        update={"strategies": {**system_config.strategies, "long_call": expensive}}
    )

    with pytest.raises(StrategyRegistryError, match="max_option_price_eur"):
        StrategyRegistry.from_config(config)


def test_configuration_loading_refuses_a_widening_strategy(tmp_config_dir) -> None:
    """The same rule at the other end: a bad file never becomes a SystemConfig."""
    path = tmp_config_dir / "strategies" / "long_call.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("dte_max: 30", "dte_max: 45"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="widens a global risk limit"):
        load_config(tmp_config_dir)


def test_effective_limits_are_the_tighter_of_the_two(registry: StrategyRegistry) -> None:
    """A consumer never has to remember to intersect them, and cannot forget."""
    straddle = registry.require(StrategyType.LONG_STRADDLE)

    assert straddle.min_open_interest == 750, "the strategy's floor, above the risk policy's 500"
    assert straddle.max_bid_ask_spread_pct == 6.0, "the strategy's ceiling, below the risk 10%"


# ---------------------------------------------------------------------------
# The agent-facing view
# ---------------------------------------------------------------------------
def test_the_agent_view_describes_structure_and_never_a_contract(
    registry: StrategyRegistry,
) -> None:
    """Legs as shapes, a DTE window as a window — no contract, no parameters.

    The descriptive prose may say the word "strikes"; that is what a strangle
    *is*. What must not exist is a field the agent could read a number out of,
    so the assertion is about fields rather than about words.
    """
    from trading_system.strategies.models import StrategyOption

    option = registry.require(StrategyType.LONG_STRANGLE).to_option()

    assert option.legs == ["BUY CALL x1", "BUY PUT x1"]
    assert option.leg_count == 2
    assert option.dte_min == 14 and option.dte_max == 30
    for forbidden in (
        "strike",
        "strikes",
        "strike_offset_pct",
        "target_delta",
        "expiration",
        "expiry",
        "contract_id",
        "delta",
        "min_open_interest",
        "max_option_price_eur",
    ):
        assert forbidden not in StrategyOption.model_fields, f"the agent view exposes {forbidden}"


def test_the_registry_refuses_a_strategy_it_does_not_have(registry: StrategyRegistry) -> None:
    from trading_system.strategies import registry as registry_module

    original = dict(registry_module.STRUCTURES)
    empty = StrategyRegistry([])

    assert empty.get(StrategyType.LONG_CALL) is None
    with pytest.raises(StrategyRegistryError, match="not tradeable"):
        empty.require(StrategyType.LONG_CALL)
    assert original == registry_module.STRUCTURES
