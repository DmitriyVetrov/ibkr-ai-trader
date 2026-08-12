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

from datetime import datetime
from decimal import Decimal

from trading_system.allocation.models import AllocationRunResult
from trading_system.domain.enums import AllocationOutcome, BudgetSource
from trading_system.risk.models import CampaignPosition, CampaignSnapshot

__all__ = ["build_campaign_snapshot", "reservations_from"]


def reservations_from(
    runs: list[AllocationRunResult],
    *,
    campaign_id: str,
    as_of: datetime,
) -> list[CampaignPosition]:
    """Every still-held reservation this campaign authorised, as of ``as_of``.

    Point-in-time by construction: a run generated after ``as_of`` is invisible,
    so a historical reconstruction cannot see capital that had not been
    committed yet. Dry runs are excluded — a diagnostic result never reserves
    anything, which is what makes ``--dry-run`` safe to run repeatedly.

    Ordered by the instant of authorisation, then by opportunity id, so two
    reconstructions of the same history produce the same list rather than
    whichever order the filesystem returned.
    """
    reservations: dict[str, CampaignPosition] = {}
    ordered = sorted(runs, key=lambda run: (run.as_of, run.generated_at, run.run_id))

    for run in ordered:
        if run.dry_run or run.campaign_id != campaign_id or run.generated_at > as_of:
            continue
        for allocation in run.allocations:
            if allocation.outcome is not AllocationOutcome.APPROVED:
                continue
            if allocation.as_of > as_of:
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
) -> CampaignSnapshot:
    """The campaign as it stands at ``as_of``, replayed from the ledger.

    ``realized_pnl_today`` stays ``None`` until a milestone exists that tracks
    it. That is not the same as zero and is not treated as zero anywhere: the
    risk engine records the daily-loss limit as ``NOT_EVALUATED``, and
    configuration decides whether an unevaluated limit blocks a trade.
    """
    return CampaignSnapshot(
        campaign_id=campaign_id,
        as_of=as_of,
        currency=currency,
        budget=budget,
        reserve=reserve,
        budget_source=budget_source.value,
        open_positions=reservations_from(runs, campaign_id=campaign_id, as_of=as_of),
        realized_pnl_today=realized_pnl_today,
    )
