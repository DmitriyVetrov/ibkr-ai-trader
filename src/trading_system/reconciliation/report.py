"""Human-readable rendering of a reconciliation.

Rendered on demand from the immutable record: a report is a view, and a stored
view is a second copy of the truth that can drift from the first.

The wording here carries more weight than in any other report in the system,
because this one is read by a person deciding whether to intervene in a trading
account. Two rules:

* **It must be impossible to mistake this for trading.** Every rendering states
  the corrective-order count — always zero — next to the submitted-order count,
  and every recommendation is phrased ``ACTION REQUIRED``, never ``AUTO-SELL``
  or ``AUTO-BUY``. Nothing here proposes a trade, because nothing in this
  milestone can place one.
* **The two ledgers are labelled every time.** ``expected`` is what this system
  believes; ``broker`` is what the account actually holds. A reader who cannot
  tell which is which cannot tell a belief from a fact.
"""

from __future__ import annotations

from trading_system.domain.enums import (
    ReconciliationFindingType,
    ReconciliationSeverity,
)
from trading_system.reconciliation.models import ReconciliationFinding, ReconciliationResult
from trading_system.reconciliation.service import ReconciliationRun

__all__ = [
    "render_finding",
    "render_reconciliation",
    "render_run",
    "render_summary",
]


def render_summary(result: ReconciliationResult) -> str:
    """The header: what was compared, what disagreed, and what was traded (nothing)."""
    counts = result.counts
    lines = [
        "RECONCILIATION",
        "",
        f"mode    : {result.trading_mode.value}",
        f"broker  : {result.broker}",
        f"account : {result.account_reference}",
        f"as of   : {result.as_of.isoformat()}",
        f"status  : {result.status.value}",
        "",
        f"broker positions          : {result.broker_position_count} "
        f"(read {result.positions_read.value})",
        f"internal expected positions: {result.expected_position_count}",
        f"broker open orders        : {counts.orders_compared} (read {result.orders_read.value})",
        f"broker fills              : {counts.fills_compared} (read {result.fills_read.value})",
        f"executions considered     : {counts.executions_considered}",
        f"reservations              : {counts.reservations_compared}",
        "",
    ]

    tally = _tally(result)
    if tally:
        lines.append("findings:")
        lines.extend(f"  {name}: {count}" for name, count in tally)
    else:
        lines.append("findings: none")

    lines.extend(
        [
            "",
            f"orders submitted  : {result.orders_submitted}",
            f"corrective orders : {result.corrective_orders}",
        ]
    )
    if result.status_detail:
        lines.extend(["", result.status_detail])
    return "\n".join(lines)


def render_finding(finding: ReconciliationFinding) -> str:
    """One finding, with both sides and the difference."""
    lines = [
        f"[{finding.severity.value}] {finding.finding_type.value}  {finding.identifier}",
        f"  {finding.summary}",
    ]
    if finding.expected_value is not None or finding.observed_value is not None:
        # Both sides always, labelled, even when one of them is absent — "the
        # broker has none" and "we did not look" print differently below, and
        # a reader must never have to guess which ledger a number came from.
        expected = finding.expected_value if finding.expected_value is not None else "-"
        observed = finding.observed_value if finding.observed_value is not None else "-"
        lines.append(f"  expected (internal): {expected}")
        lines.append(f"  observed (broker)  : {observed}")
    if finding.delta is not None:
        lines.append(f"  difference         : {finding.delta}")
    if finding.expected_provenance:
        lines.append(f"  internal provenance: {finding.expected_provenance}")
    if finding.broker_provenance:
        lines.append(f"  broker provenance  : {finding.broker_provenance}")
    if finding.observed_at:
        lines.append(f"  observed at        : {finding.observed_at.isoformat()}")
    if finding.broker_timestamp:
        lines.append(f"  broker timestamp   : {finding.broker_timestamp.isoformat()}")
    if finding.detail:
        lines.append(f"  detail             : {finding.detail}")
    if finding.recommended_action:
        lines.append(f"  {finding.recommended_action}")
    return "\n".join(lines)


def render_reconciliation(result: ReconciliationResult, *, include_agreements: bool = False) -> str:
    """The whole comparison: summary, then every finding worth reading."""
    lines = [render_summary(result)]
    findings = result.findings if include_agreements else result.disagreements
    if findings:
        lines.extend(["", "-" * 72, ""])
        for finding in sorted(findings, key=_severity_order):
            lines.append(render_finding(finding))
            lines.append("")
    if result.executions_resolved:
        lines.append(
            f"Executions resolved from broker evidence: {', '.join(result.executions_resolved)}"
        )
    if result.reservations_retained_unknown:
        lines.append(
            f"Reservations held because an execution is UNKNOWN: "
            f"{', '.join(result.reservations_retained_unknown)}"
        )
    lines.extend(
        [
            "",
            "Nothing above was corrected automatically. Reconciliation reports; it does not",
            "repair, adopt, hedge, cancel or trade.",
        ]
    )
    return "\n".join(lines)


def render_run(run: ReconciliationRun) -> str:
    """One run, including what it wrote."""
    lines = [render_reconciliation(run.result)]
    lines.extend(
        [
            "",
            f"Stored          : {'yes' if run.stored else 'no'}"
            f"{'' if run.is_new or not run.stored else '  (re-observation of identical state)'}",
            f"Fills recorded  : {len(run.capture.recorded_fills)}",
            f"Fills known     : {len(run.capture.reobserved_fills)}",
            f"Reservations moved: {sum(1 for update in run.updates if update.applied)}",
        ]
    )
    if run.dry_run:
        lines.append("")
        lines.append(
            "DRY RUN — the broker was read and nothing at all was written: no snapshot, no "
            "fill, no execution resolution, no reservation movement, no result."
        )
    return "\n".join(lines)


def _tally(result: ReconciliationResult) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for finding in result.findings:
        if finding.finding_type in _QUIET:
            continue
        counts[finding.finding_type.value] = counts.get(finding.finding_type.value, 0) + 1
    return sorted(counts.items())


#: Agreement findings that would only add noise to a summary tally.
_QUIET = frozenset(
    {
        ReconciliationFindingType.POSITION_MATCH,
        ReconciliationFindingType.ORDER_MATCH,
        ReconciliationFindingType.FILL_MATCH,
        ReconciliationFindingType.RESERVATION_MATCH,
    }
)

_SEVERITY_RANK = {
    ReconciliationSeverity.CRITICAL: 0,
    ReconciliationSeverity.WARNING: 1,
    ReconciliationSeverity.INFO: 2,
}


def _severity_order(finding: ReconciliationFinding) -> tuple[int, str, str]:
    """Most serious first, then deterministic by type and identifier."""
    return (
        _SEVERITY_RANK[finding.severity],
        finding.finding_type.value,
        finding.identifier,
    )
