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

from datetime import timedelta
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from tests.risk.conftest import NOW, eur_usd_rates
from trading_system.domain.enums import BudgetSource, FxRateOrigin, FxStatus, RiskLimitScope
from trading_system.fx.models import FxRateTable
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
    """The operator's capital, in the operator's currency. Not converted here."""
    campaign = system_config.campaign

    assert campaign.budget == Decimal("5000")
    assert campaign.budget_currency == "EUR"
    assert isinstance(campaign.budget, Decimal)


def test_the_shipped_campaign_trades_a_different_currency_than_it_holds(system_config):
    """The whole reason the FX layer exists, pinned as a fact about the config.

    The account is based in EUR because that is where the operator's money is;
    the campaign trades USD because that is what a US-listed option is quoted
    in. Neither follows from the other, and nothing anywhere treats them as the
    same unit of account.
    """
    campaign = system_config.campaign

    assert campaign.budget_currency == "EUR"
    assert campaign.target_currency == "USD"
    assert campaign.needs_conversion is True
    # risk.yaml states its capital limits in the same currency, because a limit
    # hierarchy that depended on an exchange rate would change without an edit.
    assert system_config.risk.capital_currency == "EUR"


def test_the_reserve_is_never_allocatable(system_config):
    campaign = system_config.campaign

    assert campaign.reserve == Decimal("1000.00")
    assert campaign.allocatable_budget == Decimal("4000.00")
    assert campaign.reserve + campaign.allocatable_budget == campaign.budget


def test_the_reserve_rounds_up_so_it_is_never_quietly_smaller(tmp_config_dir):
    """A 20% reserve on 5,001 is 1,000.20, not 1,000.19."""
    _write(tmp_config_dir / "campaign.yaml", budget="5001")

    campaign = load_config(tmp_config_dir).campaign

    assert campaign.reserve == Decimal("1000.20")


def test_limits_resolve_to_the_tighter_of_each_layer(system_config):
    limits = resolve_limits(system_config)

    assert limits.max_allocation_per_trade == min(
        system_config.risk.max_allocation_per_trade,
        system_config.campaign.max_allocation_per_trade,
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
        ("max_allocation_per_trade", "2000", "above the risk ceiling"),
        ("max_open_positions", 99, "above the risk ceiling"),
        ("max_risk_per_trade", "9000", "above the total open-risk ceiling"),
    ],
)
def test_a_campaign_widening_a_global_limit_refuses_to_load(tmp_config_dir, key, value, message):
    _write(tmp_config_dir / "campaign.yaml", **{key: value})

    with pytest.raises(ConfigError, match=message):
        load_config(tmp_config_dir)


def test_a_campaign_widening_a_limit_is_not_clamped(tmp_config_dir):
    """The failure mode this rule exists to prevent, stated directly."""
    _write(tmp_config_dir / "campaign.yaml", max_allocation_per_trade="2000")

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


def test_a_campaign_narrowing_a_global_limit_loads_and_binds(tmp_config_dir):
    _write(tmp_config_dir / "campaign.yaml", max_allocation_per_trade="900")

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
    text = path.read_text(encoding="utf-8").replace('budget: "5000"', "budget: 5000.50")
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="binary floating point"):
        load_config(tmp_config_dir)


def test_a_negative_budget_is_refused(tmp_config_dir):
    _write(tmp_config_dir / "campaign.yaml", budget="-1")

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


def test_a_reserve_fraction_above_one_is_refused(tmp_config_dir):
    _write(tmp_config_dir / "campaign.yaml", reserve_fraction=1.5)

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


def test_a_minimum_allocation_no_trade_could_satisfy_is_refused(tmp_config_dir):
    """min 2000 against a 1500 per-trade ceiling admits nothing at all."""
    _write(tmp_config_dir / "campaign.yaml", min_allocation="2000")

    with pytest.raises(ConfigError, match="no trade could ever"):
        load_config(tmp_config_dir)


def test_a_minimum_allocation_above_the_allocatable_budget_is_refused(tmp_config_dir):
    _write(
        tmp_config_dir / "campaign.yaml",
        budget="1000",
        min_allocation="900",
        max_allocation_per_trade="950",
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


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("treat_as_campaign_currency", ["USD"], "has been removed"),
        ("allow_conversion", True, "no longer a switch"),
    ],
)
def test_the_parity_assumption_cannot_be_reintroduced(tmp_config_dir, key, value, message):
    """The two settings that used to make two currencies equal now fail to load.

    ``treat_as_campaign_currency`` accepted a foreign currency as the
    campaign's own *without a rate*, which asserted that a dollar and a euro
    were the same amount of money. ``allow_conversion`` was its companion, and
    was refused outright because no rate source existed. Both are gone, and a
    configuration still carrying either is a load failure with a message naming
    the replacement - not an unknown-key error, which would leave an operator
    guessing which currency their figures are now in.
    """
    payload = yaml.safe_load((tmp_config_dir / "campaign.yaml").read_text(encoding="utf-8"))
    payload["currency_policy"][key] = value
    (tmp_config_dir / "campaign.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=message):
        load_config(tmp_config_dir)


@pytest.mark.parametrize(
    ("file", "old", "new"),
    [
        ("campaign.yaml", "budget_eur", "budget"),
        ("campaign.yaml", "min_allocation_eur", "min_allocation"),
        ("risk.yaml", "max_daily_loss_eur", "max_daily_loss"),
        ("risk.yaml", "min_option_price_eur", "min_option_price"),
    ],
)
def test_a_currency_in_a_key_name_is_refused_with_the_replacement_named(
    tmp_config_dir, file, old, new
):
    """A ``_eur`` suffix on a figure that now holds dollars would be a lie.

    ``extra="forbid"`` would reject these anyway, with a message naming an
    unexpected field. That is loud and unhelpful: the suffix used to mean
    something, the meaning changed, and an operator needs to be told which of
    two currencies the value is now declared in rather than left to guess and
    be wrong by the exchange rate.
    """
    payload = yaml.safe_load((tmp_config_dir / file).read_text(encoding="utf-8"))
    payload[old] = payload.pop(new)
    (tmp_config_dir / file).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError, match=f"{old}  ->  {new}"):
        load_config(tmp_config_dir)


def test_the_two_capital_files_must_agree_on_their_currency(tmp_config_dir):
    """Otherwise 'a child may not widen a parent' would depend on a rate."""
    payload = yaml.safe_load((tmp_config_dir / "risk.yaml").read_text(encoding="utf-8"))
    payload["capital_currency"] = "USD"
    (tmp_config_dir / "risk.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="must state them in the same currency"):
        load_config(tmp_config_dir)


def test_a_zero_budget_loads_and_funds_nothing(tmp_config_dir):
    """Zero is handled explicitly rather than treated as unset."""
    _write(
        tmp_config_dir / "campaign.yaml",
        budget="0",
        min_allocation="0",
        max_allocation_per_trade="0",
        max_risk_per_trade="0",
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

    assert budget == system_config.campaign.budget
    assert reserve == system_config.campaign.reserve
    assert source is BudgetSource.CONFIG


def test_a_negative_override_is_refused(system_config):
    with pytest.raises(LimitResolutionError, match="less than nothing"):
        resolve_campaign_budget(system_config.campaign, override=Decimal("-1"))


# ---------------------------------------------------------------------------
# The currency step: source budget -> explicit FX -> target currency
# ---------------------------------------------------------------------------
#
# The six cases the brief enumerates, at the layer where the conversion is
# actually applied. Everything downstream - the engine, the allocator, the
# order builder - compares figures that came out of here, so a defect at this
# layer is a defect in every one of them.
def test_case_1_a_eur_account_with_a_usd_campaign_converts_rather_than_refusing(
    system_config,
):
    """EUR capital, USD campaign, a rate available: a valid conversion.

    Not CURRENCY_MISMATCH. A mismatch is the *expected* state for a European
    account trading US options, and it is only an error when the system cannot
    determine the conversion.
    """
    limits = resolve_limits(system_config, fx_rates=eur_usd_rates(), as_of=NOW)

    assert limits.budget_currency == "EUR"
    assert limits.target_currency == "USD"
    assert limits.limit_currency == "USD"
    assert limits.convertible is True
    assert limits.fx is not None and limits.fx.status is FxStatus.VALID


def test_case_2_no_rate_fails_closed_and_never_converts_one_to_one(system_config):
    """No rate, no converted figure, and emphatically no parity."""
    limits = resolve_limits(system_config, fx_rates=FxRateTable(), as_of=NOW)

    assert limits.convertible is False
    assert limits.fx is not None and limits.fx.status is FxStatus.UNAVAILABLE
    assert limits.fx.rate is None, "a failed conversion carries no rate at all"
    assert limits.fx.converted_amount is None

    # The figures that come back are the DECLARED ones, and they say so. This
    # is the property that stops them being compared with a dollar price:
    # nothing reads a currency off a field name.
    assert limits.limit_currency == "EUR"
    assert limits.campaign_budget == Decimal("5000")
    assert limits.usable_against("USD") is False


def test_case_3_a_fixed_test_rate_produces_the_arithmetic_by_hand(system_config):
    """EUR 5,000 x 1.10 = USD 5,500, and every limit converts at the same rate."""
    limits = resolve_limits(system_config, fx_rates=eur_usd_rates(), as_of=NOW)

    assert limits.campaign_budget == Decimal("5500.00")
    assert limits.campaign_reserve == Decimal("1100.00")
    assert limits.max_allocation_per_trade == Decimal("1650.00")
    assert limits.max_total_open_risk == Decimal("4400.00")
    assert limits.max_daily_loss == Decimal("825.00")

    # And the declared originals are still there, in the currency they were
    # declared in. The source budget is never converted in place.
    assert limits.declared["campaign_budget"] == Decimal("5000")
    assert limits.declared["max_allocation_per_trade"] == Decimal("1500")
    assert limits.budget_currency == "EUR"


def test_a_stale_rate_is_not_a_rate(system_config):
    """Freshness is checked before the arithmetic, never annotated after it."""
    window = system_config.campaign.currency_policy.max_rate_age_seconds
    old = NOW - timedelta(seconds=window + 1)

    limits = resolve_limits(system_config, fx_rates=eur_usd_rates(as_of=old), as_of=NOW)

    assert limits.fx is not None and limits.fx.status is FxStatus.STALE
    assert limits.convertible is False
    assert limits.limit_currency == "EUR"


def test_a_rate_inside_the_window_still_converts(system_config):
    window = system_config.campaign.currency_policy.max_rate_age_seconds
    recent = NOW - timedelta(seconds=window - 1)

    limits = resolve_limits(system_config, fx_rates=eur_usd_rates(as_of=recent), as_of=NOW)

    assert limits.convertible is True


def test_converted_ceilings_round_down_and_the_reserve_rounds_up(system_config):
    """Both roundings fall on the conservative side of the same subtraction.

    A ceiling rounded up would permit marginally more than policy states, every
    time, in the direction nobody notices. A reserve rounded down would hold
    back marginally less. So allocatable capital can only ever shrink under
    conversion, never grow.
    """
    awkward = Decimal("1.234567")
    limits = resolve_limits(system_config, fx_rates=eur_usd_rates(rate=awkward), as_of=NOW)

    assert limits.campaign_budget == Decimal("6172.83"), "5000 x 1.234567 = 6172.835, down"
    assert limits.campaign_reserve == Decimal("1234.57"), "1000 x 1.234567 = 1234.567, up"
    exact = Decimal("5000") * awkward - Decimal("1000.00") * awkward
    assert limits.campaign_budget - limits.campaign_reserve <= exact


def test_a_campaign_trading_its_own_currency_records_the_identity(tmp_config_dir):
    """A same-currency campaign is a real configuration, not a special case.

    It runs the same conversion path everything else does and records an
    identity conversion, so the branch cannot rot from disuse and the artifact
    says explicitly that no rate was needed rather than leaving a reader to
    infer it from the absence of one.
    """
    payload = yaml.safe_load((tmp_config_dir / "campaign.yaml").read_text(encoding="utf-8"))
    payload["budget_currency"] = "USD"
    payload["currency_policy"]["target_currency"] = "USD"
    (tmp_config_dir / "campaign.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    risk_payload = yaml.safe_load((tmp_config_dir / "risk.yaml").read_text(encoding="utf-8"))
    risk_payload["capital_currency"] = "USD"
    (tmp_config_dir / "risk.yaml").write_text(
        yaml.safe_dump(risk_payload, sort_keys=False), encoding="utf-8"
    )

    limits = resolve_limits(load_config(tmp_config_dir), fx_rates=FxRateTable(), as_of=NOW)

    assert limits.needs_conversion is False
    assert limits.convertible is True
    assert limits.fx is not None
    assert limits.fx.rate_origin is FxRateOrigin.IDENTITY
    assert limits.campaign_budget == Decimal("5000"), "unchanged, not multiplied by anything"


def test_limits_cannot_claim_a_currency_they_are_not_in(system_config):
    """The label and the arithmetic are checked against each other.

    A money field labelled with a currency it is not in is the exact defect
    this milestone removed, so the model refuses to be constructed that way
    rather than trusting whoever built it.
    """
    limits = resolve_limits(system_config, fx_rates=eur_usd_rates(), as_of=NOW)

    with pytest.raises(ValidationError, match="labelled"):
        limits.model_copy(update={"fx": None}).model_validate({**limits.model_dump(), "fx": None})
