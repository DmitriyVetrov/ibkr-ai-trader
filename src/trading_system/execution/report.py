"""Human-readable rendering of executions and execution runs.

Rendered on demand from the immutable records rather than stored alongside
them: a report is a view, and a stored view is a second copy of the truth that
can drift from the first.

The wording here is load-bearing in a way it is not elsewhere in the system.
Every other stage's report describes something that has not happened yet, so an
over-confident phrasing is merely untidy. This one describes orders, and a line
that says "filled" where the broker only said "accepted" would tell an operator
they hold a position they do not. So:

* nothing says *filled* unless the broker reported a fill;
* a dry run says so on every line that could be mistaken for a submission;
* the mode is stated on every rendering, never inferred from context;
* an uncertain submission is described as uncertain, at length, because it is
  the state most likely to be misread as "it did not go through".
"""

from __future__ import annotations

from datetime import datetime

from trading_system.domain.enums import ExecutionState
from trading_system.execution.models import ExecutionRecord, ExecutionRunResult
from trading_system.execution.service import ExecutionPlan

__all__ = [
    "render_execution",
    "render_execution_run",
    "render_plan",
    "render_run_summary",
]


def render_run_summary(result: ExecutionRunResult) -> str:
    """One header: what this run did, in what mode, and how many orders it sent."""
    counts = result.counts
    banner = (
        "EXECUTION DRY RUN — nothing was submitted"
        if result.dry_run
        else f"EXECUTION {result.status.value}"
    )
    lines = [
        banner,
        "",
        f"Run             : {result.run_id}",
        f"Campaign        : {result.campaign_id}",
        f"As of           : {result.as_of.isoformat()}",
        f"Generated       : {result.generated_at.isoformat()}",
        f"Mode            : {result.trading_mode.value}",
        f"Broker          : {result.broker}",
        f"Allocation run  : {result.allocation_run_id or 'none'}",
        f"Policy version  : {result.policy_version}",
        "",
        f"Authorisations considered : {counts.considered}",
        f"Submitted                 : {counts.submitted}",
        f"Refused                   : {counts.refused}",
        f"Already submitted         : {counts.already_submitted}",
        f"Uncertain                 : {counts.uncertain}",
        "",
        # Read off the broker rather than counted here, so the number is
        # evidence rather than a claim about our own behaviour.
        f"Orders submitted (broker count) : {result.orders_submitted}",
    ]
    if result.dry_run:
        lines.append("Broker submission               : NOT PERFORMED")
    if result.status_detail:
        lines.extend(["", result.status_detail])
    if counts.uncertain:
        lines.extend(
            [
                "",
                "One or more submissions are UNCERTAIN. An order may be live at the broker.",
                "Do NOT resubmit. Resolve with: execution explain --execution-id <ID>",
            ]
        )
    return "\n".join(lines)


def render_execution(record: ExecutionRecord) -> str:
    """One execution, in full: what was sent, what came back, and what is true now."""
    lines = [
        f"Execution   : {record.execution_id}",
        f"Request     : {record.execution_request_id}",
        f"Allocation  : {record.allocation_id}",
        f"Card        : {record.purchase_card_id}",
        f"Underlying  : {record.underlying}",
        f"Strategy    : {record.strategy.value}",
        f"State       : {record.state.value}{'  (DRY RUN)' if record.dry_run else ''}",
        f"Mode        : {record.trading_mode.value}",
        f"Broker      : {record.broker}",
        "",
        f"Quantity            : {record.quantity}",
        f"Multiplier          : {record.multiplier or 'unknown'}",
        f"Order type          : {record.order_type.value} {record.time_in_force.value}",
        "",
        # Three prices, named apart, because two of them are in different units
        # and confusing them is a factor-of-100 error.
        f"Authorised cost / unit (structure) : {_or_dash(record.reference_price)}",
        f"  the same, in quoted terms        : {_or_dash(record.reference_quote)}",
        f"Submitted limit (quoted terms)     : {_or_dash(record.submitted_price)}",
        f"Capital authorised (M7)            : {record.capital_commitment}",
        f"Maximum loss authorised (M7)       : {record.maximum_loss}",
        f"Notional if fully filled at limit  : {_or_dash(record.submitted_notional)}",
    ]

    lines.extend(["", "Legs:"])
    for leg in record.legs:
        lines.append(
            f"  [{leg.leg_index}] {leg.action.value} {leg.ratio}x {leg.underlying} "
            f"{leg.expiration.isoformat()} {leg.strike} {leg.right.value} "
            f"conId={leg.contract_id} x{leg.multiplier}"
        )
    if not record.legs:
        lines.append("  (none recorded)")

    lines.extend(["", "Broker:"])
    lines.append(f"  order id        : {record.broker_order_id or 'none'}")
    lines.append(
        f"  status          : {record.broker_status.value if record.broker_status else 'none'}"
    )
    lines.append(f"  filled          : {record.filled_quantity} of {record.quantity}")
    lines.append(f"  remaining       : {_or_dash(record.remaining_quantity)}")
    lines.append(f"  average fill    : {_or_dash(record.average_fill_price)}")
    lines.append(f"  executed capital: {_or_dash(record.executed_capital)}")
    lines.append(f"  submitted at    : {_or_dash(record.submitted_at)}")
    lines.append(f"  acknowledged at : {_or_dash(record.acknowledged_at)}")
    lines.append(f"  filled at       : {_or_dash(record.filled_at)}")
    lines.append(f"  message         : {record.broker_message or 'none'}")
    lines.append(f"  orders submitted: {record.orders_submitted}")

    if record.reason_codes:
        lines.extend(["", "Reasons: " + ", ".join(code.value for code in record.reason_codes)])
    if record.failure_reason:
        lines.append(f"Detail : {record.failure_reason}")

    lines.extend(["", _state_note(record)])
    return "\n".join(lines)


def render_plan(plan: ExecutionPlan) -> str:
    """What *would* be submitted. The dry run's whole answer.

    Deliberately complete: the card, the derived limit price and the validation
    verdict. A dry run that only said "looks fine" would be a weaker promise
    than this milestone makes.
    """
    allocation = plan.allocation
    lines = [
        f"Allocation      : {allocation.allocation_id}",
        f"Underlying      : {allocation.symbol}",
        f"Strategy        : {allocation.strategy.value}",
        f"Outcome (M7)    : {allocation.outcome.value}",
        f"Quantity        : {allocation.quantity}",
        f"Capital (M7)    : {allocation.capital_committed} {allocation.currency or ''}",
        f"Max loss (M7)   : {allocation.total_max_loss}",
        f"Reference price : {_or_dash(allocation.unit_cost)}",
        f"Purchase card   : {plan.card.card_id if plan.card else 'not built'}",
    ]
    if plan.intent is not None:
        lines.extend(
            [
                f"Order intent    : {plan.intent.intent_id}",
                f"Would submit    : {plan.intent.order_type.value} "
                f"{plan.intent.quantity} x {plan.intent.underlying} @ "
                f"{plan.intent.limit_price} {plan.intent.time_in_force.value}",
                f"Legs            : {len(plan.intent.legs)}"
                + ("  (submitted as ONE combo order)" if len(plan.intent.legs) > 1 else ""),
            ]
        )
    lines.append(f"Would submit    : {'YES' if plan.submittable else 'NO'}")
    lines.append("Broker submission: NOT PERFORMED")
    if not plan.submittable:
        codes = plan.reason_codes or tuple(plan.validation.reason_codes)
        lines.append("Refused because : " + ", ".join(code.value for code in codes))
        detail = plan.detail or plan.validation.detail
        if detail:
            lines.append(f"Detail          : {detail}")
    return "\n".join(lines)


def render_execution_run(result: ExecutionRunResult) -> str:
    """The whole run: header plus one block per execution."""
    blocks = [render_run_summary(result)]
    for record in result.executions:
        blocks.append("-" * 72)
        blocks.append(render_execution(record))
    return "\n\n".join(blocks)


def _state_note(record: ExecutionRecord) -> str:
    """The sentence an operator most needs for this state.

    Written per state rather than generated from a template, because the whole
    value of these lines is that the dangerous state does not read like the
    safe one.
    """
    if record.dry_run:
        return "DRY RUN: nothing was sent to any broker and no broker state changed."
    return {
        ExecutionState.CREATED: "Built but not validated. Nothing sent.",
        ExecutionState.VALIDATED: "Validated and not sent.",
        ExecutionState.SUBMISSION_PENDING: (
            "Recorded immediately before the broker call and never updated. An order MAY be "
            "live. Do not resubmit; observe the broker."
        ),
        ExecutionState.SUBMITTED: (
            "The broker accepted the order. This is an acknowledgement, NOT a fill: no "
            "position exists from this until an execution report says so."
        ),
        ExecutionState.PARTIALLY_FILLED: (
            f"{record.filled_quantity} of {record.quantity} units have traded. The rest is "
            f"still working. This is a real, smaller position — not a whole one."
        ),
        ExecutionState.FILLED: "The whole structure traded.",
        ExecutionState.CANCEL_PENDING: (
            "Cancellation requested and not confirmed. The order can still fill."
        ),
        ExecutionState.CANCELLED: (
            "The order was cancelled."
            + (
                f" {record.filled_quantity} unit(s) had already traded and that position is real."
                if record.filled_quantity
                else " Nothing traded."
            )
        ),
        ExecutionState.REJECTED: "Refused. No order exists.",
        ExecutionState.EXPIRED: "The order expired without completing.",
        ExecutionState.UNKNOWN: (
            "UNCERTAIN. Something was sent and the answer never arrived. The order may be live "
            "at the broker right now. Do NOT resubmit — a retry here places the trade twice. "
            "Resolve by observing the broker."
        ),
        ExecutionState.FAILED: (
            "The attempt provably did not reach the broker. No order exists and nothing was sent."
        ),
    }[record.state]


def _or_dash(value: object) -> str:
    """``None`` prints as a dash, never as zero: they are different claims."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
