"""Long strangle — a bought out-of-the-money call and put.

The same claim as a straddle — a large move, direction unknown — expressed more
cheaply and requiring a bigger move to pay. One expiration, two *different*
strikes, the call above the reference price and the put below it.

``CALL_ABOVE_PUT`` is not decoration. A chain coarse enough that both offsets
round to the same strike produces a straddle, and returning that under the
strangle's name would misdescribe the position, its cost and its breakevens to
every later stage. The selector rejects it instead.
"""

from __future__ import annotations

from trading_system.domain.enums import Direction, LegAction, OptionRight, StrategyType
from trading_system.strategies.base import LegTemplate, StrategyStructure, StrikeRelationship

__all__ = ["STRUCTURE"]

STRUCTURE = StrategyStructure(
    strategy_type=StrategyType.LONG_STRANGLE,
    legs=(
        LegTemplate(action=LegAction.BUY, right=OptionRight.CALL, ratio=1),
        LegTemplate(action=LegAction.BUY, right=OptionRight.PUT, ratio=1),
    ),
    directional_view=Direction.UNCERTAIN,
    strike_relationship=StrikeRelationship.CALL_ABOVE_PUT,
    same_expiration=True,
    single_position=True,
    summary=(
        "One long out-of-the-money call and one long out-of-the-money put, same "
        "underlying, same expiration, strikes either side of the reference price. "
        "Cheaper than a straddle and needs a larger move to pay."
    ),
)
