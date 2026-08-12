"""Long straddle — a bought call and a bought put on the same strike.

The shape for hypotheses ``A`` and ``D``: a large move is expected and its
direction is not. Both legs share one expiration *and* one strike, which is
what makes it a straddle rather than two unrelated positions, and the contract
selector enforces both rather than trusting the configuration.

Managed as a single position (specification section 17A): the trailing stop
applies to the combined structure, never to one leg. ``single_position`` says
so here so a later milestone cannot decide otherwise for a strategy whose
specification does not permit it.
"""

from __future__ import annotations

from trading_system.domain.enums import (
    Direction,
    LegAction,
    MaxLossBasis,
    OptionRight,
    StrategyType,
)
from trading_system.strategies.base import LegTemplate, StrategyStructure, StrikeRelationship

__all__ = ["STRUCTURE"]

STRUCTURE = StrategyStructure(
    strategy_type=StrategyType.LONG_STRADDLE,
    legs=(
        LegTemplate(action=LegAction.BUY, right=OptionRight.CALL, ratio=1),
        LegTemplate(action=LegAction.BUY, right=OptionRight.PUT, ratio=1),
    ),
    directional_view=Direction.UNCERTAIN,
    # Bought outright, so the most that can be lost is what was paid:
    # two premiums, both paid. Nothing beyond the debit is at risk.
    max_loss_basis=MaxLossBasis.NET_DEBIT_PAID,
    strike_relationship=StrikeRelationship.SAME,
    same_expiration=True,
    single_position=True,
    summary=(
        "One long call and one long put, same underlying, same expiration, same strike. "
        "Profits from a large move in either direction; loses when the move is too small "
        "to cover two premiums."
    ),
)
