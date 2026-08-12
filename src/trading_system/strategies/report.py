"""Human-readable rendering of strategy decisions and contract selections.

Rendered on demand from the immutable records rather than stored alongside
them: a report is a view, and a stored view is a second copy of the truth that
can drift from the first.

Both renderings lead with *why*. A ``LONG_CALL`` and a ``LONG_CALL`` look
identical if you only read the strategy name — one may rest on three tier-1
filings and a HIGH confidence, the other on a single blog post — and the same
is true of a contract: the strike matters far less than the policy that chose it
and the candidates it beat.
"""

from __future__ import annotations

from trading_system.domain.enums import ContractSelectionStatus, StrategyAction
from trading_system.strategies.models import (
    ContractSelectionResult,
    ContractSelectionRunResult,
    StrategyDecisionRecord,
    StrategyRunResult,
)

__all__ = [
    "render_contract_run",
    "render_decision",
    "render_selection",
    "render_strategy_run",
    "render_strategy_summary",
]


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
def render_strategy_summary(result: StrategyRunResult) -> str:
    """One-paragraph header: what happened, over what, and who decided."""
    counts = result.counts
    lines = [
        f"Strategy Run: {result.run_id}",
        f"As Of       : {result.as_of.isoformat()}",
        f"Generated   : {result.generated_at.isoformat()}",
        f"Status      : {result.status.value}",
        f"Research    : {result.research_run_id or 'none'}",
    ]
    model = next((d.agent_metadata for d in result.decisions if d.agent_metadata), None)
    if model is not None:
        lines.append(
            f"Model       : {model.model_provider}/{model.model_name} "
            f"(prompt {model.prompt_version}, agent {model.agent_version or '-'})"
        )
    else:
        lines.append("Model       : none — no model reached a decision in this run")

    lines.extend(
        [
            "",
            f"Researched assets     : {counts.researched_assets}",
            f"Considered            : {counts.considered}",
            f"Strategy proposed     : {counts.proposed}",
            f"No trade              : {counts.no_trade}",
            f"Failed                : {counts.failed}",
            f"Skipped (cost limit)  : {counts.skipped}",
        ]
    )
    if result.status_detail:
        lines.extend(["", f"Detail: {result.status_detail}"])
    return "\n".join(lines)


def render_strategy_run(result: StrategyRunResult, *, verbose: bool = False) -> str:
    """The whole run: one block per underlying, with what it rests on."""
    sections = [render_strategy_summary(result), ""]

    if not result.decisions:
        sections.append("No underlying reached the strategy stage.")
        sections.append(
            "  An empty strategy run is a valid outcome. Nothing is invented to fill it."
        )
        sections.append("")
    else:
        sections.append("DECISIONS")
        sections.append("-" * 38)
        for decision in sorted(result.decisions, key=lambda d: (not d.proposes_a_trade, d.symbol)):
            sections.append(_decision_line(decision))
            if verbose:
                sections.append(_indent(render_decision(decision), 6))
                sections.append("")
        sections.append("")

    sections.append("Orders submitted: 0  (strategy selection has no order path)")
    sections.append("A strategy decision is not an order. No contract has been selected here.")
    return "\n".join(sections)


def render_decision(decision: StrategyDecisionRecord) -> str:
    """One underlying's decision and everything behind it."""
    lines = [
        f"{decision.symbol} — {decision.status.value} / {decision.action.value}",
        f"  decision id : {decision.decision_id}",
        f"  as of       : {decision.as_of.isoformat()}",
        f"  hypothesis  : {decision.hypothesis.value if decision.hypothesis else '-'}"
        f" (research confidence "
        f"{decision.research_confidence.value if decision.research_confidence else '-'})",
        f"  horizon     : {decision.research_horizon_days or '-'} days",
        f"  method      : {decision.decision_method.value}",
    ]
    if decision.action is StrategyAction.BUY:
        chosen = decision.selected_strategy.value if decision.selected_strategy else "-"
        lines.append(f"  strategy    : {chosen} (spec {decision.strategy_version or '-'})")
        lines.append(
            f"  confidence  : {decision.confidence.value if decision.confidence else '-'}"
            f"  (a band, not a probability)"
        )
    else:
        lines.append("  strategy    : none — NO_TRADE is a first-class outcome")

    if decision.eligible_strategies:
        lines.append(f"  eligible    : {', '.join(s.value for s in decision.eligible_strategies)}")
    else:
        lines.append("  eligible    : none for this hypothesis")
    if decision.reasons:
        lines.append(f"  reasons     : {', '.join(r.value for r in decision.reasons)}")
    if decision.rationale:
        lines.append(f"  RATIONALE   : {decision.rationale}")
    if decision.status_detail and decision.status_detail != decision.rationale:
        lines.append(f"  detail      : {decision.status_detail}")

    readiness = decision.data_readiness
    lines.append(
        f"  option data : chain={readiness.option_chain_available} "
        f"quotes={readiness.option_quotes_available} "
        f"expirations={readiness.expirations_visible} strikes={readiness.strikes_visible}"
    )
    lines.append(f"  research    : {decision.research_report_id or '-'}")
    if decision.agent_metadata is not None:
        meta = decision.agent_metadata
        lines.append(
            f"  model       : {meta.model_provider}/{meta.model_name} "
            f"(prompt {meta.prompt_version}, fingerprint "
            f"{meta.prompt_fingerprint or 'not recorded'})"
        )
    lines.append(f"  snapshots   : {', '.join(decision.input_snapshot_ids) or 'none'}")
    return "\n".join(lines)


def _decision_line(decision: StrategyDecisionRecord) -> str:
    if not decision.succeeded:
        return f"  {decision.symbol:<8} {decision.status.value:<28} no decision"
    if decision.action is StrategyAction.NO_TRADE:
        reasons = ", ".join(r.value for r in decision.reasons) or "judgement"
        return f"  {decision.symbol:<8} NO_TRADE  ({reasons})"
    strategy = decision.selected_strategy.value if decision.selected_strategy else "?"
    confidence = decision.confidence.value if decision.confidence else "-"
    hypothesis = decision.hypothesis.value if decision.hypothesis else "-"
    return f"  {decision.symbol:<8} {strategy:<16} hypothesis={hypothesis} confidence={confidence}"


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
def render_contract_run(result: ContractSelectionRunResult, *, verbose: bool = False) -> str:
    """The whole selection run, one block per underlying."""
    counts = result.counts
    sections = [
        f"Contract Run: {result.run_id}",
        f"As Of       : {result.as_of.isoformat()}",
        f"Generated   : {result.generated_at.isoformat()}",
        f"Status      : {result.status.value}",
        f"Strategy run: {result.strategy_run_id or 'none'}",
        f"Policy      : {result.selection_policy_version} (deterministic; no model involved)",
        "",
        f"Decisions considered : {counts.decisions_considered}",
        f"Contracts selected   : {counts.selected}",
        f"No valid contract    : {counts.no_contract}",
        f"No trade upstream    : {counts.no_trade}",
        f"Failed               : {counts.failed}",
        "",
    ]
    if result.status_detail:
        sections.extend([f"Detail: {result.status_detail}", ""])

    if not result.selections:
        sections.append("No decision reached contract selection.")
        sections.append("  Nothing is invented to fill an empty run.")
    else:
        sections.append("SELECTIONS")
        sections.append("-" * 38)
        for selection in sorted(result.selections, key=lambda s: (not s.succeeded, s.symbol)):
            sections.append(_selection_line(selection))
            if verbose:
                sections.append(_indent(render_selection(selection), 6))
                sections.append("")
    sections.append("")
    sections.append("Orders submitted: 0  (contract selection has no order path)")
    return "\n".join(sections)


def render_selection(selection: ContractSelectionResult) -> str:
    """One underlying's contracts, with why each was chosen and what was not."""
    lines = [
        f"{selection.symbol} — {selection.selection_status.value}",
        f"  selection id: {selection.selection_id}",
        f"  as of       : {selection.as_of.isoformat()}",
        f"  strategy    : {selection.strategy.value if selection.strategy else '-'}"
        f" (spec {selection.strategy_version or '-'})",
        f"  decision    : {selection.strategy_decision_id or '-'}",
        f"  policy      : {selection.selection_policy_version}",
    ]
    if selection.reference_price is not None:
        lines.append(
            f"  reference   : {selection.reference_price} "
            f"(from {selection.reference_price_field or 'unknown field'})"
        )

    if not selection.succeeded:
        lines.append(f"  outcome     : no contract selected — {selection.selection_status.value}")
        if selection.status_detail:
            lines.append(f"  detail      : {selection.status_detail}")
        lines.extend(_render_reasons(selection))
        lines.extend(_render_rejections(selection))
        return "\n".join(lines)

    lines.append(
        f"  expiration  : {selection.expiration.isoformat() if selection.expiration else '-'} "
        f"(DTE {selection.dte}) by "
        f"{selection.expiration_policy.value if selection.expiration_policy else '-'}"
    )
    lines.append("")
    lines.append(f"  LEGS ({len(selection.legs)})")
    for leg in selection.legs:
        lines.append(
            f"    {leg.action.value} {leg.right.value} {leg.strike} "
            f"exp {leg.expiration.isoformat()} x{leg.ratio} "
            f"(multiplier {leg.multiplier})"
        )
        lines.append(
            f"        contract id {leg.contract_id}, trading class {leg.trading_class}, "
            f"exchange {leg.exchange or '-'}"
        )
        lines.append(f"        chosen by {leg.strike_policy.value}: {leg.selection_reason}")
        lines.append(
            f"        bid={_show(leg.bid)} ask={_show(leg.ask)} last={_show(leg.last)} "
            f"iv={_show(leg.implied_volatility)} delta={_show(leg.delta)}"
        )
        lines.append(
            f"        volume={_show(leg.volume)} open_interest={_show(leg.open_interest)} "
            f"(option-level, never the underlying's)"
        )
        lines.append(
            f"        quote snapshot {leg.quote_snapshot_id or 'none'}, "
            f"chain snapshot {leg.chain_snapshot_id}"
        )

    cost = selection.cost
    lines.append("")
    if cost is None or not cost.available:
        reason = cost.unavailable_reason if cost else "no cost estimate was computed"
        lines.append(f"  cost        : UNKNOWN — {reason}")
    else:
        lines.append(
            f"  cost        : {cost.estimated_debit} {cost.currency or ''} at the ask "
            f"(midpoint {_show(cost.estimated_mid_debit)}), for one unit of the structure"
        )
        lines.append(
            "                No quantity and no allocation: sizing belongs to the risk and "
            "allocation engines."
        )
    lines.extend(_render_reasons(selection))
    lines.extend(_render_rejections(selection))
    lines.append(f"  snapshots   : {', '.join(selection.input_snapshot_ids) or 'none'}")
    return "\n".join(lines)


def _render_reasons(selection: ContractSelectionResult) -> list[str]:
    if not selection.reasons:
        return []
    lines = ["", "  SELECTION REASONING"]
    lines.extend(f"    - {reason}" for reason in selection.reasons[:12])
    if len(selection.reasons) > 12:
        lines.append(f"    ... and {len(selection.reasons) - 12} more")
    return lines


def _render_rejections(selection: ContractSelectionResult) -> list[str]:
    if not selection.rejected_candidates:
        return []
    lines = [
        "",
        f"  REJECTED CANDIDATES ({len(selection.rejected_candidates)} of "
        f"{selection.candidates_considered} considered)",
    ]
    for rejected in selection.rejected_candidates[:15]:
        identity = " ".join(
            part
            for part in (
                rejected.expiration.isoformat() if rejected.expiration else None,
                str(rejected.strike) if rejected.strike is not None else None,
                rejected.right.value if rejected.right else None,
            )
            if part
        )
        lines.append(f"    {rejected.reason.value:<32} {identity or '(chain-level)'}")
        if rejected.detail:
            lines.append(f"        {rejected.detail}")
    if len(selection.rejected_candidates) > 15:
        lines.append(f"    ... and {len(selection.rejected_candidates) - 15} more")
    if selection.rejections_recorded_truncated:
        lines.append(
            "    (the rejection list is capped by configuration; the considered count above "
            "is complete)"
        )
    return lines


def _selection_line(selection: ContractSelectionResult) -> str:
    if selection.selection_status is ContractSelectionStatus.NO_TRADE:
        return f"  {selection.symbol:<8} NO_TRADE upstream — nothing to select"
    if not selection.succeeded:
        return f"  {selection.symbol:<8} {selection.selection_status.value:<28} no contract"
    strategy = selection.strategy.value if selection.strategy else "?"
    legs = ", ".join(f"{leg.right.value} {leg.strike}" for leg in selection.legs)
    return (
        f"  {selection.symbol:<8} {strategy:<16} "
        f"{selection.expiration.isoformat() if selection.expiration else '-'} "
        f"(DTE {selection.dte}) {legs}"
    )


def _show(value: object | None) -> str:
    """Render a value. Absence stays visible: unavailable is never zero."""
    return "unavailable" if value is None else str(value)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(f"{pad}{line}" if line else line for line in text.splitlines())
