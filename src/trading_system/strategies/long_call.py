"""Long call — a single bought call.

Answers hypothesis ``B`` (predominantly up) and nothing else, because it is the
only shape here whose payoff needs a *direction* to be right. The numbers — the
delta it targets, the DTE window, the liquidity floor — are in
``config/strategies/long_call.yaml``; this module states only what the strategy
is, which no configuration may change.
"""

from __future__ import annotations

from trading_system.domain.enums import Direction, LegAction, OptionRight, StrategyType
from trading_system.strategies.base import LegTemplate, StrategyStructure, StrikeRelationship

__all__ = ["STRUCTURE"]

STRUCTURE = StrategyStructure(
    strategy_type=StrategyType.LONG_CALL,
    legs=(LegTemplate(action=LegAction.BUY, right=OptionRight.CALL, ratio=1),),
    directional_view=Direction.BULLISH,
    strike_relationship=StrikeRelationship.NONE,
    same_expiration=True,
    single_position=True,
    summary=(
        "One long call. Profits when the underlying rises far enough, soon enough, to "
        "outrun the premium paid. Loses the premium if it does not."
    ),
)
