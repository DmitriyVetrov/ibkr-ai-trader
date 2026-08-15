"""Evidence goes stale in two different ways, and neither is the other.

Most evidence expires with the **clock**: a broker probe from an hour ago says
nothing about now. Some expires with the **working tree**: a test result
belongs to the commit it ran against, and no elapsed time makes it wrong while
the code is unchanged — nor does any freshness make it right once the code has
moved.

One mechanism cannot express both, which is why there are two.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from tests.readiness.conftest import NOW, OTHER_REVISION, REVISION
from trading_system.domain.enums import (
    ReadinessCriterionId,
    ReadinessReasonCode,
    ReadinessStatus,
    TradingMode,
)
from trading_system.readiness.criteria import criterion
from trading_system.readiness.evaluator import evaluate, evaluate_criterion
from trading_system.readiness.evidence import EvidenceBundle
from trading_system.readiness.policy import ReadinessPolicy

pytestmark = pytest.mark.unit


def _judge(bundle: EvidenceBundle, policy: ReadinessPolicy, which: ReadinessCriterionId):
    return evaluate_criterion(criterion(which), bundle, policy)


# ---------------------------------------------------------------------------
# Time-bound evidence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=0), ReadinessStatus.PASS),
        (timedelta(minutes=14), ReadinessStatus.PASS),
        (timedelta(minutes=16), ReadinessStatus.STALE),
        (timedelta(days=1), ReadinessStatus.STALE),
    ],
)
def test_a_broker_probe_expires_at_its_window(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
    age: timedelta,
    expected: ReadinessStatus,
) -> None:
    """The shipped window is 900 seconds. The boundary is asserted either side."""
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                detail={"connected": True, "trading_mode": "PAPER", "broker": "IBKR"},
                age=age,
            )
        }
    )
    assert _judge(bundle, policy, ReadinessCriterionId.PAPER_BROKER_REACHABLE).status is expected


def test_a_stale_criterion_says_how_old_and_how_old_is_allowed(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """ "Stale" is not actionable; the two numbers are."""
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                detail={"connected": True, "trading_mode": "PAPER"},
                age=timedelta(hours=2),
            )
        }
    )
    result = _judge(bundle, policy, ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert result.status is ReadinessStatus.STALE
    assert "7200s" in result.detail
    assert "900s" in result.detail


def test_stale_is_not_a_failure(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """The system may well be fine; this run did not establish it."""
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                detail={"connected": True, "trading_mode": "PAPER"},
                age=timedelta(hours=2),
            )
        }
    )
    result = _judge(bundle, policy, ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert result.status is not ReadinessStatus.FAIL
    assert result.reason_code is ReadinessReasonCode.EVIDENCE_STALE
    assert "may well be fine" in result.detail


def test_a_stale_criterion_never_satisfies_a_gate(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                detail={"connected": True, "trading_mode": "PAPER"},
                age=timedelta(days=2),
            )
        }
    )
    result = _judge(bundle, policy, ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert not result.satisfied


# ---------------------------------------------------------------------------
# Revision-bound evidence
# ---------------------------------------------------------------------------
def test_a_test_result_does_not_age_with_the_clock(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """Three days old at an unchanged revision is perfectly good evidence."""
    bundle = make_bundle(
        raw={
            "test_suite": make_record(
                source="pytest",
                detail={"exit_code": 0},
                git_revision=REVISION,
                age=timedelta(days=3),
            )
        }
    )
    assert _judge(bundle, policy, ReadinessCriterionId.TEST_SUITE_PASSES).status is (
        ReadinessStatus.PASS
    )


def test_a_test_result_from_another_revision_is_stale_however_fresh(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """Brief section 29: never claim "tests passed" for code they never ran against."""
    bundle = make_bundle(
        raw={
            "test_suite": make_record(
                source="pytest",
                detail={"exit_code": 0},
                git_revision=OTHER_REVISION,
                age=timedelta(seconds=0),
            )
        }
    )
    result = _judge(bundle, policy, ReadinessCriterionId.TEST_SUITE_PASSES)
    assert result.status is ReadinessStatus.STALE
    assert result.reason_code is ReadinessReasonCode.EVIDENCE_FROM_OTHER_REVISION


def test_revision_bound_evidence_reports_no_age(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """A number would invite comparison against a window that does not apply."""
    bundle = make_bundle(
        raw={
            "test_suite": make_record(
                source="pytest", detail={"exit_code": 0}, age=timedelta(days=3)
            )
        }
    )
    assert _judge(bundle, policy, ReadinessCriterionId.TEST_SUITE_PASSES).evidence_age_seconds is (
        None
    )


def test_a_bundle_with_no_revision_accepts_revision_bound_evidence(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """Nothing to disagree with is not a disagreement.

    ``GIT_REVISION_RECORDED`` is the criterion that catches an unidentifiable
    assessment; the freshness rule does not need to catch it a second time.
    """
    bundle = make_bundle(
        raw={"test_suite": make_record(source="pytest", detail={"exit_code": 0})},
        git_revision=None,
    )
    assert _judge(bundle, policy, ReadinessCriterionId.TEST_SUITE_PASSES).status is (
        ReadinessStatus.PASS
    )


# ---------------------------------------------------------------------------
# Freshness is applied before the predicate
# ---------------------------------------------------------------------------
def test_stale_evidence_never_reaches_its_predicate(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """A stale PASS is exactly the artifact section 29 exists to prevent."""
    bundle = make_bundle(
        raw={
            "test_suite": make_record(
                source="pytest", detail={"exit_code": 0}, git_revision=OTHER_REVISION
            )
        }
    )
    result = _judge(bundle, policy, ReadinessCriterionId.TEST_SUITE_PASSES)
    assert result.status is not ReadinessStatus.PASS
    assert "exited 0" not in result.detail


def test_an_uncollected_record_is_judged_before_freshness(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """ "The gateway refused" is more useful than "this is 5 hours old"."""
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                collected=False,
                error="refused",
                detail={"connected": False},
                age=timedelta(hours=5),
            )
        }
    )
    result = _judge(bundle, policy, ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert result.reason_code is ReadinessReasonCode.PAPER_GATEWAY_UNAVAILABLE


def test_freshness_is_measured_against_the_bundle_not_wall_clock(
    make_bundle: Callable[..., EvidenceBundle],
    make_record: Callable[..., Any],
    policy: ReadinessPolicy,
) -> None:
    """A replayed assessment reaches the same verdict however long ago it ran.

    The same reasoning ``risk.yaml``'s staleness window records: the whole
    chain is anchored at one ``as_of``, so a probe captured at that instant has
    age zero however long ago the run happened.
    """
    bundle = make_bundle(
        raw={
            "broker": make_record(
                source="IBKR",
                observed_at=NOW - timedelta(minutes=1),
                detail={"connected": True, "trading_mode": "PAPER", "broker": "IBKR"},
            )
        },
        as_of=NOW,
    )
    first = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    second = evaluate(bundle, policy, trading_mode=TradingMode.PAPER)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
