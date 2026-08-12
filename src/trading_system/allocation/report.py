"""Human-readable rendering of risk evaluations and allocations.

Rendered on demand from the immutable records rather than stored alongside
them: a report is a view, and a stored view is a second copy of the truth that
can drift from the first.

Every line here is generated from structured fields — a limit name, an actual
value, a limit value — and never from prose someone wrote at decision time.
That is the whole point of the check list: an explanation that can be
regenerated is an explanation that cannot quietly disagree with the decision it
describes.

Both renderings lead with the arithmetic, because that is what an operator
actually needs. "REJECTED: MAX_RISK_EXCEEDED" answers nothing on its own;
"candidate maximum loss EUR 1,200, remaining campaign risk EUR 700" answers it
completely.
"""

from __future__ import annotations

from trading_system.allocation.models import AllocationRunResult, CampaignAllocation
from trading_system.domain.enums import AllocationOutcome, RiskCheckOutcome
from trading_system.risk.models import RiskEvaluation

__all__ = [
    "render_allocation",
    "render_allocation_run",
    "render_evaluation",
    "render_run_summary",
]


def render_run_summary(result: AllocationRunResult) -> str:
    """One-paragraph header: what happened, against what budget, and how much."""
    counts = result.counts
    lines = [
        f"Allocation Run : {result.run_id}",
        f"Campaign       : {result.campaign_id}",
        f"As Of          : {result.as_of.isoformat()}",
        f"Generated      : {result.generated_at.isoformat()}",
        f"Status         : {result.status.value}",
        f"Policy         : {result.policy.value} (v{result.policy_version})",
        f"Mode           : {result.trading_mode.value}",
        f"Contract run   : {result.contract_run_id or 'none'}",
        f"Account        : {result.account_snapshot_id or 'none'}",
        "",
        f"Campaign budget      : {result.budget}",
        f"Reserve (never spent): {result.reserve}",
        f"Already allocated    : {result.allocated_before}",
        f"Allocated this run   : {result.allocated_this_run}",
        f"Available after      : {result.available_after}",
        f"Risk authorised      : {result.risk_authorized_this_run}",
        "",
        f"Candidates considered : {counts.candidates_considered}",
        f"Approved              : {counts.approved}",
        f"Rejected              : {counts.rejected}",
        f"No trade              : {counts.no_trade}",
        f"Already allocated     : {counts.already_allocated}",
        "",
        "Orders submitted      : 0  (allocation authorises capital; it places no orders)",
    ]
    if result.dry_run:
        lines.extend(
            [
                "",
                "DRY RUN — this result is diagnostic. It reserves no capital and is not an "
                "authorisation.",
            ]
        )
    if result.status_detail:
        lines.extend(["", f"Detail: {result.status_detail}"])
    return "\n".join(lines)


def render_allocation_run(result: AllocationRunResult, *, verbose: bool = False) -> str:
    """The whole run: the summary, then one line or one block per candidate."""
    blocks = [render_run_summary(result)]
    if not result.allocations:
        return "\n".join(blocks)

    blocks.append("")
    if verbose:
        blocks.extend(render_allocation(a) for a in result.allocations)
        return "\n\n".join(blocks)

    blocks.append("Decisions:")
    blocks.extend(f"  {_allocation_line(a)}" for a in result.allocations)
    return "\n".join(blocks)


def render_allocation(allocation: CampaignAllocation) -> str:
    """One decision, with the arithmetic that produced it."""
    lines = [
        f"{allocation.symbol}  [{allocation.outcome.value}]  rank {allocation.rank}",
        f"  Strategy   : {allocation.strategy.value} ({allocation.direction.value})",
        f"  Score      : {allocation.opportunity_score.total:.2f}",
        f"  Opportunity: {allocation.opportunity_id}",
    ]

    if allocation.approved and allocation.calculation is not None:
        calculation = allocation.calculation
        lines.extend(
            [
                "",
                f"  Quantity        : {allocation.quantity} contract(s)",
                f"  Unit cost       : {allocation.unit_cost} ({_source(allocation)})",
                f"  Capital         : {allocation.capital_committed}",
                f"  Unit max loss   : {allocation.unit_max_loss} "
                f"({calculation.max_loss_basis.value})",
                f"  Total max loss  : {allocation.total_max_loss}",
                f"  Bound by        : {calculation.binding_constraint.value}",
                "",
                "  Units each ceiling permitted:",
                f"    campaign budget        : {calculation.units_by_budget}",
                f"    risk budget            : {calculation.units_by_risk}",
                f"    per-trade allocation   : {calculation.units_by_trade_cap}",
                f"    underlying concentration: {calculation.units_by_underlying_concentration}",
                f"    strategy concentration : {calculation.units_by_strategy_concentration}",
                f"    directional exposure   : {calculation.units_by_directional_exposure}",
                f"    contract count         : {calculation.units_by_contract_cap}",
                f"    broker available funds : {_or_dash(calculation.units_by_buying_power)}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"  Authorised: nothing ({allocation.outcome.value})",
                f"  Reasons   : {', '.join(c.value for c in allocation.reason_codes) or '-'}",
            ]
        )

    if allocation.detail:
        lines.extend(["", f"  {allocation.detail}"])

    failed = allocation.risk_evaluation.failed_checks
    if failed:
        lines.append("")
        lines.append("  Failed checks:")
        lines.extend(f"    {check.describe()}" for check in failed)

    unevaluated = allocation.risk_evaluation.unevaluated_checks
    if unevaluated:
        lines.append("")
        lines.append("  Not evaluated:")
        lines.extend(f"    {check.describe()}" for check in unevaluated)

    exposure = allocation.exposure_after
    lines.extend(
        [
            "",
            "  Campaign after this decision:",
            f"    allocated : {exposure.resulting_campaign_exposure}",
            f"    open risk : {exposure.resulting_campaign_risk}",
            f"    positions : {exposure.resulting_position_count}",
            f"    {allocation.symbol} exposure : {exposure.resulting_underlying_exposure}",
        ]
    )
    if allocation.dry_run:
        lines.extend(["", "  DRY RUN — diagnostic only; this reserves no capital."])
    return "\n".join(lines)


def render_evaluation(evaluation: RiskEvaluation) -> str:
    """One risk verdict in full, every check listed.

    Used by ``risk evaluate``, which answers *would this be permitted?* without
    sizing anything and without persisting anything.
    """
    lines = [
        f"{evaluation.symbol}  [{evaluation.outcome.value}]",
        f"  Opportunity : {evaluation.opportunity_id}",
        f"  As of       : {evaluation.as_of.isoformat()}",
        f"  Mode        : {evaluation.trading_mode.value}",
        f"  Reasons     : {', '.join(c.value for c in evaluation.reason_codes)}",
        f"  Unit cost   : {_or_dash(evaluation.unit_cost)}",
        f"  Unit max loss: {_or_dash(evaluation.unit_max_loss)} "
        f"({evaluation.max_loss_basis.value if evaluation.max_loss_basis else '-'})",
        "",
        "  Checks:",
    ]
    for check in evaluation.checks:
        marker = {
            RiskCheckOutcome.PASS: "  ok  ",
            RiskCheckOutcome.FAIL: " FAIL ",
            RiskCheckOutcome.NOT_EVALUATED: "  --  ",
        }[check.outcome]
        lines.append(f"   [{marker}] {check.describe()}")
    return "\n".join(lines)


def _allocation_line(allocation: CampaignAllocation) -> str:
    if allocation.outcome is AllocationOutcome.APPROVED:
        return (
            f"{allocation.symbol:<8} {allocation.outcome.value:<18} "
            f"{allocation.quantity} x {allocation.unit_cost} = "
            f"{allocation.capital_committed}  (max loss {allocation.total_max_loss})"
        )
    reasons = ", ".join(c.value for c in allocation.reason_codes) or "-"
    return f"{allocation.symbol:<8} {allocation.outcome.value:<18} {reasons}"


def _source(allocation: CampaignAllocation) -> str:
    return allocation.price_source.value if allocation.price_source else "unknown source"


def _or_dash(value: object | None) -> str:
    return "-" if value is None else str(value)
