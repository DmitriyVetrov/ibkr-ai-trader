"""Human-readable rendering of a research run.

Rendered on demand from the immutable record rather than stored alongside it: a
report is a view, and a stored view is a second copy of the truth that can
drift from the first. Everything here is derived, so the rendering cannot say
anything the record does not.

The output leads with what the run *did* — status, which model answered, what
data was missing — rather than with the hypothesis. A ``B`` reached on three
tier-1 filings and a ``B`` reached on one tier-4 blog post look identical if
you only read the letter, and they should not.
"""

from __future__ import annotations

from trading_system.domain.enums import ClaimSupport, EvidenceStance, ResearchStatus
from trading_system.research.models import (
    MarketResearchReport,
    ResearchRunResult,
)

__all__ = ["render_report", "render_run", "render_summary"]

_HYPOTHESIS_MEANING = {
    "A": "strong move likely, no specific catalyst required, direction uncertain",
    "B": "predominantly up",
    "C": "predominantly down",
    "D": "sharp move expected around a specific identified event, direction uncertain",
    "E": "other — see the explanation",
}


def render_summary(result: ResearchRunResult) -> str:
    """One-paragraph header: what happened, over what, and who answered."""
    counts = result.counts
    lines = [
        f"Research Run: {result.run_id}",
        f"As Of       : {result.as_of.isoformat()}",
        f"Generated   : {result.generated_at.isoformat()}",
        f"Status      : {result.status.value}",
        f"Horizon     : {result.horizon.min_days}-{result.horizon.max_days} days",
        f"Universe    : {result.universe_run_id or 'none'} "
        f"(snapshot {result.universe_snapshot_id or '-'})",
    ]

    model = next((r.agent_metadata for r in result.reports if r.agent_metadata), None)
    if model is not None:
        lines.append(
            f"Model       : {model.model_provider}/{model.model_name} "
            f"(prompt {model.prompt_version}, agent {model.agent_version or '-'})"
        )
    else:
        lines.append("Model       : none — no model produced an outlook in this run")

    lines.extend(
        [
            "",
            f"Universe assets       : {counts.universe_assets}",
            f"Researched            : {counts.researched}",
            f"Outlook produced      : {counts.succeeded}",
            f"Insufficient evidence : {counts.insufficient_evidence}",
            f"No data               : {counts.no_data}",
            f"Failed                : {counts.failed}",
            f"Skipped (cost limit)  : {counts.skipped}",
        ]
    )
    if result.status_detail:
        lines.extend(["", f"Detail: {result.status_detail}"])
    return "\n".join(lines)


def render_run(result: ResearchRunResult, *, verbose: bool = False) -> str:
    """The whole run: one block per underlying, with what it rests on."""
    sections = [render_summary(result), ""]

    if not result.reports:
        sections.append("No underlying was researched.")
        sections.append(
            "  An empty research run is a valid outcome. Nothing is invented to fill it."
        )
        sections.append("")
    else:
        sections.append("REPORTS")
        sections.append("-" * 38)
        for report in sorted(result.reports, key=lambda r: (not r.succeeded, r.symbol)):
            sections.append(_render_line(report))
            if verbose and report.succeeded:
                sections.append(_indent(render_report(report), 6))
                sections.append("")
        sections.append("")

    sections.append("Orders submitted: 0  (research has no order path)")
    if result.status is not ResearchStatus.SUCCESS:
        sections.append("")
        sections.append(
            f"This run produced no outlook ({result.status.value}). "
            f"No downstream stage may consume it."
        )
    return "\n".join(sections)


def render_report(report: MarketResearchReport) -> str:
    """One underlying's full outlook, with every conclusion traceable."""
    lines = [
        f"{report.symbol} — {report.status.value}",
        f"  report id   : {report.report_id}",
        f"  as of       : {report.as_of.isoformat()}",
        f"  horizon     : {report.horizon.min_days}-{report.horizon.max_days} days",
    ]

    if not report.succeeded:
        lines.append(f"  reason      : {report.status_detail or 'not stated'}")
        lines.append(
            "  outlook     : none. A failed or inconclusive report states no hypothesis, "
            "no direction and no confidence."
        )
        lines.extend(_render_data_quality(report))
        return "\n".join(lines)

    assert report.hypothesis is not None  # narrowing; guaranteed by the model
    assert report.confidence is not None
    lines.extend(
        [
            f"  hypothesis  : {report.hypothesis.value} "
            f"({_HYPOTHESIS_MEANING[report.hypothesis.value]})",
            f"  direction   : {report.direction.value if report.direction else '-'}",
            f"  magnitude   : "
            f"{report.expected_magnitude.value if report.expected_magnitude else '-'}",
            f"  confidence  : {report.confidence.value}  (a band, not a probability)",
            f"  horizon days: {report.horizon_days}",
            "",
            f"  THESIS: {report.thesis}",
            f"  EXPECTED: {report.expected_behavior}",
        ]
    )
    if report.explanation:
        lines.append(f"  EXPLANATION: {report.explanation}")

    supporting = report.supporting_evidence
    contradicting = report.contradicting_evidence
    if report.evidence:
        lines.append("")
        lines.append(f"  EVIDENCE ({len(report.evidence)})")
        for item in report.evidence:
            marker = {
                EvidenceStance.SUPPORTS: "+",
                EvidenceStance.CONTRADICTS: "-",
                EvidenceStance.NEUTRAL: "=",
            }[item.stance]
            lines.append(
                f"    {marker} [{item.source_tier.value}] {item.direction.value} "
                f"relevance={item.relevance.value} — {item.claim}"
            )
            lines.append(f"        fact  : {item.fact}")
            lines.append(
                f"        source: {item.source.source_name or item.source.provider}"
                + (f" ({item.source.source_identifier})" if item.source.source_identifier else "")
            )
            published = (
                item.source.published_at.isoformat() if item.source.published_at else "unknown"
            )
            lines.append(
                f"        published {published}, retrieved "
                f"{item.source.retrieved_at.isoformat()}, snapshot {item.source.snapshot_id}"
            )
            if item.duplicate_count:
                lines.append(
                    f"        corroborated by {item.duplicate_count} further report(s): "
                    f"{', '.join(item.duplicate_source_names) or 'unnamed'} "
                    f"(one story, not {item.duplicate_count + 1} catalysts)"
                )

    if contradicting:
        lines.append("")
        lines.append(
            f"  CONTRADICTING evidence is retained ({len(contradicting)} of "
            f"{len(report.evidence)}); disagreement is never dropped."
        )
        lines.append(
            f"    resolution: {report.contradiction_resolution or 'none stated'}"
            + ("" if report.contradiction_resolution else "  <- unresolved")
        )
    elif supporting:
        lines.append("")
        lines.append("  No contradicting evidence was recorded.")

    if report.key_events:
        lines.append("")
        lines.append(f"  KEY EVENTS ({len(report.key_events)})")
        for event in report.key_events:
            lines.append(
                f"    {event.event_type.value} at {event.expected_event_time.isoformat()} "
                f"({_days(event.days_until)}, "
                f"{'in horizon' if event.within_horizon else 'outside horizon'})"
            )
            lines.append(
                f"        relevance={event.expected_relevance.value} "
                f"direction_uncertain={event.directional_uncertainty} "
                f"confirmed={event.confirmed}"
            )
            announced = event.announced_at.isoformat() if event.announced_at else "unknown"
            lines.append(
                f"        announced {announced}, source "
                f"{event.source.source_name or event.source.provider} "
                f"[{event.source_tier.value}]"
            )
            lines.append(f"        {event.summary}")

    for label, catalysts in (
        ("BULLISH CATALYSTS", report.bullish_catalysts),
        ("BEARISH CATALYSTS", report.bearish_catalysts),
    ):
        if not catalysts:
            continue
        lines.append("")
        lines.append(f"  {label}")
        for catalyst in catalysts:
            tag = "" if catalyst.support is ClaimSupport.SUPPORTED else "  [UNSUPPORTED]"
            lines.append(f"    - {catalyst.summary}{tag}")
            if catalyst.evidence_ids:
                lines.append(f"        evidence: {', '.join(catalyst.evidence_ids)}")

    if report.risks:
        lines.append("")
        lines.append("  RISKS")
        for risk in report.risks:
            tag = "" if risk.support is ClaimSupport.SUPPORTED else "  [UNSUPPORTED]"
            lines.append(f"    {risk.category.value}: {risk.description}{tag}")

    lines.append("")
    lines.append("  INVALIDATION CONDITIONS")
    for condition in report.invalidation_conditions:
        tag = "" if condition.support is ClaimSupport.SUPPORTED else "  [UNSUPPORTED]"
        lines.append(f"    - {condition.condition}{tag}")
        if condition.observable:
            lines.append(f"        observable: {condition.observable}")

    if report.missing_information:
        lines.append("")
        lines.append("  MISSING INFORMATION")
        for missing in report.missing_information:
            lines.append(f"    - {missing}")

    lines.extend(_render_data_quality(report))

    if report.agent_metadata is not None:
        meta = report.agent_metadata
        lines.append("")
        lines.append(
            f"  model       : {meta.model_provider}/{meta.model_name} "
            f"(version {meta.model_version or 'not reported'})"
        )
        lines.append(
            f"  prompt      : {meta.prompt_version} "
            f"(fingerprint {meta.prompt_fingerprint or 'not recorded'})"
        )
    lines.append(f"  snapshots   : {', '.join(report.input_snapshot_ids) or 'none'}")
    return "\n".join(lines)


def _render_data_quality(report: MarketResearchReport) -> list[str]:
    quality = report.data_quality
    lines = [
        "",
        f"  data quality: {quality.classification.value} "
        f"(research_usable={quality.research_usable}); "
        f"{quality.records_research_usable}/{quality.records_considered} facts usable",
    ]
    if quality.gaps:
        lines.append(f"  data gaps   : {', '.join(g.value for g in quality.gaps)}")
        lines.append("                an unavailable value is unavailable, never zero or false")
    if report.limits.truncated:
        lines.append(
            f"  truncated   : {', '.join(report.limits.truncated)} "
            f"(cost limits applied; a thin input is not a quiet market)"
        )
    return lines


def _render_line(report: MarketResearchReport) -> str:
    if not report.succeeded:
        return f"  {report.symbol:<8} {report.status.value:<28} no outlook"
    assert report.hypothesis is not None
    assert report.confidence is not None
    return (
        f"  {report.symbol:<8} {report.hypothesis.value} "
        f"{_HYPOTHESIS_MEANING[report.hypothesis.value]:<62} "
        f"confidence={report.confidence.value} evidence={len(report.evidence)}"
    )


def _days(days_until: int | None) -> str:
    if days_until is None:
        return "timing unknown"
    if days_until < 0:
        return f"{abs(days_until)} days ago"
    return f"in {days_until} days"


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(f"{pad}{line}" if line else line for line in text.splitlines())
