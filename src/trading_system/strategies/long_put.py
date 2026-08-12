"""Long put — a single bought put.

The mirror of the long call, and the only shape here that answers hypothesis
``C`` (predominantly down). Its policy lives in
``config/strategies/long_put.yaml``; note that put deltas run from 0 to -1, so
the configured target is negative and the sign is validated rather than
assumed.
"""

from __future__ import annotations

from trading_system.domain.enums import Direction, LegAction, OptionRight, StrategyType
from trading_system.strategies.base import LegTemplate, StrategyStructure, StrikeRelationship

__all__ = ["STRUCTURE"]

STRUCTURE = StrategyStructure(
    strategy_type=StrategyType.LONG_PUT,
    legs=(LegTemplate(action=LegAction.BUY, right=OptionRight.PUT, ratio=1),),
    directional_view=Direction.BEARISH,
    strike_relationship=StrikeRelationship.NONE,
    same_expiration=True,
    single_position=True,
    summary=(
        "One long put. Profits when the underlying falls far enough, soon enough, to "
        "outrun the premium paid. Loses the premium if it does not."
    ),
)
