"""Human-readable rendering of a universe run.

Rendered on demand from the immutable record rather than stored alongside it:
a report is a view, and a stored view is a second copy of the truth that can
drift from the first. Everything here is derived, so the report cannot say
anything the record does not.

The report leads with what the run *did* — status, method, and which model
ranked — rather than with the selected list. A universe produced by a
deterministic fallback and one produced by a model look identical if you only
read the tickers, and they should not.
"""

from __future__ import annotations

from trading_system.domain.enums import UniverseSelectionStatus
from trading_system.universe.models import UniverseSelectionResult

__all__ = ["render_report", "render_summary"]


def render_summary(result: UniverseSelectionResult) -> str:
    """One-paragraph header: what happened, and who decided."""
    counts = result.counts
    lines = [
        f"Universe Run: {result.run_id}",
        f"Snapshot    : {result.snapshot_id}",
        f"As Of       : {result.as_of.isoformat()}",
        f"Generated   : {result.generated_at.isoformat()}",
        f"Status      : {result.status.value}",
        f"Method      : {result.selection_method.value}",
        f"Source      : {result.universe_source.kind.value} "
        f"{result.universe_source.name} v{result.universe_source.version}",
    ]
    if result.agent_metadata is not None:
        meta = result.agent_metadata
        lines.append(
            f"Model       : {meta.model_provider}/{meta.model_name} "
            f"(prompt {meta.prompt_version}, agent {meta.agent_version or '-'})"
        )
    else:
        lines.append("Model       : none — no model was consulted for this run")

    lines.extend(
        [
            "",
            f"Candidates                : {counts.candidates}",
            f"Deterministically eligible: {counts.deterministic_pass}",
            f"Deterministically rejected: {counts.deterministic_reject}",
            f"Ranked                    : {counts.ai_input}",
            f"Selected                  : {counts.final}",
            f"Rejected                  : {len(result.rejected_assets)}",
        ]
    )
    if result.status_detail:
        lines.extend(["", f"Detail: {result.status_detail}"])
    return "\n".join(lines)


def render_report(result: UniverseSelectionResult, *, max_rejected: int = 50) -> str:
    """The full report: every selected asset in detail, every rejection with a reason."""
    sections = [render_summary(result), ""]

    if result.selected_assets:
        sections.append("SELECTED (research-ready underlyings)")
        sections.append("-" * 38)
        for asset in sorted(result.selected_assets, key=lambda a: a.rank):
            sections.append(
                f"  {asset.rank:>2}. {asset.symbol:<8} "
                f"score={_or_dash(asset.selection_score)} "
                f"confidence={asset.confidence.value}"
            )
            sections.append(f"      reasons        : {', '.join(r.value for r in asset.reasons)}")
            sections.append(
                f"      optionability  : {asset.optionability.value}   "
                f"price: {_or_dash(asset.reference_price)}   "
                f"avg daily volume: {_or_dash(asset.average_daily_volume)}   "
                f"session volume: {_or_dash(asset.underlying_volume)}"
            )
            sections.append(
                f"      data quality   : {asset.data_quality.classification.value} "
                f"(research_usable={asset.data_quality.research_usable})"
            )
            if asset.source is not None:
                snapshots = ", ".join(asset.source.snapshot_ids[:3]) or "none recorded"
                sections.append(
                    f"      provenance     : {asset.source.provider} "
                    f"retrieved {asset.source.retrieved_at.isoformat()}"
                )
                sections.append(f"      snapshots      : {snapshots}")
            if asset.rationale:
                sections.append(f"      rationale      : {asset.rationale}")
            sections.append("")
    else:
        sections.append("SELECTED: none.")
        sections.append(
            "  An empty universe is a valid outcome. Filters are never relaxed to "
            "manufacture candidates."
        )
        sections.append("")

    if result.rejected_assets:
        sections.append(f"REJECTED ({len(result.rejected_assets)})")
        sections.append("-" * 38)
        ordered = sorted(result.rejected_assets, key=lambda a: (a.reason.value, a.symbol))
        for rejection in ordered[:max_rejected]:
            sections.append(f"  {rejection.symbol:<8} {rejection.reason.value}")
            if rejection.detail:
                sections.append(f"           {rejection.detail}")
        if len(result.rejected_assets) > max_rejected:
            sections.append(
                f"  ... {len(result.rejected_assets) - max_rejected} more "
                f"(every one carries a machine-readable reason in the stored run)"
            )
        sections.append("")

    sections.append(
        f"Input snapshots: {len(result.input_snapshot_ids)} "
        f"({', '.join(result.input_snapshot_ids[:3]) or 'none'}"
        f"{', ...' if len(result.input_snapshot_ids) > 3 else ''})"
    )
    sections.append("Orders submitted: 0  (universe selection has no order path)")

    if result.status is not UniverseSelectionStatus.SUCCESS:
        sections.append("")
        sections.append(
            f"This run did not produce a universe ({result.status.value}). "
            f"No downstream stage may consume it."
        )
    return "\n".join(sections)


def _or_dash(value: object | None) -> str:
    return "-" if value is None else str(value)
