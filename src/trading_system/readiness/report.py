"""Rendering readiness assessments for a person (Milestone 12).

Pure formatting. Nothing here decides anything, and nothing here reaches a
broker, a store or a network — it turns a
:class:`~trading_system.readiness.models.ReadinessRun` into text.

Two things it is careful about, both learned from the rest of this system:

* **A level is never printed without its blockers.** ``NOT_READY`` on its own
  is not something anybody can act on; the criteria holding the gate shut are.
* **The word "READY" never appears without its qualification.** The strongest
  conclusion available is ``READY_FOR_LIVE_REVIEW``, and every rendering of it
  carries the sentence saying live trading is still off.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from trading_system.domain.enums import (
    ReadinessDomain,
    ReadinessLevel,
    ReadinessStatus,
    SignoffStatus,
)
from trading_system.readiness.models import (
    LiveReadinessSignoff,
    ReadinessAssessment,
    ReadinessCriterion,
    ReadinessRun,
)

__all__ = [
    "LEVEL_STYLE",
    "STATUS_STYLE",
    "render_criteria",
    "render_run",
    "render_signoff",
    "render_summary",
]

#: Colours per status. ``UNKNOWN``, ``STALE`` and ``NOT_TESTED`` are all
#: deliberately *not* green — the reader must not be able to skim a report and
#: come away thinking an unexamined criterion was fine.
STATUS_STYLE: dict[ReadinessStatus, str] = {
    ReadinessStatus.PASS: "green",
    ReadinessStatus.FAIL: "red",
    ReadinessStatus.UNKNOWN: "yellow",
    ReadinessStatus.STALE: "yellow",
    ReadinessStatus.NOT_TESTED: "dim",
}

LEVEL_STYLE: dict[ReadinessLevel, str] = {
    ReadinessLevel.NOT_READY: "red",
    ReadinessLevel.READY_FOR_PAPER: "green",
    ReadinessLevel.READY_FOR_LIVE_REVIEW: "cyan",
}

#: Printed under every level. The disclaimer travels with the verdict rather
#: than living in documentation somebody may not have read.
LEVEL_MEANING: dict[ReadinessLevel, str] = {
    ReadinessLevel.NOT_READY: (
        "At least one blocking criterion is not satisfied. Nothing here prevents ordinary "
        "development; it means the paper gate has not been met."
    ),
    ReadinessLevel.READY_FOR_PAPER: (
        "The paper-trading readiness gate is met. This is NOT an authorisation to submit "
        "orders: execution.enabled and --confirm remain separate controls, and readiness "
        "never touches either."
    ),
    ReadinessLevel.READY_FOR_LIVE_REVIEW: (
        "Every machine-checkable live prerequisite is satisfied. LIVE TRADING REMAINS OFF. "
        "A human must review and sign the checklist, and the existing live guards must be "
        "set deliberately. There is no automatic transition from here to LIVE."
    ),
}


def render_run(console: Console, run: ReadinessRun, *, verbose: bool = False) -> None:
    """Print one readiness run in full."""
    render_summary(console, run)
    if run.assessment is None:
        console.print(f"[red]No assessment was produced:[/red] {run.error or 'unknown reason'}")
        return
    render_criteria(console, run.assessment.criteria, verbose=verbose)
    _render_blockers(console, run.assessment)


def render_summary(console: Console, run: ReadinessRun) -> None:
    """The header: what was assessed, at what revision, and what it concluded."""
    level = run.level
    style = LEVEL_STYLE[level]

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Readiness run", run.readiness_run_id)
    table.add_row("Run status", run.status.value)
    table.add_row("Evaluated at", run.evaluated_at.isoformat())
    table.add_row("Trading mode", run.trading_mode.value)
    table.add_row("Git revision", run.git_revision or "[red]NOT AVAILABLE[/red]")
    table.add_row(
        "Working tree",
        _tree_state(run.working_tree_clean),
    )
    table.add_row("Config version", run.config_version or "-")
    table.add_row("Readiness policy", run.readiness_config_version or "-")
    # Printed on every run, deliberately. Readiness observes a system; a
    # non-zero count here is refused by the model, and showing the zero is how
    # a reader confirms it rather than assuming it.
    table.add_row("Orders submitted", str(run.orders_submitted))

    if run.assessment is not None:
        counts = run.assessment.counts
        summary = "  ".join(
            f"[{STATUS_STYLE[status]}]{status.value} {counts[status.value]}[/]"
            for status in ReadinessStatus
        )
        table.add_row("Criteria", summary)

    console.print(
        Panel(
            table,
            title=f"[{style}]{level.value}[/{style}]",
            subtitle=LEVEL_MEANING[level],
            border_style=style,
        )
    )


def _tree_state(clean: bool | None) -> str:
    if clean is None:
        return "[yellow]UNKNOWN[/yellow]"
    return "CLEAN" if clean else "[yellow]DIRTY[/yellow]"


def render_criteria(
    console: Console,
    criteria: Iterable[ReadinessCriterion],
    *,
    verbose: bool = False,
) -> None:
    """One table per domain, in catalogue order.

    Grouped by domain because that is how a reader acts on it: a broker
    problem and a lint problem go to different people, and an undifferentiated
    list of forty-nine rows is one nobody reads to the end.
    """
    materialised = list(criteria)
    for domain in ReadinessDomain:
        rows = [criterion for criterion in materialised if criterion.domain is domain]
        if not rows:
            continue
        if not verbose and all(row.status is ReadinessStatus.NOT_TESTED for row in rows):
            console.print(
                f"[dim]{domain.value}: {len(rows)} criteria NOT_TESTED "
                f"(no evidence was collected)[/dim]"
            )
            continue

        table = Table(title=domain.value, show_lines=False, expand=True)
        table.add_column("Status", width=11)
        table.add_column("Criterion", width=36)
        table.add_column("Detail", overflow="fold")
        table.add_column("Blocks", width=10)
        for row in rows:
            if not verbose and row.status is ReadinessStatus.NOT_TESTED:
                continue
            table.add_row(
                f"[{STATUS_STYLE[row.status]}]{row.status.value}[/]",
                row.criterion_id.value,
                row.detail,
                _blocking_label(row),
            )
        console.print(table)


def _blocking_label(criterion: ReadinessCriterion) -> str:
    """Which levels this criterion holds shut, abbreviated."""
    if not criterion.blocking_for:
        return "[dim]advisory[/dim]"
    marks = []
    if ReadinessLevel.READY_FOR_PAPER in criterion.blocking_for:
        marks.append("paper")
    if ReadinessLevel.READY_FOR_LIVE_REVIEW in criterion.blocking_for:
        marks.append("live")
    return "+".join(marks)


def _render_blockers(console: Console, assessment: ReadinessAssessment) -> None:
    """What is holding the next level shut, and nothing else.

    The *next* level rather than every level: a reader who is not paper-ready
    is not helped by also being told what a live review would additionally
    need, and burying the four things that matter under forty that do not is
    how a report stops being read.
    """
    target = (
        ReadinessLevel.READY_FOR_LIVE_REVIEW
        if assessment.is_paper_ready
        else ReadinessLevel.READY_FOR_PAPER
    )
    blockers = assessment.blocking(target)
    if not blockers:
        return

    table = Table(
        title=f"Blocking {target.value}",
        show_lines=False,
        expand=True,
        border_style="red",
    )
    table.add_column("Criterion", width=36)
    table.add_column("Status", width=11)
    table.add_column("Reason", width=34)
    table.add_column("Evidence", overflow="fold")
    for criterion in blockers:
        table.add_row(
            criterion.criterion_id.value,
            f"[{STATUS_STYLE[criterion.status]}]{criterion.status.value}[/]",
            criterion.reason_code.value,
            criterion.evidence_id or "[dim]none collected[/dim]",
        )
    console.print(table)


def render_signoff(console: Console, signoff: LiveReadinessSignoff | None) -> None:
    """The live-readiness sign-off, or its absence."""
    if signoff is None:
        console.print(
            Panel(
                "No live-readiness sign-off has been recorded.\n"
                "Live trading remains off regardless; a sign-off records a human decision "
                "and enables nothing on its own.",
                title="[yellow]NOT_SIGNED[/yellow]",
                border_style="yellow",
            )
        )
        return

    style = {
        SignoffStatus.SIGNED: "green",
        SignoffStatus.NOT_SIGNED: "yellow",
        SignoffStatus.REVOKED: "red",
    }[signoff.status]

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Sign-off", signoff.signoff_id)
    table.add_row("Readiness run", signoff.readiness_run_id)
    table.add_row("Level reviewed", signoff.readiness_level.value)
    table.add_row("Signed by", signoff.signed_by)
    table.add_row("Signed at", signoff.signed_at.isoformat())
    table.add_row("Git revision", signoff.git_revision or "-")
    table.add_row("Working tree", _tree_state(signoff.working_tree_clean))
    if signoff.note:
        table.add_row("Note", signoff.note)
    table.add_row("Enables trading", "[bold]NO[/bold]")

    console.print(
        Panel(
            table,
            title=f"[{style}]{signoff.status.value}[/{style}]",
            subtitle=(
                "A sign-off records that a human reviewed the evidence. It does not set "
                "TRADING_MODE, LIVE_TRADING_CONFIRMED, LIVE_READINESS_CHECKLIST_SIGNED_OFF, "
                "execution.enabled or IBKR_READ_ONLY."
            ),
            border_style=style,
        )
    )
