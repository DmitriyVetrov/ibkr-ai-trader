"""Human-readable rendering of realised results, days and settlements.

Rendered on demand from the immutable records rather than stored alongside
them: a report is a view, and a stored view is a second copy of the truth that
can drift from the first.

Three rules govern every line here, two of them carried forward from earlier
milestones because the failure they prevent is the same one:

* **An unavailable figure prints as** ``-`` **, never as** ``0``. "This trade's
  result could not be computed" and "this trade broke even" are different
  claims, and they must not look the same on a screen either.
* **Quoted terms and money are labelled apart.** ``6.05`` and ``605.00``
  describe the same option and differ by a factor of a hundred, so every line
  says which one it is showing.
* **A refusal prints its reason.** A blocked settlement without the missing
  evidence next to it leaves an operator with the one question this whole
  package exists to answer — *why has my available capital not gone back up?*
"""

from __future__ import annotations

from decimal import Decimal

from trading_system.domain.enums import DailyPnLStatus, PnLStatus, SettlementStatus
from trading_system.pnl.models import (
    DailyPnL,
    PnLRunResult,
    RealizedPnL,
    ReservationSettlement,
)

__all__ = [
    "render_daily",
    "render_realized",
    "render_run",
    "render_settlement",
    "render_summary",
]


def _or_dash(value: object | None) -> str:
    """An unavailable value is a dash. Never a zero."""
    return "-" if value is None else str(value)


def _signed(value: Decimal | None) -> str:
    """A result, with its sign made explicit. A dash when there is no figure."""
    if value is None:
        return "-"
    return f"+{value}" if value > 0 else str(value)


def render_realized(record: RealizedPnL) -> str:
    """One structure's realised result, in full."""
    lines = [
        f"REALISED PROFIT AND LOSS  {record.status.value}",
        "",
        f"Position   : {record.position_id}",
        f"Underlying : {record.underlying}  ({record.strategy.value})",
        f"Result id  : {record.pnl_id}",
        f"Session    : {_or_dash(record.session_date)}",
        f"Opened     : {_or_dash(record.opened_at)}",
        f"Closed     : {_or_dash(record.closed_at)}",
        "",
        f"Reasons    : {', '.join(code.value for code in record.reason_codes)}",
        f"Units      : matched {record.matched_quantity} of {record.opened_quantity} opened",
        "",
        "ECONOMICS (money, multiplier included)",
        f"  Entry cost      : {_or_dash(record.entry_cost)} {record.currency or ''}".rstrip(),
        f"  Exit proceeds   : {_or_dash(record.exit_proceeds)} {record.currency or ''}".rstrip(),
        f"  Realised gross  : {_signed(record.realized_gross_pnl)}",
        f"  Commissions     : {_or_dash(record.total_commission)} "
        f"({record.commission_status.value})",
        f"  Realised net    : {_signed(record.realized_net_pnl)}",
        f"  Return          : {_or_dash(record.return_pct)}%",
        "",
        "LEGS",
    ]
    for leg in record.legs:
        descriptor = " ".join(
            part
            for part in (
                leg.right.value if leg.right else None,
                str(leg.strike) if leg.strike else None,
                leg.expiration.isoformat() if leg.expiration else None,
            )
            if part
        )
        lines.append(
            f"  [{leg.leg_index}] {descriptor or leg.key}: "
            f"matched {leg.matched_quantity}, "
            f"entry {_or_dash(leg.average_entry_quote)} -> exit "
            f"{_or_dash(leg.average_exit_quote)} (quoted terms), "
            f"result {_signed(leg.gross_pnl)}"
        )
    if not record.legs:
        lines.append("  (none — no confirmed fill was matched)")

    lines.extend(
        [
            "",
            "PROVENANCE",
            f"  Entry execution : {_or_dash(record.entry_execution_id)}",
            f"  Exit executions : {', '.join(record.exit_execution_ids) or '-'}",
            f"  Broker fills    : {len(record.source_fill_ids)} confirmed execution report(s)",
            f"  Allocation      : {_or_dash(record.allocation_id)}",
            "",
            record.detail or "",
        ]
    )
    if record.status is PnLStatus.NOT_AVAILABLE:
        lines.append(
            "\nNo figure is reported, deliberately. A result assembled from an estimate would "
            "be used by the daily loss limit as though it were measured."
        )
    return "\n".join(line for line in lines if line is not None)


def render_daily(record: DailyPnL) -> str:
    """One exchange-local trading day, and how reliable its figure is."""
    lines = [
        f"DAILY REALISED PROFIT AND LOSS  {record.status.value}",
        "",
        f"Session    : {record.session_date.isoformat()} ({record.timezone})",
        f"Campaign   : {record.campaign_id}",
        f"Record     : {record.daily_pnl_id}",
        "",
        f"Realised   : {_signed(record.realized_pnl)} {record.currency}",
        f"  gross    : {_signed(record.realized_gross_pnl)}",
        f"  costs    : {_or_dash(record.total_commission)} ({record.commission_status.value})",
        f"  loss     : {_or_dash(record.realized_loss)}  "
        f"(the figure the daily loss limit is compared against)",
        "",
        f"Closed     : {record.positions_closed} position(s)",
        f"  with a result    : {record.positions_with_result}",
        f"  without a result : {record.positions_without_result}",
    ]
    if record.unavailable_position_ids:
        lines.append(f"  unavailable      : {', '.join(record.unavailable_position_ids)}")
    if record.status is DailyPnLStatus.UNKNOWN:
        lines.extend(
            [
                "",
                "The day's total is UNKNOWN, which is not zero loss. At least one position "
                "closed today and produced no usable figure; the risk engine treats this as "
                "an absence of knowledge rather than as an absence of losses.",
            ]
        )
    if record.detail:
        lines.extend(["", record.detail])
    return "\n".join(lines)


def render_settlement(settlement: ReservationSettlement) -> str:
    """One settlement, or one refusal to settle."""
    lines = [
        f"RESERVATION SETTLEMENT  {settlement.status.value}",
        "",
        f"Settlement : {settlement.settlement_id}",
        f"Reservation: {settlement.reservation_id}",
        f"Position   : {settlement.position_id}",
        f"Allocation : {settlement.allocation_id}",
        "",
        f"Committed before : {settlement.committed_before} {settlement.currency}",
        f"Returned         : {settlement.settled_amount} {settlement.currency}",
        f"Committed after  : {settlement.committed_after} {settlement.currency}",
        f"Realised result  : {_signed(settlement.realized_pnl)}",
        f"Matched units    : {settlement.matched_quantity} of "
        f"{settlement.authorized_quantity} authorised",
        f"Result record    : {_or_dash(settlement.pnl_id)}",
        f"Reconciliation   : {_or_dash(settlement.reconciliation_id)}",
    ]
    if settlement.status is SettlementStatus.BLOCKED:
        lines.extend(
            [
                "",
                f"BLOCKED: {settlement.block_reason.value if settlement.block_reason else '-'}",
                "No capital moved. Capital returns to the campaign on broker-confirmed "
                "closure and on nothing weaker.",
            ]
        )
    if settlement.detail:
        lines.extend(["", settlement.detail])
    return "\n".join(lines)


def render_run(result: PnLRunResult) -> str:
    """One settlement pass, and what it moved."""
    lines = [
        f"PROFIT AND LOSS RUN  {result.run_id}",
        "",
        f"Campaign   : {result.campaign_id}",
        f"As of      : {result.as_of.isoformat()}",
        f"Dry run    : "
        f"{'yes — nothing was written and no capital moved' if result.dry_run else 'no'}",
        "",
        f"Positions examined  : {result.positions_examined}",
        f"Results computed    : {result.results_computed}",
        f"Results unavailable : {result.results_unavailable}",
        f"Settlements applied : {result.settlements_applied}",
        f"Settlements blocked : {result.settlements_blocked}",
        f"Capital returned    : {result.capital_returned} {result.currency}",
        "",
        f"Orders submitted    : {result.orders_submitted}  "
        f"(structurally zero: this package holds no broker)",
    ]
    if result.detail:
        lines.extend(["", result.detail])
    return "\n".join(lines)


def render_summary(records: list[RealizedPnL]) -> str:
    """A one-line-per-trade table. Unavailable results are shown, never hidden."""
    if not records:
        return "No realised results recorded."
    header = (
        f"{'POSITION':<28} {'SYMBOL':<8} {'STRATEGY':<16} {'STATUS':<14} "
        f"{'RESULT':>14}  {'SESSION':<12}"
    )
    lines = [header, "-" * len(header)]
    for record in records:
        lines.append(
            f"{record.position_id[:28]:<28} {record.underlying:<8} "
            f"{record.strategy.value:<16} {record.status.value:<14} "
            f"{_signed(record.best_available_pnl):>14}  "
            f"{record.session_date.isoformat() if record.session_date else '-':<12}"
        )
    return "\n".join(lines)
