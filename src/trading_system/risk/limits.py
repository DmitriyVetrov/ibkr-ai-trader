"""Resolving the limit hierarchy into the limits that actually apply.

Four layers declare limits, and the order between them is the architecture:

.. code-block:: text

    config/risk.yaml           GLOBAL    the outer boundary of the whole system
          |
    config/campaign.yaml       CAMPAIGN  what this campaign permits within it
          |
    config/strategies/*.yaml   STRATEGY  what this strategy permits within that
          |
    the candidate itself       POSITION  what one position may commit

A child may **narrow** a parent and may never **widen** one. Two things follow,
and both are deliberate:

* Widening is a *configuration load failure*, not a clamp. It is caught in
  :mod:`trading_system.infrastructure.settings` when the files are read, so a
  system that starts is a system whose limits are consistent. A clamped limit
  would run correctly and be invisible in the diff that introduced it.
* Resolution here is therefore only ever a ``min``. If two layers disagree
  after loading succeeded, the tighter one is the one both layers permit, and
  taking it cannot weaken anything.

:class:`~trading_system.risk.models.RiskLimits` records which layer supplied
each effective value, so "why is the ceiling 1200 when risk.yaml says 1500" is
answerable from the stored artifact rather than by reading two YAML files as
they exist today — which, by the time anyone asks, they will not.

**There is a fifth step, and it is a currency rather than a layer.** The limits
are declared in the operator's own currency and the instruments are quoted in
the currency they trade in:

.. code-block:: text

    declared    5000 EUR    budget_currency
        |
        |   x EUR/USD, captured from the broker with the balance it converts
        v
    effective   5850 USD    target_currency, and what every comparison uses

The conversion happens **once**, here, against every money limit at the same
rate — never per comparison, which would let two limits derived from one rate
disagree in the last digit depending on the order they were multiplied in. The
declared figures are kept beside the converted ones, so the record never loses
the currency the operator actually holds.

Nothing in this module invents a rate. When no valid one is available the
limits come back in the currency they were declared in, marked not convertible,
and the risk engine rejects the candidate with ``FX_RATE_UNAVAILABLE`` before
any of the figures is compared with anything.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from trading_system.domain.enums import BudgetSource, FxStatus, RiskLimitScope
from trading_system.fx.convert import convert
from trading_system.fx.models import FxConversion, FxRateTable
from trading_system.infrastructure.settings import CampaignConfig, RiskConfig, SystemConfig
from trading_system.risk.models import RiskLimits, StrategyRiskProfile

__all__ = [
    "LimitResolutionError",
    "campaign_conversion",
    "resolve_campaign_budget",
    "resolve_limits",
]

#: Converted limits are quantised to the cent, and **downwards**. A limit
#: rounded up would permit marginally more than the policy states, every time,
#: in the direction nobody would notice. The budget itself is rounded down for
#: the same reason; the reserve is rounded *up*, so both errors fall on the
#: conservative side of the same subtraction.
_CENT = Decimal("0.01")


class LimitResolutionError(RuntimeError):
    """A limit could not be resolved and will not be guessed at."""


def resolve_campaign_budget(
    campaign: CampaignConfig, *, override: Decimal | None = None
) -> tuple[Decimal, Decimal, BudgetSource]:
    """The campaign budget in force, its reserve, and where it came from.

    Both figures are in ``campaign.budget_currency``: this is the *declared*
    envelope, before any conversion. Converting it is a separate step with its
    own failure modes, and folding the two together would make "how much did
    the operator commit" unanswerable without a rate.

    ``CAMPAIGN_BUDGET`` in the environment overrides the committed
    configuration. That is an operator changing the size of their own capital
    envelope, not an agent widening a safety limit — every per-trade and
    concentration limit still applies on top of it. The *source* travels with
    the figure so a stored authorisation can never imply that the committed
    configuration authorised an amount it does not contain. The override
    carries no currency of its own; it is an amount in the currency the
    configuration already declares, because redenominating a campaign is a
    decision that belongs in a reviewed file rather than in a shell.

    The reserve is recomputed from the override rather than carried over: a
    20% reserve on a budget that changed is 20% of the new budget, and keeping
    the old absolute figure would silently change the reserve fraction.
    """
    if override is None:
        return campaign.budget, campaign.reserve, BudgetSource.CONFIG

    if override < 0:
        raise LimitResolutionError(
            f"CAMPAIGN_BUDGET={override} is negative; a campaign cannot hold less than nothing"
        )
    fraction = Decimal(str(campaign.reserve_fraction))
    reserve = (override * fraction).quantize(_CENT, rounding=ROUND_CEILING)
    return override, reserve, BudgetSource.ENVIRONMENT


def campaign_conversion(
    campaign: CampaignConfig,
    *,
    rates: FxRateTable | None = None,
    as_of: datetime | None = None,
) -> FxConversion:
    """The one conversion that carries this campaign into its traded currency.

    Returns an :class:`~trading_system.fx.models.FxConversion` of a single unit
    rather than of the budget, because the same rate applies to every money
    limit and converting the unit makes that explicit — there is one rate in
    the record, not one per limit that a reader has to check are equal.

    Called with no rates or no decision instant — which is what happens when a
    command prints the limits without having captured an account — the answer
    is a perfectly ordinary ``UNAVAILABLE``. That is not an error state to be
    worked around: "nobody has looked up a rate" and "the rate is missing"
    deserve the same treatment, which is that no converted figure exists.

    An instant is *required* to use a rate, not merely convenient. Freshness is
    the difference between a rate and a rate from another market, and there is
    no instant to measure it against here that would not be one this module
    made up.
    """
    return convert(
        Decimal(1),
        from_currency=campaign.budget_currency,
        to_currency=campaign.target_currency,
        rates=rates if rates is not None else FxRateTable(),
        as_of=as_of,
        max_age_seconds=float(campaign.currency_policy.max_rate_age_seconds),
    )


def _converted(amount: Decimal, fx: FxConversion) -> Decimal:
    """One money limit in the traded currency, rounded down to the cent.

    ``fx`` must be a successful conversion; callers check ``fx.ok`` once for
    the whole set rather than per limit, because a set of limits half of which
    converted would be worse than none.
    """
    return (amount * (fx.rate or Decimal(1))).quantize(_CENT, rounding=ROUND_FLOOR)


def resolve_limits(
    config: SystemConfig,
    *,
    profile: StrategyRiskProfile | None = None,
    budget_override: Decimal | None = None,
    fx_rates: FxRateTable | None = None,
    as_of: datetime | None = None,
) -> RiskLimits:
    """Intersect every layer into the limits that apply to one evaluation.

    ``profile`` is optional because some limits — the campaign budget, the
    position count — are the same whatever strategy is proposed, and a run
    reports them before it has a candidate in hand.

    ``fx_rates`` and ``as_of`` are optional for the same kind of reason:
    ``risk validate`` prints the limits without an account and therefore
    without a rate. What it gets back is the honest thing — the declared
    figures, labelled with the currency they are declared in, and marked not
    convertible. Nothing downstream may compare those with a price, and
    :meth:`RiskLimits.usable_against` is how that is checked rather than
    assumed.
    """
    risk: RiskConfig = config.risk
    campaign: CampaignConfig = config.campaign

    budget, reserve, _ = resolve_campaign_budget(campaign, override=budget_override)

    scopes: dict[str, RiskLimitScope] = {}

    def narrower_money(name: str, global_value: Decimal, campaign_value: Decimal) -> Decimal:
        """The tighter of two ceilings, recording which layer supplied it."""
        if campaign_value <= global_value:
            scopes[name] = RiskLimitScope.CAMPAIGN
            return campaign_value
        # Unreachable for a configuration that loaded: SystemConfig refuses a
        # campaign that widens a global limit. Kept as a second line of defence
        # because a limit resolved the wrong way is a limit that permits a
        # trade nobody authorised.
        scopes[name] = RiskLimitScope.GLOBAL
        return global_value

    def narrower_count(name: str, global_value: int, campaign_value: int) -> int:
        if campaign_value <= global_value:
            scopes[name] = RiskLimitScope.CAMPAIGN
            return campaign_value
        scopes[name] = RiskLimitScope.GLOBAL
        return global_value

    max_allocation = narrower_money(
        "max_allocation_per_trade",
        risk.max_allocation_per_trade,
        campaign.max_allocation_per_trade,
    )
    max_risk_per_trade = narrower_money(
        "max_risk_per_trade",
        risk.max_total_open_risk,
        campaign.max_risk_per_trade,
    )
    max_open_positions = narrower_count(
        "max_open_positions", risk.max_open_positions, campaign.max_open_positions
    )

    scopes["campaign_budget"] = RiskLimitScope.CAMPAIGN
    scopes["campaign_reserve"] = RiskLimitScope.CAMPAIGN
    scopes["min_allocation_per_trade"] = RiskLimitScope.CAMPAIGN
    scopes["max_total_open_risk"] = RiskLimitScope.GLOBAL
    scopes["max_daily_loss"] = RiskLimitScope.GLOBAL
    scopes["max_positions_per_underlying"] = RiskLimitScope.CAMPAIGN
    scopes["max_new_positions_per_run"] = RiskLimitScope.CAMPAIGN
    scopes["max_contracts_per_trade"] = RiskLimitScope.CAMPAIGN
    scopes["max_underlying_concentration_pct"] = RiskLimitScope.GLOBAL
    scopes["max_strategy_concentration_pct"] = RiskLimitScope.GLOBAL
    scopes["max_directional_exposure_pct"] = RiskLimitScope.GLOBAL
    scopes["min_opportunity_score"] = RiskLimitScope.CAMPAIGN
    scopes["max_market_data_age_seconds"] = RiskLimitScope.GLOBAL
    scopes["max_account_snapshot_age_seconds"] = RiskLimitScope.CAMPAIGN

    if profile is not None:
        # A strategy may only narrow, and the registry has already intersected
        # its price band and spread ceiling with the global ones. Recording the
        # scope keeps the artifact honest about which layer bound the trade.
        scopes["option_price_band"] = RiskLimitScope.STRATEGY
        scopes["max_bid_ask_spread_pct"] = RiskLimitScope.STRATEGY
        scopes["dte_window"] = RiskLimitScope.STRATEGY

    # --- the currency step -------------------------------------------------
    #
    # Declared first, converted second, and both kept. Every figure above is in
    # campaign.budget_currency; every figure the engine compares against a
    # price has to be in campaign.target_currency, and the step between them is
    # one rate applied to all of them at once.
    declared: dict[str, Decimal] = {
        "campaign_budget": budget,
        "campaign_reserve": reserve,
        "max_allocation_per_trade": max_allocation,
        "min_allocation_per_trade": campaign.min_allocation,
        "max_risk_per_trade": max_risk_per_trade,
        "max_total_open_risk": risk.max_total_open_risk,
        "max_daily_loss": risk.max_daily_loss,
    }
    fx = campaign_conversion(campaign, rates=fx_rates, as_of=as_of)

    if fx.status is FxStatus.VALID:
        effective = {name: _converted(value, fx) for name, value in declared.items()}
        # The reserve is the one figure rounded the other way, so that
        # budget - reserve can only ever shrink under conversion.
        effective["campaign_reserve"] = (reserve * (fx.rate or Decimal(1))).quantize(
            _CENT, rounding=ROUND_CEILING
        )
        limit_currency = campaign.target_currency
    else:
        effective = dict(declared)
        limit_currency = campaign.budget_currency

    return RiskLimits(
        budget_currency=campaign.budget_currency,
        target_currency=campaign.target_currency,
        limit_currency=limit_currency,
        fx=fx,
        declared=declared,
        max_fx_rate_age_seconds=campaign.currency_policy.max_rate_age_seconds,
        campaign_budget=effective["campaign_budget"],
        campaign_reserve=effective["campaign_reserve"],
        max_allocation_per_trade=effective["max_allocation_per_trade"],
        min_allocation_per_trade=effective["min_allocation_per_trade"],
        max_risk_per_trade=effective["max_risk_per_trade"],
        max_total_open_risk=effective["max_total_open_risk"],
        max_daily_loss=effective["max_daily_loss"],
        max_open_positions=max_open_positions,
        max_positions_per_underlying=campaign.limits.max_positions_per_underlying,
        max_new_positions_per_run=campaign.limits.max_new_positions_per_run,
        max_contracts_per_trade=campaign.limits.max_contracts_per_trade,
        max_underlying_concentration_pct=risk.max_underlying_concentration_pct,
        max_strategy_concentration_pct=risk.max_strategy_concentration_pct,
        max_directional_exposure_pct=risk.max_directional_exposure_pct,
        min_opportunity_score=campaign.min_opportunity_score,
        max_market_data_age_seconds=risk.max_market_data_age_seconds,
        max_account_snapshot_age_seconds=campaign.account.max_snapshot_age_seconds,
        require_account_snapshot=campaign.account.require_account_snapshot,
        require_daily_loss_tracking=campaign.account.require_daily_loss_tracking,
        block_on_unknown_daily_loss=campaign.account.block_on_unknown_daily_loss,
        risk_config_version=risk.config_version,
        campaign_id=campaign.campaign_id,
        scopes=scopes,
    )
