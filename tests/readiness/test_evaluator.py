"""The evaluator is deterministic, and it never certifies what it did not see."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from tests.readiness.conftest import NOW, OTHER_REVISION
from trading_system.domain.enums import (
    ReadinessCriterionId,
    ReadinessLevel,
    ReadinessReasonCode,
    ReadinessStatus,
    TradingMode,
)
from trading_system.readiness.criteria import READINESS_CRITERIA
from trading_system.readiness.evaluator import evaluate, explain_level
from trading_system.readiness.evidence import EvidenceBundle
from trading_system.readiness.models import ReadinessAssessment, ReadinessCriterion
from trading_system.readiness.policy import ReadinessPolicy

pytestmark = pytest.mark.unit


def _status(assessment: ReadinessAssessment, criterion_id: ReadinessCriterionId) -> ReadinessStatus:
    return _criterion(assessment, criterion_id).status


def _criterion(
    assessment: ReadinessAssessment, criterion_id: ReadinessCriterionId
) -> ReadinessCriterion:
    return next(c for c in assessment.criteria if c.criterion_id is criterion_id)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_evaluating_twice_produces_an_identical_record(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """Same evidence, same policy, byte-identical assessment — id included.

    This is what lets a stored assessment be re-derived and checked rather than
    merely trusted, and what makes the immutable store recognise a replay
    instead of refusing a contradictory second copy.
    """
    bundle = make_bundle(passing_evidence)
    first = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    second = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.assessment_id == second.assessment_id


def test_the_evaluator_reads_no_clock(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """``evaluated_at`` defaults to the bundle's instant, not to ``now``."""
    bundle = make_bundle(passing_evidence)
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    assert assessment.evaluated_at == NOW
    assert assessment.as_of == NOW


def test_every_criterion_is_judged(
    make_bundle: Callable[..., EvidenceBundle], policy: ReadinessPolicy
) -> None:
    assessment = evaluate(make_bundle(), policy, trading_mode=TradingMode.PAPER)
    assert len(assessment.criteria) == len(READINESS_CRITERIA)


# ---------------------------------------------------------------------------
# Absence of evidence
# ---------------------------------------------------------------------------
def test_an_empty_bundle_certifies_nothing(
    make_bundle: Callable[..., EvidenceBundle], policy: ReadinessPolicy
) -> None:
    """The cheap default cannot accidentally certify anything."""
    assessment = evaluate(make_bundle(), policy, trading_mode=TradingMode.PAPER)
    assert assessment.level is ReadinessLevel.NOT_READY
    assert assessment.counts["PASS"] == 0
    assert assessment.counts["NOT_TESTED"] == len(READINESS_CRITERIA)


def test_a_missing_slot_is_not_tested_rather_than_passing(
    make_bundle: Callable[..., EvidenceBundle], policy: ReadinessPolicy
) -> None:
    assessment = evaluate(make_bundle(), policy, trading_mode=TradingMode.PAPER)
    criterion = _criterion(assessment, ReadinessCriterionId.TEST_SUITE_PASSES)
    assert criterion.status is ReadinessStatus.NOT_TESTED
    assert criterion.evidence_id is None
    assert "different facts" in criterion.detail


def test_a_deliberately_skipped_slot_says_so(
    policy: ReadinessPolicy,
) -> None:
    """A choice not to look is a different fact from an absent collector."""
    bundle = EvidenceBundle(as_of=NOW, git_revision="abc").with_skip(
        "broker", "the broker probe was not requested for this run"
    )
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    criterion = _criterion(assessment, ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert criterion.status is ReadinessStatus.NOT_TESTED
    assert criterion.reason_code is ReadinessReasonCode.NOT_COLLECTED
    assert "not requested" in criterion.detail


# ---------------------------------------------------------------------------
# Everything present
# ---------------------------------------------------------------------------
def test_complete_passing_evidence_reaches_live_review(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """The positive control the "break exactly one thing" tests rest on."""
    assessment = evaluate(make_bundle(passing_evidence), policy, trading_mode=TradingMode.PAPER)
    unsatisfied = [
        (c.criterion_id.value, c.status.value, c.detail)
        for c in assessment.criteria
        if not c.satisfied
    ]
    assert not unsatisfied, f"expected everything to pass, got: {unsatisfied}"
    assert assessment.level is ReadinessLevel.READY_FOR_LIVE_REVIEW


def test_one_failing_blocking_criterion_holds_the_paper_gate_shut(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    evidence = dict(passing_evidence)
    evidence["test_suite"] = {"exit_code": 1, "failed": 2}
    assessment = evaluate(make_bundle(evidence), policy, trading_mode=TradingMode.PAPER)
    assert assessment.level is ReadinessLevel.NOT_READY
    blocking = {c.criterion_id for c in assessment.blocking(ReadinessLevel.READY_FOR_PAPER)}
    assert blocking == {ReadinessCriterionId.TEST_SUITE_PASSES}


def test_a_live_only_criterion_does_not_hold_paper_shut(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """Brief section 30: paper and live review are different questions."""
    evidence = dict(passing_evidence)
    evidence["git"] = {"git_revision": "abc123", "working_tree_clean": False, "changed_files": 3}
    bundle = make_bundle(evidence, working_tree_clean=False)
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    assert assessment.level is ReadinessLevel.READY_FOR_PAPER
    assert _status(assessment, ReadinessCriterionId.WORKING_TREE_CLEAN) is ReadinessStatus.FAIL


def test_a_dirty_tree_blocks_live_review(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """Brief section 29."""
    evidence = dict(passing_evidence)
    evidence["git"] = {"git_revision": "abc123", "working_tree_clean": False, "changed_files": 3}
    assessment = evaluate(
        make_bundle(evidence, working_tree_clean=False), policy, trading_mode=TradingMode.PAPER
    )
    blocking = {c.criterion_id for c in assessment.blocking(ReadinessLevel.READY_FOR_LIVE_REVIEW)}
    assert ReadinessCriterionId.WORKING_TREE_CLEAN in blocking


# ---------------------------------------------------------------------------
# UNKNOWN never satisfies
# ---------------------------------------------------------------------------
def test_an_unknown_reconciliation_holds_the_gate_shut(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """Brief section 7: UNKNOWN is neither FAILED nor SAFE."""
    evidence = dict(passing_evidence)
    evidence["reconciliation"] = {
        "status": "BROKER_DATA_UNAVAILABLE",
        "critical_findings": 0,
        "unknown_executions": 0,
    }
    assessment = evaluate(make_bundle(evidence), policy, trading_mode=TradingMode.PAPER)
    criterion = _criterion(assessment, ReadinessCriterionId.RECONCILIATION_RUNS)
    assert criterion.status is ReadinessStatus.UNKNOWN
    assert not criterion.satisfied
    assert assessment.level is ReadinessLevel.NOT_READY


# ---------------------------------------------------------------------------
# Freshness is applied before the predicate
# ---------------------------------------------------------------------------
def test_evidence_from_another_revision_is_stale_not_passing(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """A stale pass is the artifact section 29 exists to prevent."""
    evidence = {k: v for k, v in passing_evidence.items() if k != "test_suite"}
    bundle = make_bundle(
        evidence,
        raw={
            "test_suite": make_record(
                source="pytest", detail={"exit_code": 0}, git_revision=OTHER_REVISION
            )
        },
    )
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    criterion = _criterion(assessment, ReadinessCriterionId.TEST_SUITE_PASSES)
    assert criterion.status is ReadinessStatus.STALE
    assert criterion.reason_code is ReadinessReasonCode.EVIDENCE_FROM_OTHER_REVISION
    assert "never examined" in criterion.detail


def test_revision_bound_evidence_with_no_revision_is_stale(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    bundle = make_bundle(
        raw={"test_suite": make_record(source="pytest", detail={"exit_code": 0}, git_revision=None)}
    )
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    assert _status(assessment, ReadinessCriterionId.TEST_SUITE_PASSES) is ReadinessStatus.STALE


def test_time_bound_evidence_outside_its_window_is_stale(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """A gateway that answered an hour ago says nothing about now."""
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                detail={"connected": True, "trading_mode": "PAPER"},
                age=timedelta(hours=3),
            )
        }
    )
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    criterion = _criterion(assessment, ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert criterion.status is ReadinessStatus.STALE
    assert criterion.reason_code is ReadinessReasonCode.EVIDENCE_STALE


def test_time_bound_evidence_inside_its_window_is_judged(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                detail={"connected": True, "trading_mode": "PAPER", "broker": "IBKR"},
                age=timedelta(minutes=2),
            )
        }
    )
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    assert _status(assessment, ReadinessCriterionId.PAPER_BROKER_REACHABLE) is ReadinessStatus.PASS


def test_a_failed_collection_is_judged_rather_than_aged_out(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """ "The gateway refused" beats "this evidence is 40 minutes old"."""
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                collected=False,
                error="connection refused",
                detail={"connected": False},
                age=timedelta(hours=5),
            )
        }
    )
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    criterion = _criterion(assessment, ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert criterion.status is ReadinessStatus.FAIL
    assert criterion.reason_code is ReadinessReasonCode.PAPER_GATEWAY_UNAVAILABLE
    assert "connection refused" in criterion.detail


def test_revision_bound_evidence_records_no_age(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """A number there would invite comparison against a window that does not apply."""
    assessment = evaluate(make_bundle(passing_evidence), policy, trading_mode=TradingMode.PAPER)
    revision_bound = _criterion(assessment, ReadinessCriterionId.TEST_SUITE_PASSES)
    time_bound = _criterion(assessment, ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert revision_bound.evidence_age_seconds is None
    assert time_bound.evidence_age_seconds is not None


# ---------------------------------------------------------------------------
# Provenance travels with the verdict
# ---------------------------------------------------------------------------
def test_every_judged_criterion_names_its_evidence(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    assessment = evaluate(make_bundle(passing_evidence), policy, trading_mode=TradingMode.PAPER)
    for criterion in assessment.criteria:
        if criterion.status is ReadinessStatus.NOT_TESTED:
            continue
        assert criterion.evidence_id
        assert criterion.evidence_source
        assert criterion.observed_at is not None


def test_the_assessment_records_the_evidence_digest(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    bundle = make_bundle(passing_evidence)
    assessment = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    assert assessment.evidence_digest == bundle.digest()
    assert set(assessment.evidence_ids) == set(bundle.evidence_ids)


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------
def test_the_explanation_names_what_is_blocking(
    make_bundle: Callable[..., EvidenceBundle], policy: ReadinessPolicy
) -> None:
    assessment = evaluate(make_bundle(), policy, trading_mode=TradingMode.PAPER)
    text = explain_level(assessment)
    assert "READY_FOR_PAPER" in text
    assert "held shut" in text


def test_the_live_review_explanation_says_trading_stays_off(
    make_bundle: Callable[..., EvidenceBundle],
    policy: ReadinessPolicy,
    passing_evidence: dict[str, dict[str, Any]],
) -> None:
    """Brief section 42: the disclaimer travels with the strongest verdict."""
    assessment = evaluate(make_bundle(passing_evidence), policy, trading_mode=TradingMode.PAPER)
    text = explain_level(assessment)
    assert "Live trading remains off" in text
    assert "no automatic transition" in text
