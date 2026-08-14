"""What the profit-and-loss ledger tells the campaign, and nothing more.

The narrow seam between Milestone 11's money and Milestone 7's risk engine.
Two facts cross it, and only two:

* which opportunities' capital has **settled** — so a closed position stops
  consuming the campaign's envelope, which is what makes the release real
  rather than cosmetic;
* the day's **realised result and its reliability** — so the daily loss limit
  is evaluated against a measured number, or explicitly against an absence.

The seam is deliberately this thin. ``allocation/`` may not import a broker,
a provider or a data repository — a boundary test walks its whole import graph
— and this module reaches none of them: the reservation ledger and the
profit-and-loss store are plain filesystem stores. What crosses is a frozen
value object, not a service.

Nothing here decides anything. It reports two facts; the risk engine decides
what they mean, and it decides that the same way it always has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from trading_system.domain.enums import DailyPnLStatus, ReservationState

__all__ = ["CampaignPnLState", "read_campaign_state"]


@dataclass(frozen=True, slots=True)
class CampaignPnLState:
    """What the ledgers say about a campaign's settled capital and today's result.

    ``daily_pnl_status`` and ``realized_pnl_today`` travel together and must be
    read together. ``TRACKED`` with a figure is a measurement; ``UNKNOWN`` with
    no figure is an absence of knowledge about a day on which money moved; and
    ``NOT_TRACKED`` means no ledger was consulted at all. Reading the figure
    without the status is how "we could not tell" becomes "no losses today".
    """

    #: Opportunities whose capital has returned to the campaign. Their
    #: authorisations are replayed out of the campaign's committed total.
    settled_opportunity_ids: frozenset[str] = frozenset()
    realized_pnl_today: Decimal | None = None
    daily_pnl_status: DailyPnLStatus = DailyPnLStatus.NOT_TRACKED
    session_date: date | None = None
    #: Positions that closed today and produced no usable figure. Named so an
    #: operator asking "why is my daily figure unknown" gets an answer.
    unavailable_position_ids: tuple[str, ...] = field(default=())

    @property
    def tracked(self) -> bool:
        return self.daily_pnl_status is DailyPnLStatus.TRACKED

    @property
    def unknown(self) -> bool:
        return self.daily_pnl_status is DailyPnLStatus.UNKNOWN

    @classmethod
    def untracked(cls) -> CampaignPnLState:
        """The state of a deployment with no profit-and-loss ledger at all."""
        return cls()


def read_campaign_state(
    data_root: Path | str,
    *,
    campaign_id: str,
    as_of: datetime,
    day_boundary_timezone: str = "America/New_York",
    enabled: bool = True,
) -> CampaignPnLState:
    """Read the two facts the campaign needs, from the stores that hold them.

    Returns :meth:`CampaignPnLState.untracked` — never a zeroed figure — when
    profit-and-loss tracking is switched off or nothing has been recorded. That
    distinction is the whole reason this function returns a status alongside a
    number: an untracked day and a break-even day are different facts, and only
    one of them is evidence that the limit is satisfied.
    """
    if not enabled:
        return CampaignPnLState.untracked()

    root = Path(data_root)
    settled = _settled_opportunities(root, campaign_id=campaign_id)
    realized, status, session, unavailable = _today(
        root, campaign_id=campaign_id, as_of=as_of, timezone=day_boundary_timezone
    )
    return CampaignPnLState(
        settled_opportunity_ids=settled,
        realized_pnl_today=realized,
        daily_pnl_status=status,
        session_date=session,
        unavailable_position_ids=unavailable,
    )


def _settled_opportunities(root: Path, *, campaign_id: str) -> frozenset[str]:
    """Opportunities whose committed capital has come back.

    ``SETTLED`` only. A ``RELEASED`` reservation freed capital that was never
    spent and the campaign ledger already accounts for that through the
    allocation replay; a ``CONSUMED`` one is sitting in a live position. Only a
    settlement says the position is confirmed gone.
    """
    from trading_system.reservations.store import FilesystemReservationRepository

    repository = FilesystemReservationRepository(root / "reservations")
    return frozenset(
        reservation.opportunity_id
        for reservation in repository.all_current()
        if reservation.campaign_id == campaign_id and reservation.state is ReservationState.SETTLED
    )


def _today(
    root: Path, *, campaign_id: str, as_of: datetime, timezone: str
) -> tuple[Decimal | None, DailyPnLStatus, date | None, tuple[str, ...]]:
    """The day's realised figure, and how far it can be relied on."""
    from trading_system.pnl.calculator import session_date_of
    from trading_system.pnl.store import FilesystemPnLRepository

    session = session_date_of(as_of, timezone)
    repository = FilesystemPnLRepository(root / "pnl")
    daily = repository.daily(session)
    if daily is None or daily.campaign_id != campaign_id:
        # No position closed today, or no roll-up has been computed. Neither is
        # a loss and neither is a *measured* zero, so the honest answer is that
        # nothing is tracked for this day rather than that the day was flat.
        return None, DailyPnLStatus.NOT_TRACKED, session, ()
    return (
        daily.realized_pnl,
        daily.status,
        daily.session_date,
        tuple(daily.unavailable_position_ids),
    )
