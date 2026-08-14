"""Human-readable rendering of exit decisions and position lifecycle.

Rendered on demand from the immutable records rather than stored alongside
them: a report is a view, and a stored view is a second copy of the truth that
can drift from the first.

Three rules govern every line here:

* **The verdict and its reason are never separated.** ``WAIT``, ``EXIT`` and
  ``BLOCK`` each print with the policy that produced them and the two numbers
  that policy compared, because "why is this position still open" is the
  question these commands exist to answer.
* **An unavailable value prints as** ``-`` **, never as** ``0``. Carried
  forward from Milestone 3: "no bid was quoted" and "the bid is zero" must not
  look the same on a screen either.
* **Quoted terms and money are labelled apart.** ``6.05`` and ``605.00``
  describe the same option and differ by a factor of a hundred, so every line
  says which one it is showing.
"""

from __future__ import annotations

from trading_system.domain.enums import (
    EXIT_POLICY_PRECEDENCE,
    ExitDecisionType,
    ThesisConditionOutcome,
    TrailingStopState,
)
from trading_system.exit.models import (
    ExitDecisionRecord,
    ExitEvaluation,
    ExitRunResult,
    PositionLifecycleEvent,
    PositionLifecycleSnapshot,
    TrailingStopRecord,
)

__all__ = [
    "render_decision",
    "render_evaluation",
    "render_lifecycle",
    "render_lifecycle_event",
    "render_run",
    "render_trailing",
]


def _or_dash(value: object | None) -> str:
    """An unavailable value is a dash. Never a zero."""
    return "-" if value is None else str(value)


def render_decision(decision: ExitDecisionRecord) -> str:
    """One verdict, in full."""
    lines = [
        f"EXIT DECISION  {decision.decision.value}",
        "",
        f"Position   : {decision.position_id}",
        f"Underlying : {decision.underlying}  ({decision.strategy.value})",
        f"Lifecycle  : {decision.lifecycle_state.value}",
        f"As of      : {decision.as_of.isoformat()}",
        f"Decision   : {decision.decision_id}",
        f"Evaluation : {decision.evaluation_id}",
        f"Policy     : {decision.policy_version}",
        "",
        f"Reasons    : {', '.join(code.value for code in decision.reason_codes)}",
        f"Triggered  : {decision.triggering_policy.value if decision.triggering_policy else '-'}",
        f"Quantity   : {decision.quantity} (whole structure; no independent-leg exit exists)",
        "",
        "ECONOMICS",
        f"  Exit quote   : {_or_dash(decision.exit_quote)} "
        f"({decision.quote_field.value}, broker quoted terms)",
        f"  Exit value   : {_or_dash(decision.exit_value)} (money, one unit)",
        f"  Entry cost   : {_or_dash(decision.entry_cost)} (money, one unit)",
        f"  Unrealised   : {_or_dash(decision.unrealized_pnl)} {decision.currency or ''}".rstrip(),
        f"  Return       : {_or_dash(decision.return_pct)}%",
        f"  DTE          : {_or_dash(decision.days_to_expiration)}",
        f"  Data quality : {decision.data_quality.value if decision.data_quality else '-'}",
        "",
        decision.summary,
    ]
    if decision.detail:
        lines.extend(["", decision.detail])
    if decision.recommended_action:
        lines.extend(["", decision.recommended_action])
    return "\n".join(lines)


def render_evaluation(evaluation: ExitEvaluation) -> str:
    """Every policy's verdict, in precedence order.

    Verbose on purpose, and printed in the *precedence* order rather than in
    the order that happens to be interesting: an operator reading this should
    be able to see which policy would have exited the position first, and which
    ones never got a say because a block came before them.
    """
    lines = [
        f"EXIT EVALUATION  {evaluation.evaluation_id}",
        "",
        f"Position   : {evaluation.position_id}  {evaluation.underlying} "
        f"({evaluation.strategy.value})",
        f"As of      : {evaluation.as_of.isoformat()}",
        f"Lifecycle  : {evaluation.lifecycle_state.value}",
        f"Structure  : {evaluation.structure_status.value}",
        f"Held       : {evaluation.open_quantity} unit(s)",
        f"DTE        : {_or_dash(evaluation.days_to_expiration)} "
        f"(expires {_or_dash(evaluation.expiration)})",
        f"Max loss   : {_or_dash(evaluation.max_loss_total)} "
        f"({evaluation.max_loss_basis.value if evaluation.max_loss_basis else 'undefined'})",
        f"Content    : {evaluation.content_hash}",
        f"Orders submitted: {evaluation.orders_submitted}",
        "",
        "POLICY OUTCOMES (precedence order: safety before profit-taking)",
    ]
    by_kind = {outcome.policy: outcome for outcome in evaluation.outcomes}
    for kind in EXIT_POLICY_PRECEDENCE:
        outcome = by_kind.get(kind)
        if outcome is None:
            lines.append(f"  {kind.value:<22} -")
            continue
        measured = f" [{outcome.measured} vs {outcome.threshold}]" if outcome.measured else ""
        flag = "" if outcome.evaluated else "  (not evaluated)"
        lines.append(
            f"  {kind.value:<22} {outcome.decision.value:<5} "
            f"{outcome.reason_code.value}{measured}{flag}"
        )
        lines.append(f"      {outcome.summary}")

    valuation = evaluation.valuation
    lines.extend(["", f"VALUATION  ({valuation.quote_field.value})"])
    for leg in valuation.legs:
        right = leg.right.value if leg.right else "-"
        lines.append(
            f"  leg {leg.leg_index}  {right} {_or_dash(leg.strike)} "
            f"{_or_dash(leg.expiration)}  price={_or_dash(leg.price)}  "
            f"bid={_or_dash(leg.bid)} ask={_or_dash(leg.ask)}  "
            f"held={_or_dash(leg.observed_quantity)}"
        )
        if leg.detail:
            lines.append(f"      {leg.detail}")
    lines.append(
        f"  structure   quote={_or_dash(valuation.exit_quote)}  "
        f"value={_or_dash(valuation.exit_value)}  entry={_or_dash(valuation.entry_cost)}"
    )

    if evaluation.trailing is not None:
        lines.extend(["", render_trailing(evaluation.trailing)])

    if evaluation.thesis_checks:
        lines.extend(["", "THESIS"])
        for check in evaluation.thesis_checks:
            lines.append(f"  [{check.outcome.value:<13}] {check.condition}")
            if check.outcome is not ThesisConditionOutcome.NOT_EVALUATED and check.evidence:
                lines.append(f"      evidence: {check.evidence}")
    return "\n".join(lines)


def render_trailing(record: TrailingStopRecord) -> str:
    """The trailing stop, with the history that explains it."""
    peak_at = record.peak_at.isoformat() if record.peak_at else None
    moved_at = record.level_updated_at.isoformat() if record.level_updated_at else None
    lines = [
        f"TRAILING STOP  {record.state.value}  ({record.quote_field.value}, quoted terms)",
        f"  entry        : {_or_dash(record.entry_quote)}",
        f"  activation   : +{record.activation_return_pct}%  "
        f"(reached at {_or_dash(record.activation_quote)})",
        f"  distance     : {record.distance_pct}% below the peak",
        f"  peak         : {_or_dash(record.peak_quote)}  at {_or_dash(peak_at)}",
        f"  level        : {_or_dash(record.stop_quote)}  moved {_or_dash(moved_at)}",
        f"  observations : {record.observations}",
    ]
    if record.state is TrailingStopState.TRIGGERED:
        fired_at = record.triggered_at.isoformat() if record.triggered_at else None
        lines.append(
            f"  TRIGGERED    : {_or_dash(record.trigger_quote)} at or below the level, "
            f"{_or_dash(fired_at)}"
        )
    if record.detail:
        lines.append(f"  {record.detail}")
    return "\n".join(lines)


def render_lifecycle(snapshot: PositionLifecycleSnapshot) -> str:
    """One position's lifecycle state and what it is waiting on."""
    submitted_at = snapshot.exit_submitted_at.isoformat() if snapshot.exit_submitted_at else None
    closed_at = snapshot.closed_at.isoformat() if snapshot.closed_at else None
    lines = [
        f"POSITION LIFECYCLE  {snapshot.state.value}",
        "",
        f"Position   : {snapshot.position_id}",
        f"Underlying : {snapshot.underlying}  ({snapshot.strategy.value})",
        f"Held       : {snapshot.open_quantity} unit(s) (from Milestone 9, not computed here)",
        f"As of      : {snapshot.as_of.isoformat()}",
        f"Updated    : {snapshot.updated_at.isoformat()}",
        f"Evaluations: {snapshot.evaluations}",
        f"Last       : {snapshot.last_decision.value if snapshot.last_decision else '-'}"
        f"  ({_or_dash(snapshot.last_evaluation_id)})",
        "",
        "PROVENANCE (ids, never copies)",
        f"  entry execution : {_or_dash(snapshot.entry_execution_id)}",
        f"  opportunity     : {_or_dash(snapshot.opportunity_id)}",
        f"  research report : {_or_dash(snapshot.research_report_id)}",
        "",
        "EXIT",
        f"  decision   : {_or_dash(snapshot.exit_decision_id)}",
        f"  request    : {_or_dash(snapshot.exit_request_id)}",
        f"  execution  : {_or_dash(snapshot.exit_execution_id)}",
        f"  submitted  : {_or_dash(submitted_at)}",
        f"  closed     : {_or_dash(closed_at)}",
    ]
    if snapshot.blocked_reason is not None:
        lines.extend(
            [
                "",
                f"BLOCKED: {snapshot.blocked_reason.value}",
                "This position leaves the blocked state only when the condition above is "
                "resolved. Nothing retries around it.",
            ]
        )
    if snapshot.detail:
        lines.extend(["", snapshot.detail])
    return "\n".join(lines)


def render_lifecycle_event(event: PositionLifecycleEvent) -> str:
    """One appended lifecycle observation, on one line plus detail."""
    parts = [
        event.occurred_at.isoformat(),
        f"{event.event_type.value:<24}",
        f"-> {event.state.value:<16}",
    ]
    if event.decision is not None:
        parts.append(f"{event.decision.value:<5}")
    if event.reason_code is not None:
        parts.append(event.reason_code.value)
    if event.stop_quote is not None or event.peak_quote is not None:
        parts.append(f"peak={_or_dash(event.peak_quote)} level={_or_dash(event.stop_quote)}")
    line = "  ".join(parts)
    return f"{line}\n      {event.detail}" if event.detail else line


def render_run(result: ExitRunResult) -> str:
    """One monitoring run, and — plainly — what it traded."""
    counts = result.counts
    lines = [
        f"EXIT RUN  {result.status.value}",
        "",
        f"Run        : {result.run_id}",
        f"Campaign   : {result.campaign_id}",
        f"As of      : {result.as_of.isoformat()}",
        f"Mode       : {result.trading_mode.value}",
        f"Policy     : {result.policy_version}",
        f"Dry run    : {result.dry_run}",
        f"Execution authorised: {result.execution_authorized}",
        "",
        f"Evaluated  : {counts.evaluated}",
        f"  WAIT     : {counts.waiting}",
        f"  EXIT     : {counts.exiting}",
        f"  BLOCK    : {counts.blocked}",
        f"  closed   : {counts.closed}",
        f"Exits submitted : {counts.exits_submitted}",
        f"Exits refused   : {counts.exits_refused}",
    ]
    if result.status_detail:
        lines.extend(["", result.status_detail])
    if result.decisions:
        lines.extend(["", "DECISIONS"])
        for decision in result.decisions:
            lines.append(
                f"  {decision.decision.value:<5} {decision.underlying:<6} "
                f"{decision.primary_reason.value:<28} {decision.position_id}"
            )
    exits = [d for d in result.decisions if d.decision is ExitDecisionType.EXIT]
    if exits and not result.execution_authorized:
        lines.extend(
            [
                "",
                f"{len(exits)} position(s) triggered an exit and NOTHING was submitted: this run "
                "was not authorised to execute.",
                "Evaluating whether a position should close and closing it are separate acts.",
            ]
        )
    return "\n".join(lines)
