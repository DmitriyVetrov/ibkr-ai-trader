"""Campaign accounting: what this campaign has already committed.

The campaign is the system's own capital envelope, and it is **independent of
the broker account balance**. The IBKR paper account may hold a million euro;
that is not permission to spend a million euro. The campaign may spend its
budget less its reserve, and where the account holds less than that, the
account wins — the most restrictive relevant limit always does.

Campaign state is reconstructed from the allocation ledger rather than stored
as a running total. A running balance is a second copy of the truth: it drifts,
and when it does there is no way to tell which copy is wrong. Replaying the
append-only history has one answer by construction, and the replay is
point-in-time filtered, so reconstructing the campaign "as of last Tuesday"
gives what was committed by last Tuesday rather than what is committed now.

One consequence is worth stating plainly, because it is a deliberate choice and
not an oversight: **an authorisation that was never executed still consumes
campaign budget here.** Milestone 7 ends at an authorisation and cannot know
whether an order ever filled. Treating an unexecuted authorisation as free
would let the same euro be authorised twice, which is the failure worth
preventing; releasing stale reservations belongs to the milestone that learns
what actually happened to them.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from decimal import Decimal

from trading_system.allocation.models import AllocationRunResult
from trading_system.domain.enums import AllocationOutcome, BudgetSource, DailyPnLStatus
from trading_system.risk.models import CampaignPosition, CampaignSnapshot

__all__ = ["build_campaign_snapshot", "reservations_from"]


def reservations_from(
    runs: list[AllocationRunResult],
    *,
    campaign_id: str,
    as_of: datetime,
    settled_opportunity_ids: Collection[str] | None = None,
) -> list[CampaignPosition]:
    """Every still-held reservation this campaign authorised, as of ``as_of``.

    Point-in-time by construction: a run generated after ``as_of`` is invisible,
    so a historical reconstruction cannot see capital that had not been
    committed yet. Dry runs are excluded — a diagnostic result never reserves
    anything, which is what makes ``--dry-run`` safe to run repeatedly.

    ``settled_opportunity_ids`` is Milestone 11's contribution and it is what
    makes a capital release *real* rather than cosmetic. Milestone 7 could not
    know whether an order ever filled, so it treated every authorisation as
    permanently spent — the conservative reading, and the right one at the
    time. Milestone 11 learns what became of the position: an opportunity whose
    reservation has **settled** is one the broker confirms is closed, whose
    realised result is recorded, and whose capital has come back. It stops
    consuming the envelope here, which is the only place that decides whether
    another trade may be funded.

    Nothing weaker than a settlement qualifies. An unexecuted authorisation,
    a working order and an ``UNKNOWN`` submission all still consume budget,
    exactly as they did before.

    Ordered by the instant of authorisation, then by opportunity id, so two
    reconstructions of the same history produce the same list rather than
    whichever order the filesystem returned.
    """
    reservations: dict[str, CampaignPosition] = {}
    ordered = sorted(runs, key=lambda run: (run.as_of, run.generated_at, run.run_id))
    settled = frozenset(settled_opportunity_ids or ())

    for run in ordered:
        if run.dry_run or run.campaign_id != campaign_id or run.generated_at > as_of:
            continue
        for allocation in run.allocations:
            if allocation.outcome is not AllocationOutcome.APPROVED:
                continue
            if allocation.as_of > as_of:
                continue
            if allocation.opportunity_id in settled:
                continue
            # First authorisation wins. A later run that re-derived the same
            # opportunity id recognised the existing reservation rather than
            # creating a second one, so a duplicate here would be a storage
            # fault — and keeping the earlier record is the conservative read.
            reservations.setdefault(
                allocation.opportunity_id,
                CampaignPosition(
                    opportunity_id=allocation.opportunity_id,
                    allocation_id=allocation.allocation_id,
                    symbol=allocation.symbol,
                    strategy=allocation.strategy,
                    direction=allocation.direction,
                    quantity=allocation.quantity,
                    capital_committed=allocation.capital_committed,
                    max_loss=allocation.total_max_loss,
                    authorized_at=allocation.as_of,
                    expiration=allocation.expiration,
                    contract_selection_id=allocation.contract_selection_id,
                    research_report_id=allocation.research_report_id,
                ),
            )

    return sorted(reservations.values(), key=lambda p: (p.authorized_at, p.opportunity_id))


def build_campaign_snapshot(
    runs: list[AllocationRunResult],
    *,
    campaign_id: str,
    currency: str,
    budget: Decimal,
    reserve: Decimal,
    as_of: datetime,
    budget_source: BudgetSource = BudgetSource.CONFIG,
    realized_pnl_today: Decimal | None = None,
    daily_pnl_status: DailyPnLStatus = DailyPnLStatus.NOT_TRACKED,
    unavailable_pnl_position_ids: Collection[str] | None = None,
    settled_opportunity_ids: Collection[str] | None = None,
) -> CampaignSnapshot:
    """The campaign as it stands at ``as_of``, replayed from the ledger.

    ``realized_pnl_today`` and ``daily_pnl_status`` travel together and must be
    supplied together. Milestone 11's ledger produces both; a caller that has
    no ledger passes neither, and the defaults say ``NOT_TRACKED`` with no
    figure — which the risk engine records as ``NOT_EVALUATED`` rather than as
    a satisfied limit. The one combination that cannot exist is a figure
    without a ``TRACKED`` status, and the snapshot model refuses it: a comfortable
    number next to "we could not measure today" is exactly how an unmeasured
    day would pass a loss limit.
    """
    return CampaignSnapshot(
        campaign_id=campaign_id,
        as_of=as_of,
        currency=currency,
        budget=budget,
        reserve=reserve,
        budget_source=budget_source.value,
        open_positions=reservations_from(
            runs,
            campaign_id=campaign_id,
            as_of=as_of,
            settled_opportunity_ids=settled_opportunity_ids,
        ),
        realized_pnl_today=realized_pnl_today,
        daily_pnl_status=daily_pnl_status,
        unavailable_pnl_position_ids=sorted(unavailable_pnl_position_ids or ()),
    )
