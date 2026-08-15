"""The readiness criterion catalogue (Milestone 12).

One definition per question the gate asks. Each names the evidence slot it
reads, the freshness rule that applies to it, and a **pure predicate** that
turns an :class:`~trading_system.readiness.evidence.EvidenceRecord` into a
status and a reason code.

Keeping the predicates here rather than scattering them across services is the
point of the module (brief section 6). A readiness rule spread over the code it
judges is a rule nobody can review as a whole, and the one property this
milestone must have above all others is that a person can read what "ready"
means in one sitting.

Every predicate obeys the same three rules:

* it receives a record and returns a verdict — no I/O, no clock, no globals;
* it never returns ``PASS`` for a record whose ``collected`` is false;
* when the record does not settle the question it returns ``UNKNOWN``, not
  ``FAIL``. A question and a defect call for different work, and a gate that
  reported every unanswered question as a failure would train its readers to
  skim past the real ones.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from trading_system.domain.enums import (
    ReadinessCriterionId,
    ReadinessDomain,
    ReadinessReasonCode,
    ReadinessStatus,
)
from trading_system.readiness.evidence import EvidenceRecord

__all__ = [
    "READINESS_CRITERIA",
    "CriterionDefinition",
    "Verdict",
    "criterion",
]


@dataclass(frozen=True, slots=True)
class Verdict:
    """What one predicate concluded, before freshness is applied."""

    status: ReadinessStatus
    reason: ReadinessReasonCode
    detail: str


@dataclass(frozen=True, slots=True)
class CriterionDefinition:
    """One readiness question, and how to answer it from evidence."""

    criterion_id: ReadinessCriterionId
    domain: ReadinessDomain
    #: What this criterion asserts, in a sentence a reader can check.
    title: str
    #: The evidence slot in the bundle this reads.
    slot: str
    #: Which time window in ``readiness.freshness.windows`` applies. ``None``
    #: means the criterion is revision-bound or has no freshness rule.
    window: str | None
    predicate: Callable[[EvidenceRecord], Verdict]


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------
def _passed(detail: str) -> Verdict:
    return Verdict(ReadinessStatus.PASS, ReadinessReasonCode.SATISFIED, detail)


def _failed(reason: ReadinessReasonCode, detail: str) -> Verdict:
    return Verdict(ReadinessStatus.FAIL, reason, detail)


def _unknown(reason: ReadinessReasonCode, detail: str) -> Verdict:
    return Verdict(ReadinessStatus.UNKNOWN, reason, detail)


def _flag(
    key: str,
    *,
    on_false: ReadinessReasonCode,
    passed: str,
    failed: str,
) -> Callable[[EvidenceRecord], Verdict]:
    """A predicate over one boolean in the record's detail.

    A missing key is ``UNKNOWN``, never ``FAIL`` and emphatically never
    ``PASS``: a collector that did not report the flag has told us nothing
    about it, and ``dict.get(key, False)`` would silently convert that silence
    into a defect.
    """

    def predicate(record: EvidenceRecord) -> Verdict:
        value = record.detail.get(key)
        if value is None:
            return _unknown(
                ReadinessReasonCode.NO_EVIDENCE,
                f"the collector recorded no '{key}' value, so this was not established",
            )
        return _passed(passed) if bool(value) else _failed(on_false, failed)

    return predicate


def _command(*, on_failure: ReadinessReasonCode, what: str) -> Callable[[EvidenceRecord], Verdict]:
    """A predicate over a recorded command result.

    Reads the *exit code*, and requires that one was actually recorded. Brief
    section 6A is explicit that a readiness report must preserve the command
    result rather than a ``tests_passed = true``, and this is where that is
    enforced: a record with no ``exit_code`` cannot pass, however cheerful its
    other fields.
    """

    def predicate(record: EvidenceRecord) -> Verdict:
        exit_code = record.detail.get("exit_code")
        if exit_code is None:
            return _unknown(
                ReadinessReasonCode.NO_EVIDENCE,
                f"{what} recorded no exit code, so it cannot be said to have passed",
            )
        if int(exit_code) == 0:
            return _passed(f"{what} exited 0: {_summarise(record.detail)}")
        return _failed(
            on_failure,
            f"{what} exited {int(exit_code)}: {_summarise(record.detail)}",
        )

    return predicate


def _summarise(detail: Mapping[str, Any]) -> str:
    """A compact, deterministic rendering of a detail payload."""
    interesting = (
        "passed",
        "failed",
        "skipped",
        "errors",
        "count",
        "findings",
        "runs",
        "days",
        "status",
    )
    parts = [f"{key}={detail[key]}" for key in interesting if detail.get(key) is not None]
    return ", ".join(parts) if parts else "no further detail recorded"


def _zero(
    key: str, *, on_nonzero: ReadinessReasonCode, what: str
) -> Callable[[EvidenceRecord], Verdict]:
    """A predicate requiring a recorded count to be zero."""

    def predicate(record: EvidenceRecord) -> Verdict:
        value = record.detail.get(key)
        if value is None:
            return _unknown(
                ReadinessReasonCode.NO_EVIDENCE,
                f"no '{key}' count was recorded, so {what} was not established",
            )
        count = int(value)
        if count == 0:
            return _passed(f"{what}: {key}=0")
        return _failed(on_nonzero, f"{what} failed: {key}={count}")

    return predicate


# ---------------------------------------------------------------------------
# Bespoke predicates
#
# Everything that is not a flag, a count or a command result. These are the
# criteria whose judgement is worth spelling out.
# ---------------------------------------------------------------------------
def _daily_loss_state(record: EvidenceRecord) -> Verdict:
    """The daily figure has three states, and only one of them is a measurement.

    ``UNKNOWN`` means positions closed today and at least one produced no
    usable figure — an absence of knowledge about a day on which money moved,
    which is emphatically not zero loss. ``NOT_TRACKED`` means no ledger was
    consulted at all. Milestone 11 keeps them apart and this criterion has to
    as well, because collapsing them is how an unmeasured day passes a loss
    limit.
    """
    status = record.detail.get("daily_pnl_status")
    if status is None:
        return _unknown(
            ReadinessReasonCode.NO_EVIDENCE,
            "no daily profit-and-loss status was recorded",
        )
    if status == "TRACKED":
        return _passed("the daily figure is TRACKED: a ledger was consulted and produced one")
    if status == "UNKNOWN":
        return _failed(
            ReadinessReasonCode.DAILY_LOSS_UNKNOWN,
            "the daily figure is UNKNOWN: a position closed today and produced no usable "
            "figure. This is an absence of knowledge about a day on which money moved, "
            "which is not the same as a zero loss",
        )
    if status == "NOT_TRACKED":
        return _unknown(
            ReadinessReasonCode.DAILY_LOSS_NOT_TRACKED,
            "no profit-and-loss ledger was consulted. For a deployment that has never "
            "closed a position this is the ordinary state, and it is reported rather "
            "than treated as a defect",
        )
    return _unknown(
        ReadinessReasonCode.NO_EVIDENCE,
        f"unrecognised daily profit-and-loss status {status!r}",
    )


def _reconciliation_outcome(record: EvidenceRecord) -> Verdict:
    """A reconciliation that could not observe the broker is never a MATCH.

    Brief section 6F, and Milestone 9's own invariant. ``BROKER_DATA_UNAVAILABLE``
    produces no comparison at all, so it cannot be read as agreement — agreeing
    with an absence of data is not agreement.
    """
    status = record.detail.get("status")
    if status is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "no reconciliation status was recorded")
    if status in {"MATCH", "SUCCESS"}:
        return _passed(f"reconciliation ran and reported {status}")
    if status in {"BROKER_DATA_UNAVAILABLE", "INTERNAL_DATA_UNAVAILABLE"}:
        return _unknown(
            ReadinessReasonCode.RECONCILIATION_UNKNOWN,
            f"reconciliation reported {status}: no comparison was made. An inability to "
            f"observe the broker is not a match",
        )
    if status == "MISMATCH":
        return _failed(
            ReadinessReasonCode.RECONCILIATION_MISMATCH,
            "reconciliation found a disagreement between internal records and broker "
            "reality. IBKR is authoritative; resolve it before trading",
        )
    return _unknown(
        ReadinessReasonCode.RECONCILIATION_UNKNOWN,
        f"reconciliation reported {status}, which does not establish agreement",
    )


def _broker_reachable(record: EvidenceRecord) -> Verdict:
    """The paper gateway answered, and it was the paper gateway (section 32)."""
    if not record.collected:
        return _failed(
            ReadinessReasonCode.PAPER_GATEWAY_UNAVAILABLE,
            f"no broker connection could be opened: {record.error or 'unknown error'}. "
            f"READY_FOR_PAPER cannot be claimed without a reachable paper gateway",
        )
    connected = record.detail.get("connected")
    if connected is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "no connection result was recorded")
    if not connected:
        return _failed(
            ReadinessReasonCode.BROKER_UNREACHABLE,
            f"the broker did not connect: {record.detail.get('detail') or 'no detail'}",
        )
    mode = record.detail.get("trading_mode")
    if mode == "LIVE":
        return _failed(
            ReadinessReasonCode.UNSAFE_MODE,
            "the broker probe ran against LIVE. The paper gate is about PAPER; a LIVE "
            "observation cannot satisfy it",
        )
    return _passed(
        f"the broker connected in {mode} mode as {record.detail.get('broker', 'unknown')}"
    )


def _broker_read(key: str, what: str) -> Callable[[EvidenceRecord], Verdict]:
    """A broker read succeeded, distinguishing empty from unavailable.

    Milestone 9's central distinction, reused rather than restated: an empty
    list means the account holds nothing and is a valid answer; a failed read
    means we could not look and is not.
    """

    def predicate(record: EvidenceRecord) -> Verdict:
        status = record.detail.get(key)
        if status is None:
            return _unknown(ReadinessReasonCode.NO_EVIDENCE, f"no {what} read status was recorded")
        if status in {"OK", "EMPTY"}:
            note = "the account holds none" if status == "EMPTY" else "read successfully"
            return _passed(f"{what} readable ({status}): {note}")
        if status == "NOT_REQUESTED":
            return _unknown(
                ReadinessReasonCode.NOT_COLLECTED,
                f"{what} was not requested during this read",
            )
        return _failed(
            ReadinessReasonCode.BROKER_READ_FAILED,
            f"{what} could not be read: {status}. 'We could not look' is not 'there is "
            f"nothing there'",
        )

    return predicate


def _working_tree_clean(record: EvidenceRecord) -> Verdict:
    """A dirty tree is reported explicitly, and blocks live review (section 29)."""
    clean = record.detail.get("working_tree_clean")
    if clean is None:
        return _unknown(
            ReadinessReasonCode.REVISION_UNAVAILABLE,
            "the working-tree state could not be determined",
        )
    if clean:
        return _passed("the working tree is clean; the evidence describes the code that runs")
    changed = record.detail.get("changed_files")
    return _failed(
        ReadinessReasonCode.WORKING_TREE_DIRTY,
        f"the working tree has {changed if changed is not None else 'uncommitted'} "
        f"modification(s). Test results describe a revision, and the code that would run "
        f"is not that revision",
    )


def _git_revision_recorded(record: EvidenceRecord) -> Verdict:
    revision = record.detail.get("git_revision")
    if not revision:
        return _failed(
            ReadinessReasonCode.REVISION_UNAVAILABLE,
            "no git revision could be determined. A readiness result that cannot name the "
            "code it describes is not auditable",
        )
    return _passed(f"assessed at revision {revision}")


def _mode_safe(record: EvidenceRecord) -> Verdict:
    """PAPER and DRY_RUN are safe; LIVE requires every guard (section 6B)."""
    mode = record.detail.get("trading_mode")
    if mode is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "no trading mode was recorded")
    if mode in {"PAPER", "DRY_RUN"}:
        return _passed(f"TRADING_MODE={mode}")
    if mode == "LIVE":
        confirmed = bool(record.detail.get("live_trading_confirmed"))
        signed = bool(record.detail.get("live_readiness_checklist_signed_off"))
        if confirmed and signed:
            return _failed(
                ReadinessReasonCode.UNSAFE_MODE,
                "TRADING_MODE=LIVE with both guards set. Readiness assesses a system that "
                "is not yet authorised to trade live; it does not certify one that already "
                "is. Assess from PAPER",
            )
        return _failed(
            ReadinessReasonCode.LIVE_GUARDS_INCONSISTENT,
            "TRADING_MODE=LIVE without both live guards set. Settings should have refused "
            "to construct at all; that this was observable is itself the finding",
        )
    return _unknown(ReadinessReasonCode.NO_EVIDENCE, f"unrecognised trading mode {mode!r}")


def _live_guards_consistent(record: EvidenceRecord) -> Verdict:
    """Guards may be off; they may not disagree with the mode."""
    mode = record.detail.get("trading_mode")
    confirmed = record.detail.get("live_trading_confirmed")
    signed = record.detail.get("live_readiness_checklist_signed_off")
    if mode is None or confirmed is None or signed is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "the live guards were not fully recorded")
    if mode == "LIVE" and not (confirmed and signed):
        return _failed(
            ReadinessReasonCode.LIVE_GUARDS_INCONSISTENT,
            "LIVE mode with a guard unset",
        )
    if mode != "LIVE" and (confirmed or signed):
        # Not a failure. A guard set ahead of the mode is a deliberate act by
        # somebody preparing for live, and reporting it as a defect would be
        # wrong; reporting it at all is the point.
        return _passed(
            f"TRADING_MODE={mode} with live_trading_confirmed={bool(confirmed)}, "
            f"checklist_signed_off={bool(signed)}. The guards are set ahead of the mode, "
            f"which is consistent but worth seeing"
        )
    return _passed(
        f"TRADING_MODE={mode}, live_trading_confirmed={bool(confirmed)}, "
        f"checklist_signed_off={bool(signed)}"
    )


def _execution_switch_safe(record: EvidenceRecord) -> Verdict:
    """``execution.enabled`` is acceptable only under an explicit paper policy.

    Brief section 6B. The switch being on is not by itself unsafe — a paper
    submission needs it — but on *with* LIVE, or on without the explicit
    authorisation requirement, is.
    """
    enabled = record.detail.get("execution_enabled")
    mode = record.detail.get("trading_mode")
    requires_authorization = record.detail.get("require_explicit_authorization")
    allow_live = record.detail.get("execution_allow_live")
    if enabled is None or mode is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "the execution switch was not recorded")
    if allow_live:
        return _failed(
            ReadinessReasonCode.UNSAFE_MODE,
            "execution.allow_live is set. LIVE execution is refused in configuration, in "
            "the broker factory and in the adapter; a configuration that permits it has "
            "removed the first of three",
        )
    if not enabled:
        return _passed("execution.enabled=false: the shipped default, and nothing can submit")
    if mode == "LIVE":
        return _failed(
            ReadinessReasonCode.UNSAFE_MODE,
            "execution.enabled=true with TRADING_MODE=LIVE",
        )
    if not requires_authorization:
        return _failed(
            ReadinessReasonCode.EXECUTION_ENABLED_WITHOUT_POLICY,
            "execution.enabled=true without require_explicit_authorization. Two switches "
            "are required and neither may imply the other",
        )
    return _passed(
        f"execution.enabled=true under TRADING_MODE={mode} with explicit authorisation "
        f"still required. An order additionally needs --confirm"
    )


def _broker_read_only(record: EvidenceRecord) -> Verdict:
    read_only = record.detail.get("ibkr_read_only")
    if read_only is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "IBKR_READ_ONLY was not recorded")
    if read_only:
        return _passed("IBKR_READ_ONLY=true: the adapter itself refuses to place an order")
    return _failed(
        ReadinessReasonCode.BROKER_WRITABLE,
        "IBKR_READ_ONLY=false. The connection can place orders. That is required for a "
        "deliberate paper submission and is not a state to sit in for live review",
    )


def _forbidden_labels(record: EvidenceRecord) -> Verdict:
    """No domain identifier may be a metric label (brief section 14)."""
    offenders = record.detail.get("forbidden_labels_found")
    if offenders is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "no metric label scan result was recorded")
    if not offenders:
        guarded = record.detail.get("guarded_labels")
        return _passed(
            f"no forbidden label reached a metric; {guarded if guarded is not None else 'the'} "
            f"guarded names were checked against live exposition"
        )
    return _failed(
        ReadinessReasonCode.FORBIDDEN_METRIC_LABEL,
        f"domain identifiers reached metric labels: {sorted(offenders)}. One time series "
        f"per trade is how a metrics backend falls over",
    )


def _services_running(record: EvidenceRecord) -> Verdict:
    """Every observability backend answered its own health endpoint."""
    if not record.collected:
        return _failed(
            ReadinessReasonCode.OBSERVABILITY_STACK_NOT_STARTED,
            f"the observability stack was not reachable: {record.error or 'unknown error'}",
        )
    services = record.detail.get("services")
    if not isinstance(services, dict) or not services:
        return _unknown(
            ReadinessReasonCode.NO_EVIDENCE, "no per-service health results were recorded"
        )
    unhealthy = sorted(name for name, ok in services.items() if not ok)
    if unhealthy:
        return _failed(
            ReadinessReasonCode.SERVICE_UNHEALTHY,
            f"these services did not report healthy: {unhealthy}. Runtime health was "
            f"probed over HTTP; a validated compose file is not proof a stack is up",
        )
    return _passed(f"all {len(services)} services answered their health endpoint")


def _datasources(record: EvidenceRecord) -> Verdict:
    missing = record.detail.get("missing_datasources")
    if missing is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "Grafana datasources were not enumerated")
    if missing:
        return _failed(
            ReadinessReasonCode.DATASOURCE_NOT_PROVISIONED,
            f"Grafana is missing datasources {sorted(missing)}",
        )
    found = record.detail.get("datasources") or []
    return _passed(f"Grafana provisioned datasources {sorted(found)}")


def _dashboards(record: EvidenceRecord) -> Verdict:
    missing = record.detail.get("missing_dashboards")
    if missing is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "Grafana dashboards were not enumerated")
    if missing:
        return _failed(
            ReadinessReasonCode.DASHBOARD_NOT_PROVISIONED,
            f"Grafana is missing dashboards {sorted(missing)}",
        )
    found = record.detail.get("dashboards") or []
    return _passed(f"Grafana loaded dashboards {sorted(found)}")


def _correlation(record: EvidenceRecord) -> Verdict:
    """A trace id present in the tracing backend *and* in the log backend."""
    trace_id = record.detail.get("trace_id")
    in_tempo = record.detail.get("trace_found")
    in_loki = record.detail.get("log_found")
    if trace_id is None or in_tempo is None or in_loki is None:
        return _unknown(
            ReadinessReasonCode.NO_EVIDENCE,
            "the correlation probe recorded no result",
        )
    if in_tempo and in_loki:
        return _passed(
            f"trace {trace_id} was found in the tracing backend and a log line carrying the "
            f"same trace id was found in the log backend"
        )
    return _failed(
        ReadinessReasonCode.CORRELATION_NOT_DEMONSTRATED,
        f"trace {trace_id}: found in traces={bool(in_tempo)}, found in logs={bool(in_loki)}. "
        f"Correlation requires both",
    )


def _secrets_not_tracked(record: EvidenceRecord) -> Verdict:
    tracked = record.detail.get("tracked_secret_files")
    if tracked is None:
        return _unknown(
            ReadinessReasonCode.NO_EVIDENCE, "no secret-tracking scan result was recorded"
        )
    if tracked:
        return _failed(
            ReadinessReasonCode.SECRET_TRACKED_IN_GIT,
            f"these secret-bearing files are tracked in git: {sorted(tracked)}",
        )
    return _passed(".env and other secret-bearing paths are untracked and ignored")


def _operational_history(record: EvidenceRecord) -> Verdict:
    """ "It works" and "it has been operated" are different claims (section 21)."""
    shortfalls = record.detail.get("shortfalls")
    if shortfalls is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "operational history was not measured")
    if shortfalls:
        return _failed(
            ReadinessReasonCode.INSUFFICIENT_OPERATIONAL_HISTORY,
            "insufficient operational history: "
            + "; ".join(f"{item}" for item in sorted(map(str, shortfalls)))
            + ". A single successful run demonstrates that the system works and says "
            "nothing about whether it has been operated",
        )
    return _passed(f"operational history satisfied: {_summarise(record.detail)}")


def _scheduler_jobs_healthy(record: EvidenceRecord) -> Verdict:
    """``SKIPPED``, ``FAILED`` and ``UNKNOWN`` stay three different facts."""
    failed = record.detail.get("failed_jobs")
    unknown = record.detail.get("unknown_jobs")
    if failed is None or unknown is None:
        return _unknown(ReadinessReasonCode.NO_EVIDENCE, "scheduler job outcomes were not recorded")
    if int(failed):
        return _failed(
            ReadinessReasonCode.SCHEDULER_JOB_FAILED,
            f"{int(failed)} scheduler job(s) failed",
        )
    if int(unknown):
        return _unknown(
            ReadinessReasonCode.SCHEDULER_JOB_UNKNOWN,
            f"{int(unknown)} scheduler job(s) never recorded a completion. Python cannot "
            f"kill a thread, so an over-running job may still be in flight; this is a "
            f"question rather than a failure",
        )
    skipped = record.detail.get("skipped_jobs")
    return _passed(
        "no scheduler job failed or went unrecorded"
        + (f"; {int(skipped)} deliberately skipped" if skipped else "")
    )


def _telemetry_received(record: EvidenceRecord) -> Verdict:
    """The collector *accepted* spans, read off its own self-telemetry."""
    accepted = record.detail.get("spans_accepted")
    if accepted is None:
        return _unknown(
            ReadinessReasonCode.NO_EVIDENCE,
            "the collector's own telemetry did not report an accepted-span count",
        )
    if int(accepted) > 0:
        return _passed(
            f"the collector accepted {int(accepted)} span(s) over OTLP from this application"
        )
    return _failed(
        ReadinessReasonCode.TELEMETRY_NOT_RECEIVED,
        "the collector is running and has accepted no spans. Being up is not the same as "
        "receiving telemetry",
    )


def _found(
    key: str, *, on_missing: ReadinessReasonCode, what: str
) -> Callable[[EvidenceRecord], Verdict]:
    """A predicate requiring a signal to have been located in a backend."""

    def predicate(record: EvidenceRecord) -> Verdict:
        value = record.detail.get(key)
        if value is None:
            return _unknown(ReadinessReasonCode.NO_EVIDENCE, f"no result was recorded for {what}")
        if value:
            return _passed(f"{what}: {_summarise(record.detail)}")
        return _failed(on_missing, f"{what} was not found: {_summarise(record.detail)}")

    return predicate


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
def _define() -> tuple[CriterionDefinition, ...]:
    # Short local aliases. The catalogue is long enough that the fully
    # qualified names would wrap every line, and a wrapped catalogue is one
    # nobody reads end to end — which is the only way to read this one.
    c = ReadinessCriterionId
    d = ReadinessDomain
    r = ReadinessReasonCode

    return (
        # --- software quality ----------------------------------------------
        CriterionDefinition(
            c.TEST_SUITE_PASSES,
            d.SOFTWARE_QUALITY,
            "the offline test suite passes at this revision",
            "test_suite",
            None,
            _command(on_failure=r.TESTS_FAILED, what="pytest"),
        ),
        CriterionDefinition(
            c.LINT_CLEAN,
            d.SOFTWARE_QUALITY,
            "ruff reports no lint findings",
            "lint",
            None,
            _command(on_failure=r.LINT_FAILED, what="ruff check"),
        ),
        CriterionDefinition(
            c.FORMAT_CLEAN,
            d.SOFTWARE_QUALITY,
            "the tree is formatted",
            "format",
            None,
            _command(on_failure=r.FORMAT_FAILED, what="ruff format --check"),
        ),
        CriterionDefinition(
            c.TYPECHECK_CLEAN,
            d.SOFTWARE_QUALITY,
            "mypy reports no type errors",
            "typecheck",
            None,
            _command(on_failure=r.TYPECHECK_FAILED, what="mypy"),
        ),
        # --- configuration safety ------------------------------------------
        CriterionDefinition(
            c.CONFIGURATION_LOADS,
            d.CONFIGURATION_SAFETY,
            "the whole configuration tree loads and validates",
            "configuration",
            "configuration_seconds",
            _flag(
                "config_loaded",
                on_false=r.CONFIGURATION_INVALID,
                passed="every configuration file loaded and cross-file invariants hold",
                failed="the configuration tree did not load",
            ),
        ),
        CriterionDefinition(
            c.TRADING_MODE_SAFE,
            d.CONFIGURATION_SAFETY,
            "the trading mode is one readiness may be assessed from",
            "configuration",
            "configuration_seconds",
            _mode_safe,
        ),
        CriterionDefinition(
            c.LIVE_GUARDS_CONSISTENT,
            d.CONFIGURATION_SAFETY,
            "the live guards agree with the trading mode",
            "configuration",
            "configuration_seconds",
            _live_guards_consistent,
        ),
        CriterionDefinition(
            c.EXECUTION_SWITCH_SAFE,
            d.CONFIGURATION_SAFETY,
            "execution.enabled is off, or on under an explicit paper policy",
            "configuration",
            "configuration_seconds",
            _execution_switch_safe,
        ),
        CriterionDefinition(
            c.BROKER_READ_ONLY,
            d.CONFIGURATION_SAFETY,
            "the broker connection is read-only",
            "configuration",
            "configuration_seconds",
            _broker_read_only,
        ),
        # --- test isolation -------------------------------------------------
        CriterionDefinition(
            c.SUITE_RUNS_WITHOUT_GATEWAY,
            d.TEST_ISOLATION,
            "an ordinary pytest needs no gateway, credentials or personal .env",
            "test_isolation",
            None,
            _flag(
                "suite_is_hermetic",
                on_false=r.SUITE_REQUIRES_GATEWAY,
                passed="the suite clamps the safety-critical environment and skips "
                "gateway-backed tests by default",
                failed="the ordinary suite depends on a gateway or on local settings",
            ),
        ),
        CriterionDefinition(
            c.LIVE_SUITES_GATED,
            d.TEST_ISOLATION,
            "live and IBKR suites are skipped unless explicitly unlocked",
            "test_isolation",
            None,
            _flag(
                "live_suites_gated",
                on_false=r.SAFETY_CLAMP_MISSING,
                passed="live, ibkr and llm markers are skipped without ALLOW_LIVE_TESTS",
                failed="a gateway-backed suite runs by default",
            ),
        ),
        CriterionDefinition(
            c.PAPER_EXECUTION_SUITE_GATED,
            d.TEST_ISOLATION,
            "order-submitting suites need two separate unlocks",
            "test_isolation",
            None,
            _flag(
                "paper_execution_double_gated",
                on_false=r.SAFETY_CLAMP_MISSING,
                passed="paper_execution needs ALLOW_LIVE_TESTS and "
                "RUN_PAPER_EXECUTION_TESTS, checked independently",
                failed="an order-submitting suite is reachable with a single unlock",
            ),
        ),
        # --- broker ----------------------------------------------------------
        CriterionDefinition(
            c.PAPER_BROKER_REACHABLE,
            d.BROKER,
            "the IBKR Paper gateway answers",
            "broker",
            "broker_seconds",
            _broker_reachable,
        ),
        CriterionDefinition(
            c.ACCOUNT_READABLE,
            d.BROKER,
            "the account summary is readable",
            "broker",
            "broker_seconds",
            _broker_read("account_status", "the account summary"),
        ),
        CriterionDefinition(
            c.POSITIONS_READABLE,
            d.BROKER,
            "positions are readable",
            "broker",
            "broker_seconds",
            _broker_read("positions_status", "positions"),
        ),
        CriterionDefinition(
            c.ORDERS_READABLE,
            d.BROKER,
            "open orders are readable",
            "broker",
            "broker_seconds",
            _broker_read("orders_status", "open orders"),
        ),
        CriterionDefinition(
            c.FILLS_READABLE,
            d.BROKER,
            "executions and fills are readable",
            "broker",
            "broker_seconds",
            _broker_read("executions_status", "executions and fills"),
        ),
        # --- reconciliation ---------------------------------------------------
        CriterionDefinition(
            c.RECONCILIATION_RUNS,
            d.RECONCILIATION,
            "reconciliation compares internal records against broker reality",
            "reconciliation",
            "reconciliation_seconds",
            _reconciliation_outcome,
        ),
        CriterionDefinition(
            c.NO_CRITICAL_FINDINGS,
            d.RECONCILIATION,
            "no critical reconciliation finding is open",
            "reconciliation",
            "reconciliation_seconds",
            _zero(
                "critical_findings",
                on_nonzero=r.CRITICAL_FINDING_OPEN,
                what="critical reconciliation findings",
            ),
        ),
        CriterionDefinition(
            c.NO_UNRESOLVED_UNKNOWN_EXECUTIONS,
            d.RECONCILIATION,
            "no execution is in an unresolved UNKNOWN state",
            "reconciliation",
            "reconciliation_seconds",
            _zero(
                "unknown_executions",
                on_nonzero=r.UNRESOLVED_UNKNOWN_EXECUTION,
                what="unresolved UNKNOWN executions",
            ),
        ),
        # --- execution safety --------------------------------------------------
        CriterionDefinition(
            c.EXECUTION_SAFETY_GATES,
            d.EXECUTION_SAFETY,
            "every order-submission gate refuses before a broker is constructed",
            "execution_safety",
            None,
            _command(on_failure=r.SAFETY_GATE_BROKEN, what="the execution safety suite"),
        ),
        CriterionDefinition(
            c.ZERO_ORDERS_IN_SUITE,
            d.EXECUTION_SAFETY,
            "the suite submits zero orders",
            "execution_safety",
            None,
            _zero(
                "orders_submitted",
                on_nonzero=r.ORDERS_SUBMITTED_UNEXPECTEDLY,
                what="orders submitted during the offline suite",
            ),
        ),
        # --- position lifecycle ------------------------------------------------
        CriterionDefinition(
            c.POSITION_LIFECYCLE_VERIFIED,
            d.POSITION_LIFECYCLE,
            "the position lifecycle admits no false CLOSED state",
            "position_lifecycle",
            None,
            _command(on_failure=r.SAFETY_GATE_BROKEN, what="the position lifecycle suite"),
        ),
        CriterionDefinition(
            c.EXIT_MANAGEMENT_VERIFIED,
            d.POSITION_LIFECYCLE,
            "exit precedence, blocks and UNKNOWN handling hold",
            "exit_management",
            None,
            _command(on_failure=r.SAFETY_GATE_BROKEN, what="the exit management suite"),
        ),
        # --- capital -------------------------------------------------------------
        CriterionDefinition(
            c.PNL_FROM_CONFIRMED_FILLS,
            d.CAPITAL,
            "realised results come from broker-confirmed fills only",
            "pnl",
            None,
            _command(on_failure=r.SAFETY_GATE_BROKEN, what="the profit-and-loss suite"),
        ),
        CriterionDefinition(
            c.SETTLEMENT_IDEMPOTENT,
            d.CAPITAL,
            "settling twice has one economic effect",
            "pnl",
            None,
            _flag(
                "settlement_idempotent",
                on_false=r.SETTLEMENT_BLOCKED,
                passed="settlement outcomes are deltas against current state and replay to "
                "the same position",
                failed="settlement is not idempotent",
            ),
        ),
        CriterionDefinition(
            c.DAILY_LOSS_STATE_KNOWN,
            d.CAPITAL,
            "the daily loss figure is a measurement rather than an absence",
            "daily_loss",
            "pnl_seconds",
            _daily_loss_state,
        ),
        # --- scheduler ------------------------------------------------------------
        CriterionDefinition(
            c.SCHEDULER_RUNS,
            d.SCHEDULER,
            "the scheduler has ticked and persisted the result",
            "scheduler",
            "scheduler_seconds",
            _flag(
                "scheduler_ran",
                on_false=r.SCHEDULER_NEVER_RAN,
                passed="a scheduler tick was recorded",
                failed="no scheduler tick has ever been recorded",
            ),
        ),
        CriterionDefinition(
            c.SCHEDULER_JOBS_HEALTHY,
            d.SCHEDULER,
            "no scheduled job failed or went unrecorded",
            "scheduler",
            "scheduler_seconds",
            _scheduler_jobs_healthy,
        ),
        # --- observability ---------------------------------------------------------
        CriterionDefinition(
            c.OBSERVABILITY_STACK_RUNNING,
            d.OBSERVABILITY,
            "the collector, Tempo, Prometheus, Loki and Grafana are actually running",
            "observability_stack",
            "observability_seconds",
            _services_running,
        ),
        CriterionDefinition(
            c.COLLECTOR_RECEIVES_TELEMETRY,
            d.OBSERVABILITY,
            "the collector has accepted telemetry from this application",
            "collector",
            "observability_seconds",
            _telemetry_received,
        ),
        CriterionDefinition(
            c.TEMPO_HAS_TRACE,
            d.OBSERVABILITY,
            "a real application trace reached the tracing backend",
            "tempo",
            "observability_seconds",
            _found("trace_found", on_missing=r.TRACE_NOT_FOUND, what="the emitted trace"),
        ),
        CriterionDefinition(
            c.PROMETHEUS_HAS_METRIC,
            d.OBSERVABILITY,
            "a real application metric is queryable in the metrics backend",
            "prometheus",
            "observability_seconds",
            _found("metric_found", on_missing=r.METRIC_NOT_FOUND, what="the emitted metric"),
        ),
        CriterionDefinition(
            c.LOKI_HAS_LOG,
            d.OBSERVABILITY,
            "a real structured log line reached the log backend",
            "loki",
            "observability_seconds",
            _found("log_found", on_missing=r.LOG_NOT_FOUND, what="the emitted log line"),
        ),
        CriterionDefinition(
            c.GRAFANA_RUNNING,
            d.OBSERVABILITY,
            "Grafana started and answers its health API",
            "grafana",
            "observability_seconds",
            _flag(
                "grafana_healthy",
                on_false=r.SERVICE_UNHEALTHY,
                passed="Grafana answered /api/health with a database status of ok",
                failed="Grafana did not report healthy",
            ),
        ),
        CriterionDefinition(
            c.GRAFANA_DATASOURCES_PROVISIONED,
            d.OBSERVABILITY,
            "the tracing, metrics and log datasources are provisioned",
            "grafana",
            "observability_seconds",
            _datasources,
        ),
        CriterionDefinition(
            c.GRAFANA_DASHBOARDS_PROVISIONED,
            d.OBSERVABILITY,
            "the dashboards loaded",
            "grafana",
            "observability_seconds",
            _dashboards,
        ),
        CriterionDefinition(
            c.TRACE_LOG_CORRELATION,
            d.OBSERVABILITY,
            "one trace id is findable in both the tracing and the log backend",
            "correlation",
            "observability_seconds",
            _correlation,
        ),
        CriterionDefinition(
            c.METRIC_CARDINALITY_SAFE,
            d.OBSERVABILITY,
            "no domain identifier is used as a metric label",
            "cardinality",
            # A runtime window rather than a revision binding: the strongest
            # form of this evidence is a scan of what the collector is actually
            # exposing, and what a *running* process emits is not settled by
            # what is committed. Reading the configured guard is the weaker
            # fallback and the record says which was done.
            "observability_seconds",
            _forbidden_labels,
        ),
        # --- agents ------------------------------------------------------------------
        CriterionDefinition(
            c.AGENT_CONTRACTS_VALIDATED,
            d.AI_AGENTS,
            "each agent's schema and semantic validation holds offline",
            "agents",
            None,
            _command(on_failure=r.AGENT_VALIDATION_FAILED, what="the agent contract suites"),
        ),
        CriterionDefinition(
            c.AGENT_BOUNDARIES_ENFORCED,
            d.AI_AGENTS,
            "no agent reaches a broker, provider, repository or socket",
            "agent_boundaries",
            None,
            _command(on_failure=r.AGENT_VALIDATION_FAILED, what="the agent boundary suites"),
        ),
        # --- data -----------------------------------------------------------------------
        CriterionDefinition(
            c.DATA_POINT_IN_TIME_ENFORCED,
            d.DATA,
            "no reconstruction of a past instant can see later data",
            "data",
            None,
            _command(on_failure=r.DATA_QUALITY_FAILED, what="the point-in-time suites"),
        ),
        CriterionDefinition(
            c.DATA_QUALITY_ENFORCED,
            d.DATA,
            "quality flags are recorded rather than corrected",
            "data",
            None,
            _command(on_failure=r.DATA_QUALITY_FAILED, what="the data quality suites"),
        ),
        # --- security --------------------------------------------------------------------
        CriterionDefinition(
            c.SECRETS_NOT_TRACKED,
            d.SECURITY,
            ".env and other secret-bearing files are untracked",
            "secrets",
            None,
            _secrets_not_tracked,
        ),
        CriterionDefinition(
            c.TELEMETRY_PRIVACY_ENFORCED,
            d.SECURITY,
            "telemetry carries no account, credential, prompt or monetary payload",
            "privacy",
            None,
            _command(on_failure=r.SECRET_EXPOSED, what="the telemetry privacy suite"),
        ),
        CriterionDefinition(
            c.ACCOUNT_IDENTIFIERS_MASKED,
            d.SECURITY,
            "no stored readiness or position artifact carries a full account number",
            "masking",
            None,
            _flag(
                "account_identifiers_masked",
                on_false=r.SECRET_EXPOSED,
                passed="every account reference in a stored artifact is masked",
                failed="a full account number reached a stored artifact",
            ),
        ),
        # --- operational history -----------------------------------------------------------
        CriterionDefinition(
            c.OPERATIONAL_HISTORY_SUFFICIENT,
            d.OPERATIONAL_HISTORY,
            "enough operational history has accumulated for a live review",
            "operational_history",
            None,
            _operational_history,
        ),
        # --- source control ----------------------------------------------------------------
        CriterionDefinition(
            c.GIT_REVISION_RECORDED,
            d.SOURCE_CONTROL,
            "the assessment names the revision it describes",
            "git",
            None,
            _git_revision_recorded,
        ),
        CriterionDefinition(
            c.WORKING_TREE_CLEAN,
            d.SOURCE_CONTROL,
            "the working tree matches the revision the evidence describes",
            "git",
            None,
            _working_tree_clean,
        ),
    )


#: The catalogue, keyed by criterion id and in a fixed order.
#:
#: The order is the order criteria are reported in, so a reader sees software
#: quality before broker connectivity before observability every time. Nothing
#: derives a verdict from it — unlike ``EXIT_POLICY_PRECEDENCE``, where the
#: order *is* the policy.
READINESS_CRITERIA: tuple[CriterionDefinition, ...] = _define()

_BY_ID: dict[ReadinessCriterionId, CriterionDefinition] = {
    definition.criterion_id: definition for definition in READINESS_CRITERIA
}


def criterion(criterion_id: ReadinessCriterionId) -> CriterionDefinition:
    """The definition for one criterion id."""
    try:
        return _BY_ID[criterion_id]
    except KeyError:  # pragma: no cover - defensive
        raise KeyError(f"no readiness criterion is defined for {criterion_id}") from None
