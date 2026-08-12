"""The limit hierarchy (brief sections 6, 26, 33, 37.4).

Two claims, and the second is the load-bearing one:

* a child layer's value is the one that applies when it is tighter;
* a child layer that tries to *widen* a parent is a configuration **load
  failure**, never a silent clamp.

The second matters because a clamped limit runs correctly and is invisible in
the diff that introduced it. Someone reviewing ``campaign.yaml`` would see a
number that is not the number in force, which is exactly the kind of quiet
disagreement between the written policy and the running policy that this
architecture exists to prevent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from trading_system.domain.enums import BudgetSource, RiskLimitScope
from trading_system.infrastructure.settings import ConfigError, load_config
from trading_system.risk.limits import (
    LimitResolutionError,
    resolve_campaign_budget,
    resolve_limits,
)

pytestmark = pytest.mark.unit


def _write(path, **changes):
    """Rewrite one YAML file with the given top-level keys replaced."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# The shipped configuration
# ---------------------------------------------------------------------------
def test_the_campaign_budget_is_five_thousand_euro(system_config):
    campaign = system_config.campaign

    assert campaign.budget_eur == Decimal("5000")
    assert campaign.currency == "EUR"
    assert isinstance(campaign.budget_eur, Decimal)


def test_the_reserve_is_never_allocatable(system_config):
    campaign = system_config.campaign

    assert campaign.reserve_eur == Decimal("1000.00")
    assert campaign.allocatable_budget_eur == Decimal("4000.00")
    assert campaign.reserve_eur + campaign.allocatable_budget_eur == campaign.budget_eur


def test_the_reserve_rounds_up_so_it_is_never_quietly_smaller(tmp_config_dir):
    """A 20% reserve on 5,001 is 1,000.20, not 1,000.19."""
    _write(tmp_config_dir / "campaign.yaml", budget_eur="5001")

    campaign = load_config(tmp_config_dir).campaign

    assert campaign.reserve_eur == Decimal("1000.20")


def test_limits_resolve_to_the_tighter_of_each_layer(system_config):
    limits = resolve_limits(system_config)

    assert limits.max_allocation_per_trade == min(
        system_config.risk.max_allocation_per_trade_eur,
        system_config.campaign.max_allocation_per_trade_eur,
    )
    assert limits.max_open_positions == min(
        system_config.risk.max_open_positions, system_config.campaign.max_open_positions
    )


def test_every_effective_limit_records_which_layer_owns_it(system_config):
    """'Why is the ceiling 1200 when risk.yaml says 1500' must be answerable."""
    limits = resolve_limits(system_config)

    assert limits.scopes["max_allocation_per_trade"] is RiskLimitScope.CAMPAIGN
    assert limits.scopes["max_total_open_risk"] is RiskLimitScope.GLOBAL
    assert limits.scopes["max_underlying_concentration_pct"] is RiskLimitScope.GLOBAL


def test_a_concentration_cap_rounds_down(system_config):
    """A ceiling is never quietly larger than the policy states."""
    limits = resolve_limits(system_config)

    assert limits.concentration_cap(30.0) == Decimal("1500.00")
    assert limits.concentration_cap(33.333) == Decimal("1666.65")


# ---------------------------------------------------------------------------
# Widening is refused, never clamped
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("max_allocation_per_trade_eur", "2000", "above the risk ceiling"),
        ("max_open_positions", 99, "above the risk ceiling"),
        ("max_risk_per_trade_eur", "9000", "above the total open-risk ceiling"),
    ],
)
def test_a_campaign_widening_a_global_limit_refuses_to_load(tmp_config_dir, key, value, message):
    _write(tmp_config_dir / "campaign.yaml", **{key: value})

    with pytest.raises(ConfigError, match=message):
        load_config(tmp_config_dir)


def test_a_campaign_widening_a_limit_is_not_clamped(tmp_config_dir):
    """The failure mode this rule exists to prevent, stated directly."""
    _write(tmp_config_dir / "campaign.yaml", max_allocation_per_trade_eur="2000")

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


def test_a_campaign_narrowing_a_global_limit_loads_and_binds(tmp_config_dir):
    _write(tmp_config_dir / "campaign.yaml", max_allocation_per_trade_eur="900")

    limits = resolve_limits(load_config(tmp_config_dir))

    assert limits.max_allocation_per_trade == Decimal("900")
    assert limits.scopes["max_allocation_per_trade"] is RiskLimitScope.CAMPAIGN


def test_per_underlying_positions_may_not_exceed_the_global_position_cap(tmp_config_dir):
    payload = yaml.safe_load((tmp_config_dir / "campaign.yaml").read_text(encoding="utf-8"))
    payload["limits"]["max_positions_per_underlying"] = 99
    (tmp_config_dir / "campaign.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="max_positions_per_underlying"):
        load_config(tmp_config_dir)


# ---------------------------------------------------------------------------
# Invalid configuration is refused
# ---------------------------------------------------------------------------
def test_an_unquoted_money_value_is_refused(tmp_config_dir):
    """An unquoted 0.50 is a binary float, and money is never binary."""
    path = tmp_config_dir / "campaign.yaml"
    text = path.read_text(encoding="utf-8").replace('budget_eur: "5000"', "budget_eur: 5000.50")
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="binary floating point"):
        load_config(tmp_config_dir)


def test_a_negative_budget_is_refused(tmp_config_dir):
    _write(tmp_config_dir / "campaign.yaml", budget_eur="-1")

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


def test_a_reserve_fraction_above_one_is_refused(tmp_config_dir):
    _write(tmp_config_dir / "campaign.yaml", reserve_fraction=1.5)

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


def test_a_minimum_allocation_no_trade_could_satisfy_is_refused(tmp_config_dir):
    """min 2000 against a 1500 per-trade ceiling admits nothing at all."""
    _write(tmp_config_dir / "campaign.yaml", min_allocation_eur="2000")

    with pytest.raises(ConfigError, match="no trade could ever"):
        load_config(tmp_config_dir)


def test_a_minimum_allocation_above_the_allocatable_budget_is_refused(tmp_config_dir):
    _write(
        tmp_config_dir / "campaign.yaml",
        budget_eur="1000",
        min_allocation_eur="900",
        max_allocation_per_trade_eur="950",
    )

    with pytest.raises(ConfigError, match="no trade could ever be funded"):
        load_config(tmp_config_dir)


def test_ranking_weights_must_sum_to_one(tmp_config_dir):
    payload = yaml.safe_load((tmp_config_dir / "campaign.yaml").read_text(encoding="utf-8"))
    payload["ranking"]["research_confidence_weight"] = 0.90
    (tmp_config_dir / "campaign.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=r"must sum to 1\.0"):
        load_config(tmp_config_dir)


def test_an_unknown_campaign_key_is_refused(tmp_config_dir):
    """Config models are extra=forbid, so a typo fails loudly."""
    _write(tmp_config_dir / "campaign.yaml", maximum_allocation="9999")

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


def test_a_missing_campaign_file_is_refused(tmp_config_dir):
    (tmp_config_dir / "campaign.yaml").unlink()

    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_config_dir)


def test_enabling_fx_conversion_without_a_rate_source_is_refused(tmp_config_dir):
    """Converting at an arbitrary rate would invent a price."""
    payload = yaml.safe_load((tmp_config_dir / "campaign.yaml").read_text(encoding="utf-8"))
    payload["currency_policy"]["allow_conversion"] = True
    (tmp_config_dir / "campaign.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="no deterministic FX rate source"):
        load_config(tmp_config_dir)


def test_a_zero_budget_loads_and_funds_nothing(tmp_config_dir):
    """Zero is handled explicitly rather than treated as unset."""
    _write(
        tmp_config_dir / "campaign.yaml",
        budget_eur="0",
        min_allocation_eur="0",
        max_allocation_per_trade_eur="0",
        max_risk_per_trade_eur="0",
    )

    limits = resolve_limits(load_config(tmp_config_dir))

    assert limits.campaign_budget == Decimal("0")
    assert limits.campaign_budget - limits.campaign_reserve == Decimal("0")


# ---------------------------------------------------------------------------
# The environment override
# ---------------------------------------------------------------------------
def test_the_environment_can_override_the_budget_and_says_so(system_config):
    budget, reserve, source = resolve_campaign_budget(
        system_config.campaign, override=Decimal("2500")
    )

    assert budget == Decimal("2500")
    assert reserve == Decimal("500.00"), "the reserve fraction applies to the new budget"
    assert source is BudgetSource.ENVIRONMENT


def test_without_an_override_the_budget_comes_from_configuration(system_config):
    budget, reserve, source = resolve_campaign_budget(system_config.campaign)

    assert budget == system_config.campaign.budget_eur
    assert reserve == system_config.campaign.reserve_eur
    assert source is BudgetSource.CONFIG


def test_a_negative_override_is_refused(system_config):
    with pytest.raises(LimitResolutionError, match="less than nothing"):
        resolve_campaign_budget(system_config.campaign, override=Decimal("-1"))
