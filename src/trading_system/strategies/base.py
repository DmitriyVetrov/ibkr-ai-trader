"""What a strategy *is*, as opposed to how it is configured.

Two kinds of fact live in this milestone, and keeping them apart is the point
of this module:

* **Structure** — a straddle is one long call and one long put, on the same
  underlying, the same expiration and the same strike. That is not a policy
  choice, it is what the word means. Changing it does not produce a differently
  tuned straddle, it produces a different strategy. Structure lives here, in
  code, where it is testable and where a configuration edit cannot alter it.
* **Policy** — which delta, which offset, which DTE, which liquidity floor.
  Those are judgements that should be reviewable in a diff, so they live in
  ``config/strategies/*.yaml`` and nothing here hard-codes one.

The registry checks a strategy's configured legs against the structure defined
here. A configuration that added a third leg, sold one, or swapped a right
would not be accepted as a "customised" straddle — it would be rejected as a
strategy the system has no definition for. That is what keeps
``do not silently add spreads`` (brief section 5) enforceable rather than
aspirational.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum, unique

from trading_system.domain.enums import (
    Direction,
    LegAction,
    MaxLossBasis,
    OptionRight,
    StrategyType,
)
from trading_system.infrastructure.settings import StrategyLegConfig

__all__ = [
    "LegTemplate",
    "StrategyStructure",
    "StrikeRelationship",
]


@unique
class StrikeRelationship(StrEnum):
    """How a multi-leg strategy's strikes must relate to each other.

    Checked after selection, on the contracts actually chosen. A strangle whose
    two legs landed on the same strike is not a cheap strangle — it is a
    straddle, and returning it under the strangle's name would misdescribe the
    position to every later stage.
    """

    #: Single-leg strategies: nothing to relate.
    NONE = "NONE"
    #: Every leg on the same strike (straddle).
    SAME = "SAME"
    #: The call strictly above the put (strangle).
    CALL_ABOVE_PUT = "CALL_ABOVE_PUT"


@dataclass(frozen=True, slots=True)
class LegTemplate:
    """Structural definition of one leg: what it is, never where it is.

    There is no strike, no expiry and no contract id here by construction. A
    template is resolved against a real chain by the deterministic contract
    selector; until then a leg is a right, a direction and a ratio.
    """

    action: LegAction
    right: OptionRight
    ratio: int = 1

    def describe(self) -> str:
        """``BUY CALL x1`` — enough for the agent to see the shape, no more."""
        return f"{self.action.value} {self.right.value} x{self.ratio}"


@dataclass(frozen=True, slots=True)
class StrategyStructure:
    """The invariant shape of one strategy.

    ``directional_view`` records what the structure expresses, not what any
    particular trade expects: a long call is ``BULLISH`` because that is what
    the payoff is, and a straddle is ``UNCERTAIN`` because it profits from
    magnitude in either direction. The deterministic validator uses this to
    refuse a bullish strategy for a bearish outlook, without an agent's opinion
    entering into it.
    """

    strategy_type: StrategyType
    legs: tuple[LegTemplate, ...]
    directional_view: Direction
    #: How much this structure can lose, as a *model* rather than a number.
    #: Declared here, with the structure, because it is a property of the
    #: payoff and not of any configuration: a long call cannot lose more than
    #: its premium whatever delta it targets. The risk engine reads this rather
    #: than assuming one formula fits every strategy — the first credit spread
    #: added to this system would break that assumption silently, and the
    #: honest answer for a structure with no computable bound is
    #: :attr:`~trading_system.domain.enums.MaxLossBasis.NOT_DEFINED`, which the
    #: risk engine refuses to size.
    max_loss_basis: MaxLossBasis = MaxLossBasis.NOT_DEFINED
    strike_relationship: StrikeRelationship = StrikeRelationship.NONE
    #: Whether every leg must share one expiration. True for every multi-leg
    #: strategy shipped today; a calendar spread would set it False, and does
    #: not exist because no configuration defines one.
    same_expiration: bool = True
    #: Whether the whole structure is managed as one position (specification
    #: section 17A). Carried here so a later milestone cannot decide otherwise
    #: for a strategy whose specification says it is one trade.
    single_position: bool = True
    summary: str = ""

    @property
    def leg_count(self) -> int:
        return len(self.legs)

    @property
    def is_multi_leg(self) -> bool:
        return len(self.legs) > 1

    def describe_legs(self) -> list[str]:
        return [leg.describe() for leg in self.legs]

    def mismatches(self, configured: Sequence[StrategyLegConfig]) -> list[str]:
        """Every way ``configured`` differs from this structure.

        Returns a list rather than a bool so a configuration error names what is
        wrong. An empty list means the configuration describes this strategy and
        not some other one.
        """
        problems: list[str] = []
        if len(configured) != len(self.legs):
            problems.append(
                f"{self.strategy_type.value} has {len(self.legs)} leg(s) but the "
                f"configuration defines {len(configured)}"
            )
            return problems

        for index, (template, leg) in enumerate(zip(self.legs, configured, strict=True)):
            if leg.action is not template.action:
                problems.append(
                    f"leg {index} must be {template.action.value}, configured as {leg.action.value}"
                )
            if leg.right is not template.right:
                problems.append(
                    f"leg {index} must be a {template.right.value}, configured as {leg.right.value}"
                )
            if leg.ratio != template.ratio:
                problems.append(
                    f"leg {index} must have ratio {template.ratio}, configured as {leg.ratio}"
                )
        return problems
