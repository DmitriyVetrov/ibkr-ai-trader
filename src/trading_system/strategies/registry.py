"""The strategy registry: the allow-list, resolved and checked.

Three sources meet here, and the order of precedence is the architecture:

.. code-block:: text

    config/risk.yaml          the outer boundary - no strategy may widen it
          |
    config/strategies/*.yaml  policy per strategy - may only narrow
          |
    strategies/*.py           structure - what the strategy *is*, in code

A strategy the registry does not contain is not tradeable, whatever an agent
proposes and whatever a prompt says. A strategy that tries to widen a global
risk limit does not get a warning and a clamp — the registry refuses to build,
because a silently clamped limit is a limit nobody can see.

The registry also answers the mapping question — *which strategies may answer
hypothesis B?* — and it answers it from each strategy's own
``applicable_hypotheses``. There is deliberately no second table anywhere: a
hypothesis-to-strategy list maintained apart from the strategies themselves is
a list that will disagree with them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from trading_system.domain.enums import (
    Direction,
    ExpirationSelectionPolicy,
    LegAction,
    MarketHypothesis,
    MaxLossBasis,
    OptionDataField,
    OptionRight,
    SecurityType,
    StrategyType,
    StrikeSelectionPolicy,
)
from trading_system.infrastructure.settings import (
    ExitPolicyConfig,
    RiskConfig,
    StrategyConfig,
    StrategyLegConfig,
    SystemConfig,
)
from trading_system.strategies import long_call, long_put, long_straddle, long_strangle
from trading_system.strategies.base import LegTemplate, StrategyStructure, StrikeRelationship
from trading_system.strategies.models import StrategyOption

__all__ = [
    "STRUCTURES",
    "LegSpecification",
    "StrategyRegistry",
    "StrategyRegistryError",
    "StrategySpecification",
]


class StrategyRegistryError(RuntimeError):
    """A configured strategy cannot be admitted, and will not be repaired."""


#: Every strategy the system has a definition for. A ``StrategyType`` absent
#: from this mapping cannot be registered even with a configuration file, and a
#: configuration file absent from ``config/strategies/`` cannot be traded even
#: with a definition here. Both are required, deliberately.
STRUCTURES: Mapping[StrategyType, StrategyStructure] = {
    StrategyType.LONG_CALL: long_call.STRUCTURE,
    StrategyType.LONG_PUT: long_put.STRUCTURE,
    StrategyType.LONG_STRADDLE: long_straddle.STRUCTURE,
    StrategyType.LONG_STRANGLE: long_strangle.STRUCTURE,
}


@dataclass(frozen=True, slots=True)
class LegSpecification:
    """One resolved leg: its structure plus the policy that will place it."""

    index: int
    template: LegTemplate
    strike_policy: StrikeSelectionPolicy
    target_delta: float | None = None
    strike_offset_pct: float | None = None

    @property
    def right(self) -> OptionRight:
        return self.template.right

    @property
    def action(self) -> LegAction:
        return self.template.action

    @property
    def ratio(self) -> int:
        return self.template.ratio


@dataclass(frozen=True, slots=True)
class StrategySpecification:
    """One strategy, with every limit already resolved against global risk.

    Every numeric bound here is the *effective* one — the tighter of the
    strategy's own value and the risk policy's. A consumer never has to
    remember to intersect them, and cannot forget to.
    """

    strategy_id: StrategyType
    name: str
    version: str
    enabled: bool
    description: str
    structure: StrategyStructure
    applicable_hypotheses: tuple[MarketHypothesis, ...]
    allowed_underlying_types: tuple[SecurityType, ...]
    legs: tuple[LegSpecification, ...]

    dte_min: int
    dte_max: int
    expiration_rule: ExpirationSelectionPolicy
    target_dte: int | None
    event_max_days_after: int | None

    required_option_fields: tuple[OptionDataField, ...]
    require_option_liquidity: bool

    min_option_price_eur: Decimal
    max_option_price_eur: Decimal
    min_implied_volatility: float | None
    max_implied_volatility: float | None
    min_open_interest: int
    min_daily_volume: int
    max_bid_ask_spread_pct: float

    exit_policy: ExitPolicyConfig

    # --- questions consumers ask -------------------------------------------
    @property
    def directional_view(self) -> Direction:
        return self.structure.directional_view

    @property
    def strike_relationship(self) -> StrikeRelationship:
        return self.structure.strike_relationship

    @property
    def max_loss_basis(self) -> MaxLossBasis:
        """How much this strategy can lose, as declared by its structure.

        Read by the risk engine through the candidate rather than computed
        there: a generic "max loss is the premium" formula is right for every
        strategy shipped today and wrong for the first one that sells an
        option.
        """
        return self.structure.max_loss_basis

    @property
    def is_multi_leg(self) -> bool:
        return self.structure.is_multi_leg

    @property
    def aligns_to_events(self) -> bool:
        return self.expiration_rule is ExpirationSelectionPolicy.EVENT_ALIGNED

    def applies_to(self, hypothesis: MarketHypothesis) -> bool:
        return hypothesis in self.applicable_hypotheses

    def accepts_underlying(self, security_type: SecurityType) -> bool:
        return security_type in self.allowed_underlying_types

    def covers_horizon(self, horizon_days: int) -> bool:
        """Whether a contract in this strategy's window can express the horizon.

        Deliberately an overlap test rather than containment: an outlook over 21
        days is expressible by a 30-day contract, and demanding an exact match
        would reject every legitimate pairing.
        """
        return self.dte_min <= horizon_days or horizon_days <= self.dte_max

    def to_option(self) -> StrategyOption:
        """The metadata the strategy agent is shown.

        Structure, hypotheses and window — never a strike, never an expiry,
        never a contract. The agent chooses *what*; it is never told enough to
        choose *which*.
        """
        return StrategyOption(
            strategy_id=self.strategy_id,
            name=self.name,
            version=self.version,
            description=self.description or self.structure.summary,
            structure=self.structure.summary,
            applicable_hypotheses=list(self.applicable_hypotheses),
            legs=self.structure.describe_legs(),
            leg_count=self.structure.leg_count,
            directional_view=self.directional_view,
            single_position=self.structure.single_position,
            aligns_to_events=self.aligns_to_events,
            dte_min=self.dte_min,
            dte_max=self.dte_max,
        )


class StrategyRegistry:
    """The resolved allow-list. Built once, from configuration, and immutable."""

    def __init__(self, specifications: Iterable[StrategySpecification]) -> None:
        resolved = list(specifications)
        identifiers = [s.strategy_id for s in resolved]
        duplicates = sorted({s.value for s in identifiers if identifiers.count(s) > 1})
        if duplicates:
            raise StrategyRegistryError(f"duplicate strategy definitions: {', '.join(duplicates)}")
        self._by_id = {s.strategy_id: s for s in resolved}

    # --- construction ------------------------------------------------------
    @classmethod
    def from_config(cls, config: SystemConfig) -> StrategyRegistry:
        """Resolve every configured strategy against structure and risk."""
        return cls(
            _specification(name, strategy, config.risk)
            for name, strategy in sorted(config.strategies.items())
        )

    # --- lookups -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._by_id

    def all(self) -> list[StrategySpecification]:
        return [self._by_id[key] for key in sorted(self._by_id, key=lambda s: s.value)]

    def enabled(self) -> list[StrategySpecification]:
        return [s for s in self.all() if s.enabled]

    def get(self, strategy_id: StrategyType) -> StrategySpecification | None:
        return self._by_id.get(strategy_id)

    def require(self, strategy_id: StrategyType) -> StrategySpecification:
        specification = self._by_id.get(strategy_id)
        if specification is None:
            raise StrategyRegistryError(
                f"{strategy_id.value} has no configuration in config/strategies/; a strategy "
                f"without one is not tradeable, whatever proposes it"
            )
        return specification

    def for_hypothesis(self, hypothesis: MarketHypothesis) -> list[StrategySpecification]:
        """Enabled strategies that declare this hypothesis, in a stable order.

        An empty list is a real and expected answer — hypothesis ``E`` has one
        today — and it means ``NO_TRADE`` without a model call.
        """
        return [s for s in self.enabled() if s.applies_to(hypothesis)]

    def options_for(self, hypothesis: MarketHypothesis) -> list[StrategyOption]:
        return [s.to_option() for s in self.for_hypothesis(hypothesis)]

    def hypothesis_map(self) -> dict[MarketHypothesis, list[StrategyType]]:
        """The mapping, derived from the strategies rather than declared."""
        return {
            hypothesis: [s.strategy_id for s in self.for_hypothesis(hypothesis)]
            for hypothesis in MarketHypothesis
        }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _specification(name: str, strategy: StrategyConfig, risk: RiskConfig) -> StrategySpecification:
    structure = STRUCTURES.get(strategy.strategy_type)
    if structure is None:
        raise StrategyRegistryError(
            f"strategy '{name}' declares {strategy.strategy_type.value}, which has no "
            f"structural definition in trading_system.strategies. A configuration file is "
            f"not a definition: add the structure in code, deliberately, or remove the file."
        )

    mismatches = structure.mismatches(strategy.legs)
    if mismatches:
        raise StrategyRegistryError(
            f"strategy '{name}' does not describe a {strategy.strategy_type.value}: "
            + "; ".join(mismatches)
            + ". Configuration tunes a strategy; it does not redefine one."
        )

    _refuse_widening(name, strategy, risk)

    return StrategySpecification(
        strategy_id=strategy.strategy_type,
        name=strategy.name,
        version=strategy.spec_version,
        enabled=strategy.enabled,
        description=strategy.description.strip(),
        structure=structure,
        applicable_hypotheses=tuple(strategy.applicable_hypotheses),
        allowed_underlying_types=tuple(strategy.allowed_underlying_types),
        legs=tuple(
            _leg(index, template, leg, strategy)
            for index, (template, leg) in enumerate(zip(structure.legs, strategy.legs, strict=True))
        ),
        dte_min=max(strategy.dte_min, risk.dte_min),
        dte_max=min(strategy.dte_max, risk.dte_max),
        expiration_rule=strategy.expiration_policy.rule,
        target_dte=strategy.expiration_policy.target_dte,
        event_max_days_after=strategy.expiration_policy.event_max_days_after,
        required_option_fields=tuple(strategy.required_option_fields),
        require_option_liquidity=strategy.require_option_liquidity,
        min_option_price_eur=max(strategy.min_option_price_eur, risk.min_option_price_eur),
        max_option_price_eur=min(strategy.max_option_price_eur, risk.max_option_price_eur),
        min_implied_volatility=strategy.min_implied_volatility,
        max_implied_volatility=strategy.max_implied_volatility,
        min_open_interest=max(strategy.liquidity.min_open_interest, risk.min_open_interest),
        min_daily_volume=max(strategy.liquidity.min_daily_volume, risk.min_daily_volume),
        max_bid_ask_spread_pct=min(
            strategy.liquidity.max_bid_ask_spread_pct, risk.max_bid_ask_spread_pct
        ),
        exit_policy=strategy.exit_policy,
    )


def _leg(
    index: int,
    template: LegTemplate,
    leg: StrategyLegConfig,
    strategy: StrategyConfig,
) -> LegSpecification:
    return LegSpecification(
        index=index,
        template=template,
        strike_policy=leg.strike_policy,
        target_delta=strategy.leg_target_delta(leg),
        strike_offset_pct=strategy.leg_offset_pct(leg),
    )


def _refuse_widening(name: str, strategy: StrategyConfig, risk: RiskConfig) -> None:
    """A strategy may narrow a global risk limit. It may never widen one.

    Checked here as well as in :class:`~trading_system.infrastructure.settings.SystemConfig`
    because the registry can be built from a configuration a test assembled by
    hand, and the invariant has to hold for that path too. Duplication of a
    safety check is not duplication worth removing.
    """
    violations: list[str] = []
    if strategy.dte_min < risk.dte_min or strategy.dte_max > risk.dte_max:
        violations.append(
            f"DTE window [{strategy.dte_min}, {strategy.dte_max}] falls outside the risk "
            f"limit [{risk.dte_min}, {risk.dte_max}]"
        )
    if strategy.min_option_price_eur < risk.min_option_price_eur:
        violations.append("min_option_price_eur is below the risk floor")
    if strategy.max_option_price_eur > risk.max_option_price_eur:
        violations.append("max_option_price_eur is above the risk ceiling")
    if strategy.liquidity.min_open_interest < risk.min_open_interest:
        violations.append("min_open_interest is below the risk floor")
    if strategy.liquidity.min_daily_volume < risk.min_daily_volume:
        violations.append("min_daily_volume is below the risk floor")
    if strategy.liquidity.max_bid_ask_spread_pct > risk.max_bid_ask_spread_pct:
        violations.append("max_bid_ask_spread_pct is above the risk ceiling")

    if violations:
        raise StrategyRegistryError(
            f"strategy '{name}' widens a global risk limit: "
            + "; ".join(violations)
            + ". The limit is not clamped silently: a strategy specification cannot "
            "overrule the risk policy."
        )
