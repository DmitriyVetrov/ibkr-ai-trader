"""The configuration hierarchy: a strategy may narrow, never widen.

The direction that counts as *narrowing* differs per limit, and getting one
wrong would silently enforce the inverse of the intended safety property. Each
is asserted here in both directions: the narrowing loads, the widening does not.

Widening is a **load failure**, never a clamp. A clamped limit is a limit
nobody can see, and here that would mean nobody could see how long a position
is actually allowed to be held.
"""

from __future__ import annotations

import pytest

from trading_system.domain.enums import RiskLimitScope, StrategyType
from trading_system.exit.validation import (
    configuration_report,
    effective_policy,
    strategy_config_for,
)
from trading_system.infrastructure.settings import ConfigError, SystemConfig, load_config

pytestmark = pytest.mark.unit


def _edit(config_dir, strategy: str, old: str, new: str) -> None:
    path = config_dir / "strategies" / f"{strategy}.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new), encoding="utf-8")


# ---------------------------------------------------------------------------
# The shipped configuration is coherent
# ---------------------------------------------------------------------------
def test_the_shipped_configuration_loads(system_config: SystemConfig) -> None:
    assert system_config.exit.enabled is True
    assert system_config.exit.policy_version


def test_every_shipped_strategy_narrows_or_matches_the_global_envelope(
    system_config: SystemConfig,
) -> None:
    envelope = system_config.exit
    for name, strategy in system_config.strategies.items():
        policy = strategy.exit_policy
        assert policy.close_at_dte >= envelope.expiration.force_exit_dte, name
        assert policy.trailing_stop_pct <= envelope.trailing.trail_distance_pct, name
        assert policy.max_loss_pct <= envelope.max_loss.loss_pct, name
        if policy.take_profit_pct is not None:
            assert policy.take_profit_pct <= envelope.take_profit.return_pct, name


# ---------------------------------------------------------------------------
# Widening fails to load, per limit, in the right direction
# ---------------------------------------------------------------------------
def test_holding_closer_to_expiry_than_the_global_floor_fails_to_load(
    tmp_config_dir,
) -> None:
    """A *smaller* close_at_dte holds the position later and is a widening."""
    _edit(tmp_config_dir, "long_call", "close_at_dte: 7", "close_at_dte: 2")

    with pytest.raises(ConfigError, match="close_at_dte"):
        load_config(tmp_config_dir)


def test_closing_earlier_than_the_global_floor_loads(tmp_config_dir) -> None:
    """A *larger* close_at_dte closes sooner and is a narrowing."""
    _edit(tmp_config_dir, "long_call", "close_at_dte: 7", "close_at_dte: 14")

    config = load_config(tmp_config_dir)

    assert config.strategies["long_call"].exit_policy.close_at_dte == 14


def test_a_looser_trail_fails_to_load(tmp_config_dir) -> None:
    """A *larger* distance gives back more of the peak and is a widening."""
    _edit(tmp_config_dir, "long_call", "trailing_stop_pct: 30.0", "trailing_stop_pct: 55.0")

    with pytest.raises(ConfigError, match="trailing_stop_pct"):
        load_config(tmp_config_dir)


def test_a_tighter_trail_loads(tmp_config_dir) -> None:
    _edit(tmp_config_dir, "long_call", "trailing_stop_pct: 30.0", "trailing_stop_pct: 15.0")

    config = load_config(tmp_config_dir)

    assert config.strategies["long_call"].exit_policy.trailing_stop_pct == 15.0


def test_a_larger_maximum_loss_fails_to_load(tmp_config_dir) -> None:
    _edit(tmp_config_dir, "long_call", "max_loss_pct: 50.0", "max_loss_pct: 90.0")

    with pytest.raises(ConfigError, match="max_loss_pct"):
        load_config(tmp_config_dir)


def test_a_higher_take_profit_target_fails_to_load(tmp_config_dir) -> None:
    """Holding out for more is holding longer, which is a widening."""
    _edit(tmp_config_dir, "long_call", "take_profit_pct: 100.0", "take_profit_pct: 500.0")

    with pytest.raises(ConfigError, match="take_profit_pct"):
        load_config(tmp_config_dir)


def test_no_take_profit_target_is_permitted(tmp_config_dir) -> None:
    """Take profit is not a safety limit: a position that never takes one is
    still bounded by the trailing stop, the maximum loss and the expiration."""
    _edit(tmp_config_dir, "long_call", "take_profit_pct: 100.0", "take_profit_pct: null")

    config = load_config(tmp_config_dir)

    assert config.strategies["long_call"].exit_policy.take_profit_pct is None


def test_the_failure_names_the_strategy_and_the_limit(tmp_config_dir) -> None:
    """A load failure nobody can act on is barely better than a clamp."""
    _edit(tmp_config_dir, "long_straddle", "max_loss_pct: 55.0", "max_loss_pct: 95.0")

    with pytest.raises(ConfigError) as raised:
        load_config(tmp_config_dir)

    assert "long_straddle" in str(raised.value)
    assert "max_loss_pct" in str(raised.value)


def test_nothing_is_clamped(tmp_config_dir) -> None:
    """The whole point of failing to load: a clamped limit is one nobody sees."""
    _edit(tmp_config_dir, "long_call", "max_loss_pct: 50.0", "max_loss_pct: 90.0")

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


# ---------------------------------------------------------------------------
# Global safety switches that cannot be turned off
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("section", "key", "message"),
    [
        ("exit", "allow_independent_leg_exit: false", "naked long"),
        ("exit", "allow_quote_field_substitution: false", "invents a price"),
        ("exit", "allow_level_to_fall: false", "is not a stop"),
        ("exit", "allow_prose_interpretation: false", "NOT_EVALUATED"),
        ("exit", "require_explicit_authorization: true", "two acts"),
    ],
)
def test_a_safety_switch_cannot_be_turned_off(
    tmp_config_dir, section: str, key: str, message: str
) -> None:
    path = tmp_config_dir / f"{section}.yaml"
    text = path.read_text(encoding="utf-8")
    assert key in text
    name, value = key.split(": ")
    path.write_text(
        text.replace(key, f"{name}: {'false' if value == 'true' else 'true'}"), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=message):
        load_config(tmp_config_dir)


def test_a_warning_that_fires_after_the_deadline_fails_to_load(tmp_config_dir) -> None:
    path = tmp_config_dir / "exit.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("warning_dte: 10", "warning_dte: 2"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="warns nobody"):
        load_config(tmp_config_dir)


# ---------------------------------------------------------------------------
# The effective policy records where each value came from
# ---------------------------------------------------------------------------
def test_a_strategy_value_is_recorded_as_supplied_by_the_strategy(
    system_config: SystemConfig,
) -> None:
    strategy = strategy_config_for(system_config, StrategyType.LONG_CALL)
    assert strategy is not None

    policy = effective_policy(
        strategy=StrategyType.LONG_CALL,
        strategy_config=strategy,
        exit_config=system_config.exit,
    )

    assert policy.expiration_force_exit_dte == strategy.exit_policy.close_at_dte
    assert policy.scopes["expiration_force_exit_dte"] is RiskLimitScope.STRATEGY
    assert policy.trailing_distance_pct == strategy.exit_policy.trailing_stop_pct
    assert policy.scopes["trailing_distance_pct"] is RiskLimitScope.STRATEGY


def test_a_position_whose_strategy_configuration_is_gone_falls_back_to_the_global_envelope(
    system_config: SystemConfig,
) -> None:
    """The *policy* falls back; the *service* still blocks with
    ``STRATEGY_METADATA_UNAVAILABLE`` rather than managing the position under a
    policy nobody chose for it."""
    policy = effective_policy(
        strategy=StrategyType.LONG_CALL,
        strategy_config=None,
        exit_config=system_config.exit,
    )

    assert policy.expiration_force_exit_dte == system_config.exit.expiration.force_exit_dte
    assert policy.scopes["expiration_force_exit_dte"] is RiskLimitScope.GLOBAL


def test_a_missing_strategy_configuration_is_a_real_answer(
    system_config: SystemConfig,
) -> None:
    assert strategy_config_for(system_config, StrategyType.LONG_CALL) is not None


def test_the_snapshot_never_permits_an_independent_leg_exit(
    system_config: SystemConfig,
) -> None:
    for strategy in StrategyType:
        policy = effective_policy(
            strategy=strategy,
            strategy_config=strategy_config_for(system_config, strategy),
            exit_config=system_config.exit,
        )
        assert policy.allow_independent_leg_exit is False


def test_the_scope_map_covers_every_layered_value(system_config: SystemConfig) -> None:
    """A reader must never have to infer that an absent key means "global"."""
    policy = effective_policy(
        strategy=StrategyType.LONG_CALL,
        strategy_config=strategy_config_for(system_config, StrategyType.LONG_CALL),
        exit_config=system_config.exit,
    )

    assert set(policy.scopes) == {
        "expiration_force_exit_dte",
        "expiration_warning_dte",
        "trailing_distance_pct",
        "trailing_activation_return_pct",
        "trailing_min_improvement_pct",
        "max_loss_pct",
        "take_profit_return_pct",
        "quote_field",
        "max_quote_age_seconds",
    }


# ---------------------------------------------------------------------------
# The report an operator reads
# ---------------------------------------------------------------------------
def test_the_configuration_report_states_every_narrowing_rule(
    system_config: SystemConfig,
) -> None:
    """An operator should be able to answer "why would this position exit
    sooner than that one" without opening a file."""
    rows = configuration_report(system_config)

    assert rows
    for row in rows:
        assert row.narrowing_rule
        assert row.global_value
        assert row.scope in (RiskLimitScope.GLOBAL, RiskLimitScope.STRATEGY)
    assert {row.name.split(".")[0] for row in rows} == set(system_config.strategies)
