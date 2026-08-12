"""Deterministic exposure arithmetic.

Answers one question, in decimal, with no opinions in it: *what does the
campaign hold now, and what would it hold if this candidate were funded?*

Both sides are computed because both are asked. "Would this breach the limit"
needs the resulting figure; "how close are we" needs the current one; and an
operator reading a rejection wants to see the two side by side rather than
subtract them mentally.

What this module deliberately does **not** do is model correlation. Two
positions on the same underlying are two positions on the same underlying, and
a system that scored them as 1.7 positions on the strength of a correlation
matrix would be claiming a precision nobody has measured here. Concentration
rules stay explicit and countable: exposure per underlying, per strategy, per
direction, and a position count. That is enough to prevent the failure modes
this milestone is responsible for, and it is auditable by hand.
"""

from __future__ import annotations

from decimal import Decimal

from trading_system.domain.enums import Direction
from trading_system.risk.models import (
    AllocationCandidate,
    CampaignSnapshot,
    PortfolioExposure,
)

__all__ = ["directional_view_of", "exposure_for", "would_add"]


def exposure_for(
    campaign: CampaignSnapshot,
    candidate: AllocationCandidate | None = None,
    *,
    capital: Decimal = Decimal("0"),
    risk: Decimal = Decimal("0"),
) -> PortfolioExposure:
    """The campaign's exposure, optionally including a prospective position.

    ``capital`` and ``risk`` are what the candidate would add. Passing zero —
    the default — describes the campaign exactly as it stands, which is what a
    run reports before it has funded anything and what a rejection quotes as
    the "current" side of its comparison.
    """
    underlying = campaign.committed_to(candidate.symbol) if candidate else Decimal("0")
    strategy = campaign.committed_to_strategy(candidate.strategy) if candidate else Decimal("0")
    direction = (
        campaign.committed_to_direction(candidate.risk_profile.directional_view)
        if candidate
        else Decimal("0")
    )
    positions_in_underlying = campaign.positions_in(candidate.symbol) if candidate else 0

    adds_a_position = 1 if capital > 0 else 0

    return PortfolioExposure(
        campaign_budget=campaign.budget,
        campaign_reserve=campaign.reserve,
        campaign_allocated=campaign.allocated,
        campaign_available=campaign.available,
        campaign_open_risk=campaign.open_risk,
        position_count=campaign.position_count,
        underlying_exposure=underlying,
        strategy_exposure=strategy,
        directional_exposure=direction,
        positions_in_underlying=positions_in_underlying,
        resulting_campaign_exposure=campaign.allocated + capital,
        resulting_campaign_risk=campaign.open_risk + risk,
        resulting_underlying_exposure=underlying + capital,
        resulting_strategy_exposure=strategy + capital,
        resulting_directional_exposure=direction + capital,
        resulting_position_count=campaign.position_count + adds_a_position,
    )


def would_add(
    campaign: CampaignSnapshot,
    candidate: AllocationCandidate,
    *,
    quantity: int,
    unit_cost: Decimal,
    unit_max_loss: Decimal,
) -> PortfolioExposure:
    """Exposure if exactly ``quantity`` units of ``candidate`` were funded.

    A quantity of zero produces the campaign as it stands — deliberately, so a
    ``NO_TRADE`` outcome still records a truthful exposure picture rather than
    one describing a position nobody took.
    """
    if quantity < 0:
        raise ValueError(f"quantity must not be negative, got {quantity}")
    units = Decimal(quantity)
    return exposure_for(
        campaign,
        candidate,
        capital=unit_cost * units,
        risk=unit_max_loss * units,
    )


def directional_view_of(candidate: AllocationCandidate) -> Direction:
    """What the candidate's payoff expresses, from its strategy's structure.

    Read from the strategy rather than from the research report: a report says
    what the market is expected to do, a structure says what the position
    profits from, and only the second is a fact about the exposure being taken
    on.
    """
    return candidate.risk_profile.directional_view
