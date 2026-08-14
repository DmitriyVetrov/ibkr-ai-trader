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
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from trading_system.domain.enums import BudgetSource, RiskLimitScope
from trading_system.infrastructure.settings import CampaignConfig, RiskConfig, SystemConfig
from trading_system.risk.models import RiskLimits, StrategyRiskProfile

__all__ = ["LimitResolutionError", "resolve_campaign_budget", "resolve_limits"]


class LimitResolutionError(RuntimeError):
    """A limit could not be resolved and will not be guessed at."""


def resolve_campaign_budget(
    campaign: CampaignConfig, *, override: Decimal | None = None
) -> tuple[Decimal, Decimal, BudgetSource]:
    """The campaign budget in force, its reserve, and where it came from.

    ``CAMPAIGN_BUDGET_EUR`` in the environment overrides the committed
    configuration. That is an operator changing the size of their own capital
    envelope, not an agent widening a safety limit — every per-trade and
    concentration limit still applies on top of it. The *source* travels with
    the figure so a stored authorisation can never imply that the committed
    configuration authorised an amount it does not contain.

    The reserve is recomputed from the override rather than carried over: a
    20% reserve on a budget that changed is 20% of the new budget, and keeping
    the old absolute figure would silently change the reserve fraction.
    """
    if override is None:
        return campaign.budget_eur, campaign.reserve_eur, BudgetSource.CONFIG

    if override < 0:
        raise LimitResolutionError(
            f"CAMPAIGN_BUDGET_EUR={override} is negative; a campaign cannot hold less than nothing"
        )
    fraction = Decimal(str(campaign.reserve_fraction))
    reserve = (override * fraction).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return override, reserve, BudgetSource.ENVIRONMENT


def resolve_limits(
    config: SystemConfig,
    *,
    profile: StrategyRiskProfile | None = None,
    budget_override: Decimal | None = None,
) -> RiskLimits:
    """Intersect every layer into the limits that apply to one evaluation.

    ``profile`` is optional because some limits — the campaign budget, the
    position count — are the same whatever strategy is proposed, and a run
    reports them before it has a candidate in hand.
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
        risk.max_allocation_per_trade_eur,
        campaign.max_allocation_per_trade_eur,
    )
    max_risk_per_trade = narrower_money(
        "max_risk_per_trade",
        risk.max_total_open_risk_eur,
        campaign.max_risk_per_trade_eur,
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

    return RiskLimits(
        campaign_budget=budget,
        campaign_reserve=reserve,
        max_allocation_per_trade=max_allocation,
        min_allocation_per_trade=campaign.min_allocation_eur,
        max_risk_per_trade=max_risk_per_trade,
        max_total_open_risk=risk.max_total_open_risk_eur,
        max_daily_loss=risk.max_daily_loss_eur,
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
        allow_currency_conversion=campaign.currency_policy.allow_conversion,
        accepted_currencies=sorted(
            {campaign.currency, *campaign.currency_policy.treat_as_campaign_currency}
        ),
        risk_config_version=risk.config_version,
        campaign_id=campaign.campaign_id,
        scopes=scopes,
    )
