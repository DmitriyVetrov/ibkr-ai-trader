"""Deterministic evaluation of a research thesis's invalidation conditions.

Milestone 5 produced the thesis; this module **consumes** it. There is no
second research engine here, no agent, no LLM client and no import that reaches
one — a test asserts the whole transitive closure.

The rule that shapes everything below:

    An invalidation condition that cannot be checked against a structured fact
    is ``NOT_EVALUATED``. It is never interpreted.

Research states its invalidation conditions in prose, because that is how a
falsifiable claim about a market is written: *"the guidance cut is confirmed"*,
*"the stock closes below 150 for three sessions"*. Reading such a sentence and
deciding whether it happened is a judgement, and a judgement made by pattern
matching on words is worse than no judgement at all — it is one nobody can
predict, reproduce or review. ``exit.thesis.allow_prose_interpretation: true``
fails to load precisely so this cannot be turned on.

What *is* checkable is checked, against facts this system already stores in
structured form:

``horizon``
    Research stated an expected horizon in days. Past it, the thesis has had
    its time and is recorded as expired — not as invalidated, because a
    forecast whose window closed was not proved wrong.
``catalyst``
    Research named dated events. An event whose date has passed without the
    move happening is a *structured* fact about the calendar, and the
    condition that named it is evaluable.
``direction``
    Where research stated a direction and the position's own economics have
    moved decisively the other way, the market has answered the question the
    thesis asked.

Anything else is labelled and left alone. That is a deliberately small set, and
the honesty is the point: a thesis monitor that claimed to evaluate ten
conditions and actually pattern-matched nine of them would be worse than one
that says it evaluated one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    ExitDecisionType,
    ExitPolicyKind,
    ExitReasonCode,
    ThesisConditionOutcome,
    ThesisStatus,
)
from trading_system.exit.models import ExitPolicyOutcome, ThesisConditionCheck
from trading_system.infrastructure.settings import ExitThesisConfig

__all__ = [
    "ThesisView",
    "check_conditions",
    "evaluate_thesis",
    "thesis_status_of",
]


@dataclass(frozen=True, slots=True)
class ThesisView:
    """The stored thesis, projected to what a deterministic check can use.

    Built by the service from a Milestone 5 report. Deliberately a plain
    dataclass of facts rather than the report itself: the exit engine has no
    business reading evidence, sources or confidence rationale, and a shape
    that cannot carry them cannot be tempted to interpret them.
    """

    #: The conditions research stated, verbatim and in order.
    conditions: tuple[tuple[str, str | None], ...] = ()
    #: When the report was made, and how long it claimed to speak for.
    as_of: datetime | None = None
    horizon_days: int | None = None
    #: Dated events research named, with the instant each is expected.
    catalysts: tuple[tuple[str, datetime | None], ...] = ()
    #: The direction research expected, where it stated one.
    direction: str | None = None
    #: True when no report could be read at all — distinct from a report that
    #: stated no conditions, which is a report saying something.
    unavailable: bool = False
    detail: str | None = None


def check_conditions(
    view: ThesisView,
    *,
    at: datetime,
    return_pct: Decimal | None = None,
    adverse_move_pct: Decimal = Decimal("50"),
) -> list[ThesisConditionCheck]:
    """Check each condition against structured facts, labelling the rest.

    ``adverse_move_pct`` is how far the position's own economics must have gone
    against the stated direction before that counts as the market having
    answered. It is a *large* number by default: a long option that has lost
    half its value has not merely wobbled, and anything tighter would make this
    a second maximum-loss policy under another name.
    """
    checks: list[ThesisConditionCheck] = []
    horizon_expired = _horizon_expired(view, at=at)
    passed_catalysts = _passed_catalysts(view, at=at)
    adverse = _direction_contradicted(view, return_pct=return_pct, threshold=adverse_move_pct)

    for condition, observable in view.conditions:
        outcome = ThesisConditionOutcome.NOT_EVALUATED
        evidence: str | None = None
        detail: str | None = (
            "stated as prose with no structured fact to check it against; labelled rather "
            "than interpreted, because reading a sentence as a sell signal is a judgement "
            "this engine does not make"
        )

        if horizon_expired is not None:
            outcome, evidence, detail = horizon_expired
        elif passed_catalysts is not None and _mentions_event(condition, view):
            outcome, evidence, detail = passed_catalysts
        elif adverse is not None and _mentions_direction(condition, view):
            outcome, evidence, detail = adverse

        checks.append(
            ThesisConditionCheck(
                condition=condition,
                observable=observable,
                outcome=outcome,
                evidence=evidence,
                detail=detail,
            )
        )
    return checks


def _horizon_expired(
    view: ThesisView, *, at: datetime
) -> tuple[ThesisConditionOutcome, str, str] | None:
    """Whether the thesis's own stated window has closed.

    Returns ``HOLDS`` deliberately rather than ``VIOLATED``. A forecast whose
    horizon ran out was not proved wrong; it simply stopped speaking. Treating
    that as an invalidation would exit every position on a schedule that
    duplicates the expiration policy while pretending to be a judgement about
    the market.
    """
    if view.as_of is None or view.horizon_days is None:
        return None
    elapsed = (at - view.as_of).days
    if elapsed <= view.horizon_days:
        return None
    return (
        ThesisConditionOutcome.HOLDS,
        f"research horizon of {view.horizon_days} day(s) from {view.as_of.date().isoformat()}",
        (
            f"{elapsed} day(s) have elapsed, so the thesis is past the window it claimed to "
            f"speak for. That is not an invalidation: a forecast whose horizon closed was "
            f"never proved wrong, and the expiration policy owns the deadline"
        ),
    )


def _passed_catalysts(
    view: ThesisView, *, at: datetime
) -> tuple[ThesisConditionOutcome, str, str] | None:
    """Whether every dated catalyst research named has already happened."""
    dated = [(name, when) for name, when in view.catalysts if when is not None]
    if not dated:
        return None
    past = [(name, when) for name, when in dated if when is not None and when <= at]
    if len(past) != len(dated):
        return None
    names = ", ".join(f"{name} ({when.date().isoformat()})" for name, when in past if when)
    return (
        ThesisConditionOutcome.VIOLATED,
        f"every catalyst research named has passed: {names}",
        (
            "the thesis rested on a dated event, the date has passed, and the position is "
            "still held. The catalyst can no longer produce the move it was expected to"
        ),
    )


def _direction_contradicted(
    view: ThesisView, *, return_pct: Decimal | None, threshold: Decimal
) -> tuple[ThesisConditionOutcome, str, str] | None:
    """Whether the position's own economics have decisively contradicted the view."""
    if view.direction is None or return_pct is None:
        return None
    if return_pct > -threshold:
        return None
    return (
        ThesisConditionOutcome.VIOLATED,
        f"position return {return_pct:.2f}% against a stated {view.direction} view",
        (
            f"the structure has lost more than {threshold}% of what was paid for it while "
            f"research expected {view.direction}. The market has answered the question the "
            f"thesis asked"
        ),
    )


def _mentions_event(condition: str, view: ThesisView) -> bool:
    """Whether a condition names one of the catalysts research itself recorded.

    Matching is against *research's own structured catalyst names*, never
    against a vocabulary of trading words invented here. That is the difference
    between reading a stored fact and interpreting prose: the name came from
    the report, and a report that named no catalyst matches nothing.
    """
    text = condition.casefold()
    return any(name.casefold() in text for name, _ in view.catalysts if name)


def _mentions_direction(condition: str, view: ThesisView) -> bool:
    """Whether a condition names the direction research itself recorded."""
    if not view.direction:
        return False
    return view.direction.casefold() in condition.casefold()


def thesis_status_of(checks: Sequence[ThesisConditionCheck]) -> ThesisStatus:
    """Collapse the individual checks onto the Milestone 1 vocabulary.

    ``UNKNOWN`` when nothing could be evaluated, which is the honest answer and
    the common one. Note what is absent: nothing here ever returns
    ``WEAKENING``. Deciding that a thesis has weakened without being falsified
    is a judgement, and this engine makes none — the specification's thesis
    monitor is where that verdict belongs, and it is not this milestone.
    """
    if not checks:
        return ThesisStatus.UNKNOWN
    if any(check.outcome is ThesisConditionOutcome.VIOLATED for check in checks):
        return ThesisStatus.INVALIDATED
    if all(check.outcome is ThesisConditionOutcome.NOT_EVALUATED for check in checks):
        return ThesisStatus.UNKNOWN
    return ThesisStatus.VALID


def evaluate_thesis(
    view: ThesisView,
    checks: Sequence[ThesisConditionCheck],
    *,
    config: ExitThesisConfig,
) -> ExitPolicyOutcome:
    """Turn the checks into a verdict. Pure."""
    if not config.enabled:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.THESIS,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            summary="thesis invalidation is switched off in configuration",
            evaluated=False,
        )

    if view.unavailable:
        if config.block_on_unavailable_thesis:
            return ExitPolicyOutcome(
                policy=ExitPolicyKind.THESIS,
                decision=ExitDecisionType.BLOCK,
                reason_code=ExitReasonCode.THESIS_DATA_UNAVAILABLE,
                summary="the research report this position rests on could not be read",
                detail=(
                    f"{view.detail or 'no detail supplied'}. 'We could not look' and 'the "
                    f"thesis holds' are different facts, and only one of them is a statement "
                    f"about the market"
                ),
                evaluated=False,
            )
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.THESIS,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            summary="no research report available; the thesis policy was not evaluated",
            detail=view.detail,
            evaluated=False,
        )

    status = thesis_status_of(checks)
    violated = [check for check in checks if check.outcome is ThesisConditionOutcome.VIOLATED]
    evaluated = sum(
        1 for check in checks if check.outcome is not ThesisConditionOutcome.NOT_EVALUATED
    )

    if status is ThesisStatus.INVALIDATED and config.exit_on_invalidated:
        first = violated[0]
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.THESIS,
            decision=ExitDecisionType.EXIT,
            reason_code=ExitReasonCode.THESIS_INVALIDATED,
            measured=str(len(violated)),
            threshold="1",
            summary=f"the research thesis is invalidated: {first.condition}",
            detail=f"{first.evidence}. {first.detail or ''}".strip(),
        )

    if status is ThesisStatus.UNKNOWN:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.THESIS,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            measured="0",
            threshold=str(len(checks)),
            summary=(
                f"none of the {len(checks)} stated invalidation condition(s) can be checked "
                f"deterministically"
            ),
            detail=(
                "labelled NOT_EVALUATED rather than passed, exactly as an untested risk limit "
                "is. Prose is not interpreted here, and a thesis monitor that claimed to have "
                "checked these would be claiming a judgement nobody made"
            ),
            evaluated=False,
        )

    return ExitPolicyOutcome(
        policy=ExitPolicyKind.THESIS,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.THESIS_INTACT,
        measured=str(evaluated),
        threshold=str(len(checks)),
        summary=(
            f"{evaluated} of {len(checks)} invalidation condition(s) were checkable and none "
            f"is violated"
        ),
    )
