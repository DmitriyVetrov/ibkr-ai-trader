"""Human-readable rendering of a cleanup plan and run.

Presentation only: no decision is made here and nothing is recomputed. Two
things it is careful about, because both are ways a report can mislead an
operator about an account:

* a **dash rather than a zero** wherever a figure is genuinely unknown. "The
  broker was not re-read" and "the broker holds none" are different facts, and
  a zero in the second column would read as the second;
* the **confirmation summary** is printed before anything is sent, not after.
  It is the last thing an operator sees before an order leaves the process, so
  it states the mode, the account, the exact contracts and the total quantity
  rather than a count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trading_system.cleanup.models import (
    CleanupOutcome,
    CleanupOutcomeStatus,
    OrphanCleanupRequest,
    OrphanCleanupRun,
)
from trading_system.domain.enums import TradingMode

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Annotation-only. Importing the service at runtime would put the one
    # module that can obtain a writable broker into the import graph of a
    # renderer, which has no business being anywhere near one.
    from trading_system.cleanup.service import CleanupPlan, CleanupRunOutcome

__all__ = [
    "render_confirmation_summary",
    "render_plan",
    "render_run",
]


def _or_dash(value: object) -> str:
    return "-" if value is None else str(value)


def render_plan(plan: CleanupPlan) -> str:
    """The whole review: candidates, gates, and the order for each target."""
    lines = [
        "ORPHAN POSITION CLEANUP — REVIEW",
        "=" * 64,
        "",
        f"Source reconciliation : {plan.selection.reconciliation_id}",
        f"Account               : {plan.selection.account_reference}",
        f"Orphan findings       : {plan.selection.orphan_count}",
        f"Targetable            : {len(plan.selection.targets)}",
        "",
    ]

    lines.append("TARGETS")
    lines.append("-" * 64)
    if not plan.selection.targets:
        lines.append("  (none)")
    for target in plan.selection.targets:
        lines.extend(
            [
                f"  {target.key}  {target.describe()}",
                f"    broker contract id : {target.contract_id}",
                f"    local symbol       : {_or_dash(target.local_symbol)}",
                f"    trading class      : {_or_dash(target.trading_class)}",
                f"    currency           : {_or_dash(target.currency)}",
                f"    multiplier         : {_or_dash(target.multiplier)}",
                f"    held (broker)      : {target.quantity}",
                f"    broker avg cost    : {_or_dash(target.average_cost)}  "
                f"(recorded, never attributed to the campaign)",
                f"    broker mkt price   : {_or_dash(target.market_price)}  (quoted terms)",
                f"    observed at        : {target.observed_at.isoformat()}",
                f"    finding            : {target.finding_id}",
            ]
        )

    if plan.selection.rejected:
        lines.extend(["", "NOT TARGETED", "-" * 64])
        for candidate in plan.selection.rejected:
            lines.append(f"  {candidate.key}: {candidate.reason}")

    lines.extend(["", "SAFETY GATES", "-" * 64])
    for verdict in plan.run_gates:
        lines.append(f"  {verdict.render()}")
    for key, verdicts in sorted(plan.target_gates.items()):
        for verdict in verdicts:
            lines.append(f"  {key}  {verdict.render()}")

    if plan.submissions:
        lines.extend(["", "PROPOSED ORDERS", "-" * 64])
        for submission in plan.submissions:
            record = submission.record
            intent = submission.intent
            if record is None or intent is None:
                lines.append(
                    f"  {submission.target_key}: nothing built — {submission.detail or 'no detail'}"
                )
                continue
            leg = intent.legs[0]
            lines.extend(
                [
                    f"  {submission.target_key}  {leg.action.value} {intent.quantity} x "
                    f"{leg.underlying} {leg.expiration} {leg.strike} {leg.right.value}",
                    f"    order              : {intent.order_type.value} "
                    f"{intent.time_in_force.value} @ {intent.limit_price}",
                    f"    reference (broker) : {_or_dash(record.reference_quote)}",
                    f"    contract id        : {leg.broker_contract_id}",
                    f"    intent             : {intent.intent.value}",
                    "    allocation / card / risk decision / strategy : none — "
                    "this system did not open this holding",
                ]
            )

    if plan.detail:
        lines.extend(["", plan.detail])
    return "\n".join(lines)


def render_confirmation_summary(
    request: OrphanCleanupRequest, *, account_reference: str, mode: TradingMode
) -> str:
    """The block printed immediately before any order leaves the process."""
    total = sum(int(target.quantity) for target in request.targets)
    lines = [
        "",
        "!" * 64,
        "ABOUT TO SUBMIT REAL PAPER ORDERS",
        "!" * 64,
        f"  MODE            : {mode.value}",
        f"  ACCOUNT         : {account_reference}",
        f"  TARGET COUNT    : {len(request.targets)}",
        "  TARGET POSITIONS:",
    ]
    for target in request.targets:
        lines.append(
            f"      {target.key}  {target.describe()}  "
            f"SELL {int(target.quantity)} @ limit derived from {target.market_price}"
        )
    lines.extend(
        [
            "  ACTION          : CLOSE (SELL to close a long holding)",
            f"  TOTAL QUANTITY  : {total} contract(s)",
            "  EXECUTION PATH  : ExecutionService.submit_cleanup -> ExecutionEngine -> broker",
            "  LIVE            : BLOCKED (PAPER only, both live guards off, refused in four "
            "independent places)",
            "  CONFIRMATION    : GIVEN (--confirm)",
            f"  REQUEST         : {request.cleanup_request_id}",
            f"  RECONCILIATION  : {request.source_reconciliation_id}",
            "!" * 64,
            "",
        ]
    )
    return "\n".join(lines)


def render_run(outcome: CleanupRunOutcome) -> str:
    """What happened, per target, with broker reality kept apart from claims."""
    run: OrphanCleanupRun = outcome.run
    lines = [
        "ORPHAN POSITION CLEANUP — RESULT",
        "=" * 64,
        "",
        f"Run                   : {run.run_id}",
        f"Request               : {run.cleanup_request_id}",
        f"Status                : {run.status.value}",
        f"Mode                  : {run.trading_mode.value}{'  (DRY RUN)' if run.dry_run else ''}",
        f"Account               : {run.account_reference}",
        f"Broker                : {run.broker}",
        f"Source reconciliation : {run.source_reconciliation_id}",
        f"Result reconciliation : {_or_dash(run.result_reconciliation_id)}",
        f"Trace                 : {_or_dash(run.trace_id)}",
        "",
        f"Orders submitted (read off the broker) : {run.orders_submitted}",
        f"Corrective orders                      : {run.corrective_orders}",
        f"Targets closed                         : {run.closed} of {len(run.outcomes)}",
        f"Uncertain submissions                  : {run.uncertain}",
        "",
        "PER TARGET",
        "-" * 64,
    ]
    for item in run.outcomes:
        lines.extend(_render_outcome(item))

    if run.uncertain:
        lines.extend(
            [
                "",
                "UNCERTAIN SUBMISSION(S). An order may be live at the broker right now.",
                "Do NOT re-run this command. Resolve by observation:",
                "  execution explain --execution-id <ID> --resolve",
            ]
        )
    if run.detail:
        lines.extend(["", run.detail])
    return "\n".join(lines)


def _render_outcome(item: CleanupOutcome) -> list[str]:
    lines = [
        f"  {item.key}  {item.describe}",
        f"    status             : {item.status.value}",
        f"    held before        : {item.observed_quantity_before}",
        f"    requested          : {item.requested_quantity}",
        f"    filled (broker)    : {item.filled_quantity}",
        # A dash, never a zero. "Not re-read" and "holds none" are different.
        f"    held after (broker): {_or_dash(item.observed_quantity_after)}",
        f"    execution          : {_or_dash(item.execution_id)}",
        f"    broker order       : {_or_dash(item.broker_order_id)}",
        f"    execution state    : {item.execution_state.value if item.execution_state else '-'}",
        f"    limit sent         : {_or_dash(item.limit_price)}",
        f"    reference (broker) : {_or_dash(item.reference_quote)}",
        f"    orders submitted   : {item.orders_submitted}",
    ]
    if item.reason_codes:
        lines.append(f"    reasons            : {', '.join(c.value for c in item.reason_codes)}")
    for failure in item.gate_failures:
        lines.append(f"    gate               : {failure}")
    if item.detail:
        lines.append(f"    detail             : {item.detail}")
    if item.status is CleanupOutcomeStatus.PARTIALLY_CLOSED:
        lines.append(
            "    NOTE               : partially filled. The remainder is reported and "
            "NOTHING further was sent"
        )
    return lines
