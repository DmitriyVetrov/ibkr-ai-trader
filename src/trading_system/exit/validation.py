"""Resolving the exit policy actually in force, layer by layer.

Two jobs, both pure:

* :func:`effective_policy` combines ``config/exit.yaml`` with one strategy's
  ``exit_policy`` block and records **which layer supplied each value**. That
  is what lets a stored decision stay explicable after the configuration has
  moved on: "the trailing distance was 30% and the strategy supplied it" is not
  reconstructible from a file that has since been edited.
* :func:`configuration_report` restates the resolved hierarchy for
  ``exit validate``, so an operator can see the whole envelope without reading
  five files and doing the narrowing arithmetic by hand.

**Widening is not handled here, on purpose.** A strategy that tried to hold a
position closer to expiry than the global floor, trail looser, or lose more
never reaches this module: ``SystemConfig`` refuses to load. Resolving it here
would mean clamping, and a clamped limit is a limit nobody can see — the same
rule Milestone 6 and Milestone 7 apply to the risk hierarchy, applied to this
one.

The direction that counts as *narrowing* differs per limit and is stated with
each, because getting a direction wrong would silently enforce the inverse of
the intended safety property.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_system.domain.enums import RiskLimitScope, StrategyType
from trading_system.exit.models import ExitPolicySnapshot
from trading_system.infrastructure.settings import ExitConfig, StrategyConfig, SystemConfig

__all__ = [
    "PolicyLayer",
    "configuration_report",
    "effective_policy",
    "strategy_config_for",
]


@dataclass(frozen=True, slots=True)
class PolicyLayer:
    """One effective value, and which layer supplied it. For display."""

    name: str
    value: str
    scope: RiskLimitScope
    global_value: str
    strategy_value: str | None = None
    narrowing_rule: str = ""


def strategy_config_for(config: SystemConfig, strategy: StrategyType) -> StrategyConfig | None:
    """The configuration block for one strategy type, or ``None``.

    ``None`` is a real answer: a position may have been opened under a strategy
    whose configuration file has since been removed, and the caller reports
    ``STRATEGY_METADATA_UNAVAILABLE`` rather than falling back to the global
    defaults. Managing a position under a policy that was never chosen for it
    is not a smaller version of managing it correctly.
    """
    for candidate in config.strategies.values():
        if candidate.strategy_type is strategy:
            return candidate
    return None


def effective_policy(
    *,
    strategy: StrategyType,
    strategy_config: StrategyConfig | None,
    exit_config: ExitConfig,
) -> ExitPolicySnapshot:
    """The policy in force for one position, with every value's source recorded.

    Where the strategy states a value it narrows the global one and is used;
    where it does not, the global value binds. There is no third case: a
    strategy cannot widen, because such a configuration does not load.
    """
    scopes: dict[str, RiskLimitScope] = {}

    def resolve[T](name: str, strategy_value: T | None, global_value: T) -> T:
        if strategy_value is None:
            scopes[name] = RiskLimitScope.GLOBAL
            return global_value
        scopes[name] = RiskLimitScope.STRATEGY
        return strategy_value

    policy = strategy_config.exit_policy if strategy_config else None

    # Closing EARLIER is narrower, so the strategy's close_at_dte is at or
    # above the global force-exit threshold. Configuration load already refused
    # anything else.
    force_exit_dte = resolve(
        "expiration_force_exit_dte",
        policy.close_at_dte if policy else None,
        exit_config.expiration.force_exit_dte,
    )
    # Trailing TIGHTER is narrower: a smaller distance gives back less.
    trailing_distance = resolve(
        "trailing_distance_pct",
        policy.trailing_stop_pct if policy else None,
        exit_config.trailing.trail_distance_pct,
    )
    # Losing LESS is narrower.
    max_loss_pct = resolve(
        "max_loss_pct",
        policy.max_loss_pct if policy else None,
        exit_config.max_loss.loss_pct,
    )
    # Taking profit EARLIER is narrower. ``None`` on the strategy means it
    # states no target — permitted, and not a widening: take profit is not a
    # safety limit, and a position that never takes one is still bounded by the
    # trailing stop, the maximum loss and the expiration policy.
    if policy is not None:
        scopes["take_profit_return_pct"] = (
            RiskLimitScope.STRATEGY if policy.take_profit_pct is not None else RiskLimitScope.GLOBAL
        )
        take_profit = policy.take_profit_pct
    else:
        scopes["take_profit_return_pct"] = RiskLimitScope.GLOBAL
        take_profit = exit_config.take_profit.return_pct

    # Global-only values. Stated explicitly so the scope map is complete and a
    # reader never has to infer that an absent key means "global".
    for name in (
        "expiration_warning_dte",
        "trailing_activation_return_pct",
        "trailing_min_improvement_pct",
        "quote_field",
        "max_quote_age_seconds",
    ):
        scopes[name] = RiskLimitScope.GLOBAL

    return ExitPolicySnapshot(
        policy_version=exit_config.policy_version,
        strategy=strategy,
        expiration_warning_dte=max(exit_config.expiration.warning_dte, force_exit_dte),
        expiration_force_exit_dte=force_exit_dte,
        trailing_enabled=exit_config.trailing.enabled,
        trailing_activation_return_pct=exit_config.trailing.activation_return_pct,
        trailing_distance_pct=trailing_distance,
        trailing_min_improvement_pct=exit_config.trailing.min_improvement_pct,
        take_profit_enabled=exit_config.take_profit.enabled,
        take_profit_return_pct=take_profit,
        max_loss_enabled=exit_config.max_loss.enabled,
        max_loss_pct=max_loss_pct,
        thesis_enabled=exit_config.thesis.enabled,
        quote_field=exit_config.data_quality.quote_field,
        max_quote_age_seconds=exit_config.data_quality.max_quote_age_seconds,
        require_research_usable=exit_config.data_quality.require_research_usable,
        scopes=scopes,
        allow_independent_leg_exit=False,
    )


def configuration_report(config: SystemConfig) -> list[PolicyLayer]:
    """The whole exit hierarchy, resolved, for ``exit validate``.

    One row per strategy per limit, with the global value, the strategy's, and
    the rule that decides which direction is a narrowing. An operator reading
    this should be able to answer "why would this position exit sooner than
    that one" without opening a file.
    """
    rows: list[PolicyLayer] = []
    for name, strategy in sorted(config.strategies.items()):
        policy = strategy.exit_policy
        effective = effective_policy(
            strategy=strategy.strategy_type,
            strategy_config=strategy,
            exit_config=config.exit,
        )
        rows.extend(
            [
                PolicyLayer(
                    name=f"{name}.force_exit_dte",
                    value=str(effective.expiration_force_exit_dte),
                    scope=effective.scopes["expiration_force_exit_dte"],
                    global_value=str(config.exit.expiration.force_exit_dte),
                    strategy_value=str(policy.close_at_dte),
                    narrowing_rule="a LARGER dte closes earlier and is narrower",
                ),
                PolicyLayer(
                    name=f"{name}.trailing_distance_pct",
                    value=str(effective.trailing_distance_pct),
                    scope=effective.scopes["trailing_distance_pct"],
                    global_value=str(config.exit.trailing.trail_distance_pct),
                    strategy_value=str(policy.trailing_stop_pct),
                    narrowing_rule="a SMALLER distance gives back less and is narrower",
                ),
                PolicyLayer(
                    name=f"{name}.max_loss_pct",
                    value=str(effective.max_loss_pct),
                    scope=effective.scopes["max_loss_pct"],
                    global_value=str(config.exit.max_loss.loss_pct),
                    strategy_value=str(policy.max_loss_pct),
                    narrowing_rule="a SMALLER percentage loses less and is narrower",
                ),
                PolicyLayer(
                    name=f"{name}.take_profit_pct",
                    value=(
                        str(effective.take_profit_return_pct)
                        if effective.take_profit_return_pct is not None
                        else "(none)"
                    ),
                    scope=effective.scopes["take_profit_return_pct"],
                    global_value=str(config.exit.take_profit.return_pct),
                    strategy_value=(
                        str(policy.take_profit_pct)
                        if policy.take_profit_pct is not None
                        else "(none)"
                    ),
                    narrowing_rule=(
                        "a SMALLER target takes profit earlier and is narrower; null is "
                        "permitted — take profit is not a safety limit"
                    ),
                ),
            ]
        )
    return rows
