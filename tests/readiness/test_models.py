"""The readiness artifacts refuse to record a claim they cannot support.

Every test here is about a *model validator*, which is where this milestone's
safety claims stop being prose. If a shape cannot be constructed, it cannot be
written, so it cannot be read back and believed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trading_system.domain.enums import (
    READINESS_INCONCLUSIVE_STATUSES,
    READINESS_SATISFYING_STATUSES,
    ReadinessCriterionId,
    ReadinessDomain,
    ReadinessLevel,
    ReadinessReasonCode,
    ReadinessRunStatus,
    ReadinessStatus,
    SignoffStatus,
    TradingMode,
)
from trading_system.readiness.models import (
    IDENTITY_NOT_AVAILABLE,
    LiveReadinessSignoff,
    ReadinessAssessment,
    ReadinessCriterion,
    ReadinessRun,
    assessment_identifier,
    run_identifier,
    signoff_identifier,
)

pytestmark = pytest.mark.unit


def _criterion(**overrides: object) -> ReadinessCriterion:
    payload: dict[str, object] = {
        "criterion_id": ReadinessCriterionId.TEST_SUITE_PASSES,
        "domain": ReadinessDomain.SOFTWARE_QUALITY,
        "title": "the suite passes",
        "status": ReadinessStatus.PASS,
        "reason_code": ReadinessReasonCode.SATISFIED,
        "detail": "pytest exited 0",
        "evidence_id": "evidence-1",
    }
    payload.update(overrides)
    return ReadinessCriterion(**payload)


# ---------------------------------------------------------------------------
# Every verdict has evidence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status",
    [ReadinessStatus.PASS, ReadinessStatus.FAIL, ReadinessStatus.UNKNOWN, ReadinessStatus.STALE],
)
def test_a_verdict_cannot_be_recorded_without_evidence(status: ReadinessStatus) -> None:
    """Brief section 27, as a type rather than as a discipline."""
    with pytest.raises(ValidationError, match="no evidence"):
        _criterion(
            status=status,
            evidence_id=None,
            reason_code=(
                ReadinessReasonCode.SATISFIED
                if status is ReadinessStatus.PASS
                else ReadinessReasonCode.NO_EVIDENCE
            ),
        )


def test_not_tested_cannot_carry_evidence() -> None:
    """The converse: evidence means a verdict was reached, even an UNKNOWN one."""
    with pytest.raises(ValidationError, match="NOT_TESTED"):
        _criterion(
            status=ReadinessStatus.NOT_TESTED,
            reason_code=ReadinessReasonCode.NO_EVIDENCE,
            evidence_id="evidence-1",
        )


def test_not_tested_without_evidence_is_the_ordinary_shape() -> None:
    criterion = _criterion(
        status=ReadinessStatus.NOT_TESTED,
        reason_code=ReadinessReasonCode.NO_EVIDENCE,
        evidence_id=None,
    )
    assert not criterion.satisfied
    assert criterion.inconclusive


def test_only_a_pass_may_read_satisfied() -> None:
    with pytest.raises(ValidationError, match="reasoned SATISFIED"):
        _criterion(status=ReadinessStatus.FAIL, reason_code=ReadinessReasonCode.SATISFIED)


def test_a_pass_may_not_carry_a_caveat() -> None:
    with pytest.raises(ValidationError, match="reasoned"):
        _criterion(status=ReadinessStatus.PASS, reason_code=ReadinessReasonCode.EVIDENCE_STALE)


# ---------------------------------------------------------------------------
# Only PASS satisfies
# ---------------------------------------------------------------------------
def test_exactly_one_status_satisfies_a_criterion() -> None:
    """``UNKNOWN``, ``STALE`` and ``NOT_TESTED`` are outside the satisfying set.

    Brief section 7. Asserted against the frozenset rather than against a
    chain of comparisons, because the set is what every call site reads.
    """
    assert {ReadinessStatus.PASS} == READINESS_SATISFYING_STATUSES
    assert ReadinessStatus.UNKNOWN in READINESS_INCONCLUSIVE_STATUSES
    assert ReadinessStatus.STALE in READINESS_INCONCLUSIVE_STATUSES
    assert ReadinessStatus.NOT_TESTED in READINESS_INCONCLUSIVE_STATUSES
    assert ReadinessStatus.PASS not in READINESS_INCONCLUSIVE_STATUSES
    assert ReadinessStatus.FAIL not in READINESS_INCONCLUSIVE_STATUSES


@pytest.mark.parametrize(
    "status",
    [ReadinessStatus.FAIL, ReadinessStatus.UNKNOWN, ReadinessStatus.STALE],
)
def test_an_unsatisfied_criterion_blocks_its_levels(status: ReadinessStatus) -> None:
    criterion = _criterion(
        status=status,
        reason_code=ReadinessReasonCode.NO_EVIDENCE,
        blocking_for=(ReadinessLevel.READY_FOR_PAPER,),
    )
    assert criterion.blocks(ReadinessLevel.READY_FOR_PAPER)
    assert not criterion.blocks(ReadinessLevel.READY_FOR_LIVE_REVIEW)


# ---------------------------------------------------------------------------
# The level is derived, never asserted
# ---------------------------------------------------------------------------
def _assessment(criteria: tuple[ReadinessCriterion, ...], **overrides: object):
    level = overrides.pop("level", None) or ReadinessAssessment.derive_level(criteria)
    payload: dict[str, object] = {
        "assessment_id": "readiness-test",
        "as_of": datetime(2026, 8, 15, tzinfo=UTC),
        "evaluated_at": datetime(2026, 8, 15, tzinfo=UTC),
        "trading_mode": TradingMode.PAPER,
        "level": level,
        "criteria": criteria,
        "evidence_digest": "digest",
    }
    payload.update(overrides)
    return ReadinessAssessment(**payload)


def test_a_level_that_contradicts_its_criteria_cannot_be_constructed() -> None:
    """The central validator: an assessment cannot lie about what it found."""
    blocked = _criterion(
        status=ReadinessStatus.FAIL,
        reason_code=ReadinessReasonCode.TESTS_FAILED,
        blocking_for=(ReadinessLevel.READY_FOR_PAPER, ReadinessLevel.READY_FOR_LIVE_REVIEW),
    )
    with pytest.raises(ValidationError, match="derive"):
        _assessment((blocked,), level=ReadinessLevel.READY_FOR_PAPER)


def test_all_satisfied_derives_the_strongest_level() -> None:
    criteria = (
        _criterion(
            blocking_for=(ReadinessLevel.READY_FOR_PAPER, ReadinessLevel.READY_FOR_LIVE_REVIEW)
        ),
    )
    assert ReadinessAssessment.derive_level(criteria) is ReadinessLevel.READY_FOR_LIVE_REVIEW


def test_a_live_only_blocker_still_permits_paper() -> None:
    """The two gates are genuinely different questions (brief section 30)."""
    criteria = (
        _criterion(criterion_id=ReadinessCriterionId.TEST_SUITE_PASSES),
        _criterion(
            criterion_id=ReadinessCriterionId.WORKING_TREE_CLEAN,
            domain=ReadinessDomain.SOURCE_CONTROL,
            status=ReadinessStatus.FAIL,
            reason_code=ReadinessReasonCode.WORKING_TREE_DIRTY,
            blocking_for=(ReadinessLevel.READY_FOR_LIVE_REVIEW,),
        ),
    )
    assessment = _assessment(criteria)
    assert assessment.level is ReadinessLevel.READY_FOR_PAPER
    assert assessment.is_paper_ready
    assert not assessment.is_live_review_ready


def test_an_unknown_blocking_criterion_holds_the_gate_shut() -> None:
    """Brief section 7: UNKNOWN never satisfies a blocking criterion."""
    criteria = (
        _criterion(
            status=ReadinessStatus.UNKNOWN,
            reason_code=ReadinessReasonCode.RECONCILIATION_UNKNOWN,
            blocking_for=(ReadinessLevel.READY_FOR_PAPER, ReadinessLevel.READY_FOR_LIVE_REVIEW),
        ),
    )
    assert ReadinessAssessment.derive_level(criteria) is ReadinessLevel.NOT_READY


def test_an_advisory_criterion_never_holds_a_gate_shut() -> None:
    criteria = (
        _criterion(
            status=ReadinessStatus.FAIL,
            reason_code=ReadinessReasonCode.NO_EVIDENCE,
            blocking_for=(),
        ),
    )
    assert ReadinessAssessment.derive_level(criteria) is ReadinessLevel.READY_FOR_LIVE_REVIEW


def test_one_criterion_cannot_appear_twice() -> None:
    duplicate = (
        _criterion(),
        _criterion(status=ReadinessStatus.FAIL, reason_code=ReadinessReasonCode.TESTS_FAILED),
    )
    with pytest.raises(ValidationError, match="more than once"):
        _assessment(duplicate)


def test_counts_report_every_status() -> None:
    assessment = _assessment((_criterion(),))
    assert assessment.counts["PASS"] == 1
    assert set(assessment.counts) == {status.value for status in ReadinessStatus}


# ---------------------------------------------------------------------------
# There is no READY_FOR_LIVE
# ---------------------------------------------------------------------------
def test_the_vocabulary_has_no_ready_for_live() -> None:
    """Brief sections 5 and 42.

    A level named ``READY_FOR_LIVE`` would eventually be read as the
    authorisation itself, and the authorisation is a human control.
    """
    values = {level.value for level in ReadinessLevel}
    assert values == {"NOT_READY", "READY_FOR_PAPER", "READY_FOR_LIVE_REVIEW"}
    assert "READY_FOR_LIVE" not in values


# ---------------------------------------------------------------------------
# A run never trades
# ---------------------------------------------------------------------------
def _run(**overrides: object) -> ReadinessRun:
    payload: dict[str, object] = {
        "readiness_run_id": "readiness-run-test",
        "status": ReadinessRunStatus.NO_EVIDENCE,
        "evaluated_at": datetime(2026, 8, 15, tzinfo=UTC),
        "as_of": datetime(2026, 8, 15, tzinfo=UTC),
        "trading_mode": TradingMode.PAPER,
    }
    payload.update(overrides)
    return ReadinessRun(**payload)


def test_a_readiness_run_cannot_record_a_submitted_order() -> None:
    with pytest.raises(ValidationError, match="submitted order"):
        _run(orders_submitted=1)


def test_a_completed_run_must_carry_its_assessment() -> None:
    with pytest.raises(ValidationError, match="carries no assessment"):
        _run(status=ReadinessRunStatus.COMPLETE)


def test_a_failed_run_cannot_carry_an_assessment() -> None:
    assessment = _assessment((_criterion(),))
    with pytest.raises(ValidationError, match="carries an assessment"):
        _run(status=ReadinessRunStatus.CONFIGURATION_ERROR, assessment=assessment)


def test_a_run_with_no_assessment_is_not_ready() -> None:
    """Absence of a verdict is never a favourable verdict."""
    assert _run().level is ReadinessLevel.NOT_READY


# ---------------------------------------------------------------------------
# A sign-off enables nothing
# ---------------------------------------------------------------------------
def _signoff(**overrides: object) -> LiveReadinessSignoff:
    payload: dict[str, object] = {
        "signoff_id": "signoff-test",
        "status": SignoffStatus.SIGNED,
        "readiness_run_id": "readiness-run-test",
        "readiness_level": ReadinessLevel.READY_FOR_LIVE_REVIEW,
        "signed_by": "A Person",
        "signed_at": datetime(2026, 8, 15, tzinfo=UTC),
    }
    payload.update(overrides)
    return LiveReadinessSignoff(**payload)


def test_a_signoff_cannot_claim_to_enable_trading() -> None:
    with pytest.raises(ValidationError, match="cannot enable trading"):
        _signoff(enables_trading=True)


def test_a_signed_record_must_name_a_person() -> None:
    with pytest.raises(ValidationError, match="must name who signed"):
        _signoff(signed_by=IDENTITY_NOT_AVAILABLE)


def test_a_signed_record_refuses_an_empty_signer() -> None:
    with pytest.raises(ValidationError, match="must name who signed"):
        _signoff(signed_by="   ")


def test_a_signoff_records_enables_trading_false() -> None:
    assert _signoff().enables_trading is False


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_an_assessment_id_changes_with_the_conclusion() -> None:
    """The lesson allocation, execution and P&L each learned separately.

    Two runs over the same evidence that reach *different verdicts* are
    different facts. An id derived from the inputs alone would collide them and
    the immutable store would refuse the second.
    """
    common = {
        "git_revision": "abc",
        "as_of": datetime(2026, 8, 15, tzinfo=UTC),
        "evidence_digest": "same-evidence",
        "criteria_digest": "same-criteria",
    }
    paper = assessment_identifier(level="READY_FOR_PAPER", **common)  # type: ignore[arg-type]
    not_ready = assessment_identifier(level="NOT_READY", **common)  # type: ignore[arg-type]
    assert paper != not_ready


def test_an_assessment_id_is_stable_for_identical_input() -> None:
    common = {
        "git_revision": "abc",
        "as_of": datetime(2026, 8, 15, tzinfo=UTC),
        "evidence_digest": "e",
        "criteria_digest": "c",
        "level": "NOT_READY",
    }
    assert assessment_identifier(**common) == assessment_identifier(**common)  # type: ignore[arg-type]


def test_a_run_id_changes_with_its_status() -> None:
    instant = datetime(2026, 8, 15, tzinfo=UTC)
    complete = run_identifier(assessment_id="a", as_of=instant, status="COMPLETE")
    dry = run_identifier(assessment_id="a", as_of=instant, status="DRY_RUN")
    assert complete != dry


def test_signing_the_same_run_twice_produces_two_records() -> None:
    """Two decisions by a person, not one decision observed twice."""
    first = signoff_identifier(
        readiness_run_id="r",
        git_revision="abc",
        signed_by="P",
        signed_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
        status="SIGNED",
    )
    second = signoff_identifier(
        readiness_run_id="r",
        git_revision="abc",
        signed_by="P",
        signed_at=datetime(2026, 8, 15, 11, tzinfo=UTC),
        status="SIGNED",
    )
    assert first != second


def test_a_revocation_does_not_collide_with_the_signing_it_reverses() -> None:
    instant = datetime(2026, 8, 15, 10, tzinfo=UTC)
    common = {
        "readiness_run_id": "r",
        "git_revision": "abc",
        "signed_by": "P",
        "signed_at": instant,
    }
    signed = signoff_identifier(status="SIGNED", **common)  # type: ignore[arg-type]
    revoked = signoff_identifier(status="REVOKED", **common)  # type: ignore[arg-type]
    assert signed != revoked


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _criterion(observed_at=datetime(2026, 8, 15, 12, 0))
