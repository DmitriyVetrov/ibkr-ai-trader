"""The criterion catalogue is complete, and each predicate judges honestly.

The catalogue tests are structural: every criterion in the vocabulary has a
definition, no slot is orphaned, and nothing is defined twice. The predicate
tests are behavioural, and they all probe the same boundary — *what does this
say when the evidence does not settle the question?*
"""

from __future__ import annotations

from typing import Any

import pytest

from trading_system.domain.enums import (
    ReadinessCriterionId,
    ReadinessEvidenceKind,
    ReadinessReasonCode,
    ReadinessStatus,
)
from trading_system.readiness.criteria import READINESS_CRITERIA, criterion
from trading_system.readiness.evidence import EvidenceRecord

pytestmark = pytest.mark.unit

NOW = __import__("datetime").datetime(2026, 8, 15, 12, 0, tzinfo=__import__("datetime").UTC)


def _judge(criterion_id: ReadinessCriterionId, detail: dict[str, Any], **kwargs: Any):
    definition = criterion(criterion_id)
    record = EvidenceRecord.of(
        kind=ReadinessEvidenceKind.COMMAND,
        source="test",
        observed_at=NOW,
        detail=detail,
        **kwargs,
    )
    return definition.predicate(record)


# ---------------------------------------------------------------------------
# The catalogue is complete
# ---------------------------------------------------------------------------
def test_every_criterion_in_the_vocabulary_has_a_definition() -> None:
    """A criterion nobody defined can never be satisfied.

    Configuration can name it as blocking, and the level it blocks would then
    stay shut for ever with nothing on screen explaining why.
    """
    defined = {definition.criterion_id for definition in READINESS_CRITERIA}
    missing = sorted(c.value for c in set(ReadinessCriterionId) - defined)
    assert not missing, f"criteria with no definition: {missing}"


def test_no_criterion_is_defined_twice() -> None:
    ids = [definition.criterion_id for definition in READINESS_CRITERIA]
    assert len(ids) == len(set(ids))


def test_every_definition_has_a_title_and_a_slot() -> None:
    for definition in READINESS_CRITERIA:
        assert definition.title.strip(), definition.criterion_id
        assert definition.slot.strip(), definition.criterion_id


def test_every_window_names_a_real_freshness_key(policy) -> None:
    """A typo'd window name would silently mean "no freshness rule at all"."""
    for definition in READINESS_CRITERIA:
        if definition.window is None:
            continue
        assert policy.window_seconds(definition.window) is not None, (
            f"{definition.criterion_id.value} names window {definition.window!r}, "
            f"which config/readiness.yaml does not define"
        )


def test_the_configured_policy_names_no_undefined_criterion(policy) -> None:
    assert policy.unknown_criteria() == ()


# ---------------------------------------------------------------------------
# A predicate never passes on absent evidence
# ---------------------------------------------------------------------------
def test_no_predicate_passes_on_an_empty_payload() -> None:
    """The single most important property of the whole catalogue.

    Every predicate is handed a record with nothing in it. None of them may
    conclude ``PASS``: an empty payload is silence, and silence is not
    agreement. ``dict.get(key, False)`` anywhere would turn silence into a
    ``FAIL``, which is also wrong but far less dangerous — this asserts the
    dangerous direction and the next test asserts the other.
    """
    offenders = []
    for definition in READINESS_CRITERIA:
        record = EvidenceRecord.of(
            kind=ReadinessEvidenceKind.COMMAND, source="empty", observed_at=NOW, detail={}
        )
        verdict = definition.predicate(record)
        if verdict.status is ReadinessStatus.PASS:
            offenders.append(definition.criterion_id.value)
    assert not offenders, f"these criteria passed with no evidence at all: {offenders}"


def test_an_empty_payload_is_inconclusive_rather_than_a_defect() -> None:
    """Silence is a question, not a failure — with two deliberate exceptions.

    ``GIT_REVISION_RECORDED`` and ``PAPER_BROKER_REACHABLE`` are genuine
    failures when nothing was recorded: a readiness result that cannot name its
    revision is not auditable, and a paper gate with no reachable gateway has
    not met the thing it is a gate for.
    """
    allowed_failures = {
        ReadinessCriterionId.GIT_REVISION_RECORDED,
        ReadinessCriterionId.PAPER_BROKER_REACHABLE,
    }
    for definition in READINESS_CRITERIA:
        record = EvidenceRecord.of(
            kind=ReadinessEvidenceKind.COMMAND, source="empty", observed_at=NOW, detail={}
        )
        verdict = definition.predicate(record)
        if definition.criterion_id in allowed_failures:
            continue
        assert verdict.status is not ReadinessStatus.FAIL, (
            f"{definition.criterion_id.value} calls an absence of evidence a defect; "
            f"it should be UNKNOWN"
        )


def test_no_predicate_passes_an_uncollected_record() -> None:
    """A collector that could not complete never yields a satisfied criterion."""
    offenders = []
    for definition in READINESS_CRITERIA:
        record = EvidenceRecord.of(
            kind=ReadinessEvidenceKind.SERVICE_PROBE,
            source="broken",
            observed_at=NOW,
            collected=False,
            error="the collector failed",
            detail={},
        )
        if definition.predicate(record).status is ReadinessStatus.PASS:
            offenders.append(definition.criterion_id.value)
    assert not offenders, f"these criteria passed an uncollected record: {offenders}"


# ---------------------------------------------------------------------------
# Command results preserve the evidence
# ---------------------------------------------------------------------------
def test_a_command_with_no_exit_code_cannot_pass() -> None:
    """Brief section 6A: record the command result, not ``tests_passed=true``."""
    verdict = _judge(ReadinessCriterionId.TEST_SUITE_PASSES, {"passed": 4895, "failed": 0})
    assert verdict.status is ReadinessStatus.UNKNOWN
    assert "exit code" in verdict.detail


def test_a_command_that_exited_nonzero_fails_with_its_own_reason() -> None:
    verdict = _judge(ReadinessCriterionId.TEST_SUITE_PASSES, {"exit_code": 1, "failed": 3})
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.TESTS_FAILED
    assert "failed=3" in verdict.detail


def test_a_command_that_exited_zero_passes() -> None:
    verdict = _judge(ReadinessCriterionId.LINT_CLEAN, {"exit_code": 0})
    assert verdict.status is ReadinessStatus.PASS
    assert verdict.reason is ReadinessReasonCode.SATISFIED


# ---------------------------------------------------------------------------
# The daily figure has three states
# ---------------------------------------------------------------------------
def test_a_tracked_daily_figure_passes() -> None:
    verdict = _judge(ReadinessCriterionId.DAILY_LOSS_STATE_KNOWN, {"daily_pnl_status": "TRACKED"})
    assert verdict.status is ReadinessStatus.PASS


def test_an_unknown_daily_figure_is_a_failure_not_a_question() -> None:
    """Money moved and we could not measure it. That is worse than not looking."""
    verdict = _judge(ReadinessCriterionId.DAILY_LOSS_STATE_KNOWN, {"daily_pnl_status": "UNKNOWN"})
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.DAILY_LOSS_UNKNOWN


def test_an_untracked_daily_figure_is_a_question_not_a_failure() -> None:
    """A deployment that has never closed a position is not defective."""
    verdict = _judge(
        ReadinessCriterionId.DAILY_LOSS_STATE_KNOWN, {"daily_pnl_status": "NOT_TRACKED"}
    )
    assert verdict.status is ReadinessStatus.UNKNOWN
    assert verdict.reason is ReadinessReasonCode.DAILY_LOSS_NOT_TRACKED


def test_the_two_daily_states_are_never_collapsed() -> None:
    unknown = _judge(ReadinessCriterionId.DAILY_LOSS_STATE_KNOWN, {"daily_pnl_status": "UNKNOWN"})
    untracked = _judge(
        ReadinessCriterionId.DAILY_LOSS_STATE_KNOWN, {"daily_pnl_status": "NOT_TRACKED"}
    )
    assert unknown.reason is not untracked.reason
    assert unknown.status is not untracked.status


# ---------------------------------------------------------------------------
# Reconciliation: an inability to observe is never a match
# ---------------------------------------------------------------------------
def test_a_reconciliation_match_passes() -> None:
    assert (
        _judge(ReadinessCriterionId.RECONCILIATION_RUNS, {"status": "MATCH"}).status
        is ReadinessStatus.PASS
    )


def test_broker_data_unavailable_is_never_a_match() -> None:
    """Brief section 6F, and Milestone 9's own invariant."""
    verdict = _judge(
        ReadinessCriterionId.RECONCILIATION_RUNS, {"status": "BROKER_DATA_UNAVAILABLE"}
    )
    assert verdict.status is ReadinessStatus.UNKNOWN
    assert verdict.reason is ReadinessReasonCode.RECONCILIATION_UNKNOWN
    assert "not a match" in verdict.detail


def test_a_reconciliation_mismatch_fails() -> None:
    verdict = _judge(ReadinessCriterionId.RECONCILIATION_RUNS, {"status": "MISMATCH"})
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.RECONCILIATION_MISMATCH


# ---------------------------------------------------------------------------
# Broker reads: empty is an answer, unavailable is not
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["OK", "EMPTY"])
def test_an_empty_broker_read_is_a_valid_answer(status: str) -> None:
    """Milestone 9's distinction, reused rather than restated."""
    verdict = _judge(ReadinessCriterionId.POSITIONS_READABLE, {"positions_status": status})
    assert verdict.status is ReadinessStatus.PASS


@pytest.mark.parametrize("status", ["UNAVAILABLE", "TIMEOUT", "MALFORMED"])
def test_a_failed_broker_read_is_never_an_empty_account(status: str) -> None:
    verdict = _judge(ReadinessCriterionId.POSITIONS_READABLE, {"positions_status": status})
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.BROKER_READ_FAILED


def test_a_not_requested_read_is_a_question() -> None:
    verdict = _judge(ReadinessCriterionId.ORDERS_READABLE, {"orders_status": "NOT_REQUESTED"})
    assert verdict.status is ReadinessStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Configuration safety
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["PAPER", "DRY_RUN"])
def test_paper_and_dry_run_are_safe_modes_to_assess_from(mode: str) -> None:
    assert (
        _judge(ReadinessCriterionId.TRADING_MODE_SAFE, {"trading_mode": mode}).status
        is ReadinessStatus.PASS
    )


def test_assessing_from_live_is_refused() -> None:
    verdict = _judge(
        ReadinessCriterionId.TRADING_MODE_SAFE,
        {
            "trading_mode": "LIVE",
            "live_trading_confirmed": True,
            "live_readiness_checklist_signed_off": True,
        },
    )
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.UNSAFE_MODE


def test_the_shipped_execution_switch_passes() -> None:
    verdict = _judge(
        ReadinessCriterionId.EXECUTION_SWITCH_SAFE,
        {"execution_enabled": False, "trading_mode": "PAPER", "execution_allow_live": False},
    )
    assert verdict.status is ReadinessStatus.PASS
    assert "nothing can submit" in verdict.detail


def test_execution_enabled_under_paper_with_authorisation_is_acceptable() -> None:
    """Enabled is not by itself unsafe: a paper submission needs it."""
    verdict = _judge(
        ReadinessCriterionId.EXECUTION_SWITCH_SAFE,
        {
            "execution_enabled": True,
            "trading_mode": "PAPER",
            "require_explicit_authorization": True,
            "execution_allow_live": False,
        },
    )
    assert verdict.status is ReadinessStatus.PASS
    assert "--confirm" in verdict.detail


def test_execution_enabled_without_explicit_authorisation_fails() -> None:
    verdict = _judge(
        ReadinessCriterionId.EXECUTION_SWITCH_SAFE,
        {
            "execution_enabled": True,
            "trading_mode": "PAPER",
            "require_explicit_authorization": False,
            "execution_allow_live": False,
        },
    )
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.EXECUTION_ENABLED_WITHOUT_POLICY


def test_execution_allowing_live_fails_whatever_else_is_set() -> None:
    verdict = _judge(
        ReadinessCriterionId.EXECUTION_SWITCH_SAFE,
        {
            "execution_enabled": False,
            "trading_mode": "PAPER",
            "execution_allow_live": True,
        },
    )
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.UNSAFE_MODE


def test_a_writable_broker_fails_the_read_only_criterion() -> None:
    verdict = _judge(ReadinessCriterionId.BROKER_READ_ONLY, {"ibkr_read_only": False})
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.BROKER_WRITABLE


# ---------------------------------------------------------------------------
# Cardinality
# ---------------------------------------------------------------------------
def test_a_domain_identifier_in_a_metric_label_fails() -> None:
    verdict = _judge(
        ReadinessCriterionId.METRIC_CARDINALITY_SAFE,
        {"forbidden_labels_found": ["execution_id"], "guarded_labels": 16},
    )
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.FORBIDDEN_METRIC_LABEL
    assert "execution_id" in verdict.detail


def test_no_forbidden_label_passes() -> None:
    verdict = _judge(
        ReadinessCriterionId.METRIC_CARDINALITY_SAFE,
        {"forbidden_labels_found": [], "guarded_labels": 16},
    )
    assert verdict.status is ReadinessStatus.PASS


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
def test_a_collector_that_is_up_but_received_nothing_fails() -> None:
    """Being up is not the same as receiving telemetry — M11's exact gap."""
    verdict = _judge(ReadinessCriterionId.COLLECTOR_RECEIVES_TELEMETRY, {"spans_accepted": 0})
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.TELEMETRY_NOT_RECEIVED


def test_a_collector_that_accepted_spans_passes() -> None:
    verdict = _judge(ReadinessCriterionId.COLLECTOR_RECEIVES_TELEMETRY, {"spans_accepted": 3})
    assert verdict.status is ReadinessStatus.PASS


def test_one_unhealthy_service_fails_the_stack_criterion() -> None:
    verdict = _judge(
        ReadinessCriterionId.OBSERVABILITY_STACK_RUNNING,
        {"services": {"tempo": True, "loki": False, "grafana": True}},
    )
    assert verdict.status is ReadinessStatus.FAIL
    assert "loki" in verdict.detail


def test_correlation_needs_both_backends() -> None:
    """A trace in the tracing backend alone is not correlation."""
    verdict = _judge(
        ReadinessCriterionId.TRACE_LOG_CORRELATION,
        {"trace_id": "abc", "trace_found": True, "log_found": False},
    )
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.CORRELATION_NOT_DEMONSTRATED


def test_correlation_in_both_backends_passes() -> None:
    verdict = _judge(
        ReadinessCriterionId.TRACE_LOG_CORRELATION,
        {"trace_id": "abc", "trace_found": True, "log_found": True},
    )
    assert verdict.status is ReadinessStatus.PASS


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def test_a_failed_job_fails_and_an_unknown_job_is_a_question() -> None:
    """Milestone 11's distinction: Python cannot kill a thread."""
    failed = _judge(
        ReadinessCriterionId.SCHEDULER_JOBS_HEALTHY, {"failed_jobs": 1, "unknown_jobs": 0}
    )
    assert failed.status is ReadinessStatus.FAIL
    assert failed.reason is ReadinessReasonCode.SCHEDULER_JOB_FAILED

    unknown = _judge(
        ReadinessCriterionId.SCHEDULER_JOBS_HEALTHY, {"failed_jobs": 0, "unknown_jobs": 2}
    )
    assert unknown.status is ReadinessStatus.UNKNOWN
    assert unknown.reason is ReadinessReasonCode.SCHEDULER_JOB_UNKNOWN


def test_skipped_jobs_are_not_a_problem() -> None:
    """A job that deliberately did not run is not an error."""
    verdict = _judge(
        ReadinessCriterionId.SCHEDULER_JOBS_HEALTHY,
        {"failed_jobs": 0, "unknown_jobs": 0, "skipped_jobs": 7},
    )
    assert verdict.status is ReadinessStatus.PASS
    assert "7 deliberately skipped" in verdict.detail


# ---------------------------------------------------------------------------
# Source control and history
# ---------------------------------------------------------------------------
def test_a_dirty_tree_reports_how_many_files_changed() -> None:
    verdict = _judge(
        ReadinessCriterionId.WORKING_TREE_CLEAN,
        {"working_tree_clean": False, "changed_files": 12},
    )
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.WORKING_TREE_DIRTY
    assert "12" in verdict.detail


def test_a_missing_revision_is_a_failure() -> None:
    verdict = _judge(ReadinessCriterionId.GIT_REVISION_RECORDED, {"git_revision": None})
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.REVISION_UNAVAILABLE


def test_insufficient_history_names_every_shortfall() -> None:
    """ "Insufficient history" is not actionable; the numbers are."""
    verdict = _judge(
        ReadinessCriterionId.OPERATIONAL_HISTORY_SUFFICIENT,
        {"shortfalls": ["readiness_runs: 1 recorded, 3 required"]},
    )
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.INSUFFICIENT_OPERATIONAL_HISTORY
    assert "1 recorded, 3 required" in verdict.detail


def test_a_tracked_secret_is_named() -> None:
    verdict = _judge(ReadinessCriterionId.SECRETS_NOT_TRACKED, {"tracked_secret_files": [".env"]})
    assert verdict.status is ReadinessStatus.FAIL
    assert verdict.reason is ReadinessReasonCode.SECRET_TRACKED_IN_GIT
    assert ".env" in verdict.detail


# ---------------------------------------------------------------------------
# Every detail is actionable
# ---------------------------------------------------------------------------
def test_no_verdict_detail_is_merely_not_ready() -> None:
    """Brief section 27: a criterion must say something a reader can act on."""
    for definition in READINESS_CRITERIA:
        record = EvidenceRecord.of(
            kind=ReadinessEvidenceKind.COMMAND, source="empty", observed_at=NOW, detail={}
        )
        detail = definition.predicate(record).detail
        assert len(detail) > 20, f"{definition.criterion_id.value}: {detail!r} says nothing"
        assert detail.strip().upper() != "NOT READY"
