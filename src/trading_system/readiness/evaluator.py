"""The deterministic readiness evaluator (Milestone 12).

A **pure function** of captured evidence and resolved policy. No broker, no
LLM, no Docker client, no socket, no repository, no clock — the instant comes
from the bundle's ``as_of``, so an assessment recomputed tomorrow from the same
stored evidence reaches the same verdict, byte for byte.
``tests/readiness/test_boundaries.py`` walks the transitive import graph to
keep it that way.

.. code-block:: text

    for each criterion in the catalogue:
        record = bundle.get(criterion.slot)
        no record?          -> NOT_TESTED       (never PASS)
        record not collected? -> the predicate decides, and cannot PASS
        revision mismatch?  -> STALE            (evidence is another commit's)
        outside its window? -> STALE
        otherwise           -> predicate(record)

    level = the highest level no unsatisfied blocking criterion holds shut

The order of those checks is the interesting part, and it is the same
reasoning Milestone 10 applies to exit precedence. **Freshness is checked
before the predicate.** Evidence from a different revision does not get to say
``PASS`` and then be marked stale afterwards — a stale pass is exactly the
artifact section 29 exists to prevent, because it reads as a green result for
code that was never tested.

One deliberate exception: evidence that *failed to collect* is judged by the
predicate first. A broker that could not be reached is a ``FAIL`` with a reason
naming the gateway, which is more useful than ``STALE``, and it cannot pass by
that route because no predicate returns ``PASS`` for a record whose
``collected`` is false.
"""

from __future__ import annotations

from datetime import datetime

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    ReadinessLevel,
    ReadinessReasonCode,
    ReadinessStatus,
    TradingMode,
)
from trading_system.readiness.criteria import READINESS_CRITERIA, CriterionDefinition, Verdict
from trading_system.readiness.evidence import EvidenceBundle, EvidenceRecord
from trading_system.readiness.models import (
    ReadinessAssessment,
    ReadinessCriterion,
    assessment_identifier,
)
from trading_system.readiness.policy import ReadinessPolicy

__all__ = ["evaluate", "evaluate_criterion"]


def evaluate_criterion(
    definition: CriterionDefinition,
    bundle: EvidenceBundle,
    policy: ReadinessPolicy,
) -> ReadinessCriterion:
    """Judge one criterion against the bundle. Pure."""
    record = bundle.get(definition.slot)
    blocking = policy.blocking_levels(definition.criterion_id)

    if record is None:
        skip = bundle.skip_reason(definition.slot)
        detail = (
            f"not collected: {skip}"
            if skip
            else (
                f"no evidence was collected in slot '{definition.slot}'. This is reported as "
                f"NOT_TESTED rather than as a pass: 'we never looked' and 'we looked and it "
                f"was fine' are different facts about a system"
            )
        )
        return ReadinessCriterion(
            criterion_id=definition.criterion_id,
            domain=definition.domain,
            title=definition.title,
            status=ReadinessStatus.NOT_TESTED,
            reason_code=(
                ReadinessReasonCode.NOT_COLLECTED if skip else ReadinessReasonCode.NO_EVIDENCE
            ),
            detail=detail,
            blocking_for=blocking,
        )

    verdict = _verdict(definition, record, bundle, policy)
    age = _age_seconds(definition, record, bundle, policy)

    return ReadinessCriterion(
        criterion_id=definition.criterion_id,
        domain=definition.domain,
        title=definition.title,
        status=verdict.status,
        reason_code=verdict.reason,
        detail=verdict.detail,
        blocking_for=blocking,
        evidence_id=record.evidence_id,
        evidence_kind=record.kind.value,
        evidence_source=record.source,
        observed_at=record.observed_at,
        evidence_age_seconds=age,
        artifact_ids=record.artifact_ids,
    )


def _verdict(
    definition: CriterionDefinition,
    record: EvidenceRecord,
    bundle: EvidenceBundle,
    policy: ReadinessPolicy,
) -> Verdict:
    """Freshness first, then the predicate — except for a failed collection.

    A record that never collected is handed straight to the predicate so the
    reader gets "the gateway refused the connection" instead of "this evidence
    is 40 minutes old", which is true and useless. It cannot pass that way:
    every predicate treats an uncollected record as unsatisfied.
    """
    if not record.collected:
        return definition.predicate(record)

    if policy.is_revision_bound(definition.criterion_id):
        if record.git_revision is None:
            return Verdict(
                ReadinessStatus.STALE,
                ReadinessReasonCode.EVIDENCE_FROM_OTHER_REVISION,
                "this evidence is bound to a git revision and records none, so it cannot be "
                "attributed to the code being assessed",
            )
        if bundle.git_revision is not None and record.git_revision != bundle.git_revision:
            return Verdict(
                ReadinessStatus.STALE,
                ReadinessReasonCode.EVIDENCE_FROM_OTHER_REVISION,
                f"this evidence was gathered at {record.git_revision[:12]} and the assessment "
                f"describes {bundle.git_revision[:12]}. Claiming it for this revision would "
                f"report a result for code that was never examined",
            )
        return definition.predicate(record)

    window = policy.window_seconds(definition.window)
    if window is not None:
        age = record.age_seconds(bundle.as_of)
        if age > window:
            return Verdict(
                ReadinessStatus.STALE,
                ReadinessReasonCode.EVIDENCE_STALE,
                f"this evidence is {age:.0f}s old and its freshness window is {window:.0f}s. "
                f"The system may well be fine; this run did not establish it",
            )

    return definition.predicate(record)


def _age_seconds(
    definition: CriterionDefinition,
    record: EvidenceRecord,
    bundle: EvidenceBundle,
    policy: ReadinessPolicy,
) -> float | None:
    """Evidence age, or ``None`` for revision-bound evidence.

    Revision-bound evidence records no age deliberately. A number there would
    invite somebody to compare it against a window that does not apply, and a
    three-day-old test result at an unchanged revision is perfectly good
    evidence.
    """
    if policy.is_revision_bound(definition.criterion_id):
        return None
    return record.age_seconds(bundle.as_of)


def evaluate(
    bundle: EvidenceBundle,
    policy: ReadinessPolicy,
    *,
    trading_mode: TradingMode,
    system_version: str | None = None,
    config_version: str | None = None,
    evaluated_at: datetime | None = None,
) -> ReadinessAssessment:
    """Judge every criterion and derive the readiness level. Pure.

    ``evaluated_at`` defaults to the bundle's ``as_of`` rather than to a clock
    read. That is what makes the function reproducible: a stored assessment
    re-evaluated from its stored evidence produces an identical record,
    including its content-derived id, so the immutable store recognises a
    replay instead of refusing a contradictory second copy.
    """
    instant = evaluated_at or bundle.as_of
    criteria = tuple(
        evaluate_criterion(definition, bundle, policy) for definition in READINESS_CRITERIA
    )
    level = ReadinessAssessment.derive_level(criteria)

    criteria_digest = stable_hash(
        [
            [
                criterion.criterion_id.value,
                criterion.status.value,
                criterion.reason_code.value,
                criterion.evidence_id,
            ]
            for criterion in criteria
        ]
    )
    evidence_digest = bundle.digest()

    return ReadinessAssessment(
        assessment_id=assessment_identifier(
            git_revision=bundle.git_revision,
            as_of=bundle.as_of,
            evidence_digest=evidence_digest,
            level=level.value,
            criteria_digest=criteria_digest,
        ),
        as_of=bundle.as_of,
        evaluated_at=instant,
        trading_mode=trading_mode,
        git_revision=bundle.git_revision,
        working_tree_clean=bundle.working_tree_clean,
        system_version=system_version,
        config_version=config_version,
        level=level,
        criteria=criteria,
        evidence_digest=evidence_digest,
        evidence_ids=bundle.evidence_ids,
    )


def explain_level(assessment: ReadinessAssessment) -> str:
    """One sentence saying why the assessment reached the level it did.

    Deliberately names the *first* few blockers rather than all of them: a
    reader who is told forty things are wrong reads none of them, and the full
    list is a command away.
    """
    if assessment.level is ReadinessLevel.READY_FOR_LIVE_REVIEW:
        return (
            "every machine-checkable prerequisite is satisfied. Live trading remains off: a "
            "human must review and sign, and the existing live guards must be set "
            "deliberately. There is no automatic transition from here."
        )
    target = (
        ReadinessLevel.READY_FOR_LIVE_REVIEW
        if assessment.is_paper_ready
        else ReadinessLevel.READY_FOR_PAPER
    )
    blockers = assessment.blocking(target)
    if not blockers:  # pragma: no cover - defensive; the validator forbids it
        return f"{assessment.level.value} with no blocking criterion recorded"
    named = ", ".join(criterion.criterion_id.value for criterion in blockers[:3])
    remainder = len(blockers) - 3
    suffix = f" and {remainder} more" if remainder > 0 else ""
    return f"{target.value} is held shut by {named}{suffix}"
