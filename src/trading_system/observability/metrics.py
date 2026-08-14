"""Operational metrics, with the cardinality guard (Milestone 11).

Metric names and their permitted labels, in one place, and a guard that refuses
a high-cardinality label at the point of recording rather than discovering it
when a Prometheus instance falls over.

**The rule.** A domain identifier is never a metric label. ``execution_id``,
``position_id``, ``campaign_id``, ``trace_id`` and the rest create one time
series per trade, and a metrics backend stores every one of them for the
retention period. Those identifiers belong in **traces and logs**, which are
built for exactly that — and the navigation path is: notice a rate change in a
metric, open the trace, follow the domain id to the immutable artifact.

The guard is enforced twice. ``config/observability.yaml`` lists the forbidden
labels, and :func:`record_count` / :func:`record_duration` drop them whatever
the configuration says — because a metrics explosion caused by a mis-edited
YAML file is still a metrics explosion.

Every recording call is best effort and never raises. This module is called
from inside trading operations, and a metric that could throw would be a
telemetry fault changing a trading outcome.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from trading_system.observability import tracing

__all__ = [
    "FORBIDDEN_LABELS",
    "METRIC_NAMES",
    "record_count",
    "record_duration",
    "safe_labels",
]

# --- counters ---------------------------------------------------------------
WORKFLOWS_TOTAL: Final = "trading_workflows_total"
WORKFLOWS_FAILED_TOTAL: Final = "trading_workflows_failed_total"

UNIVERSE_RUNS_TOTAL: Final = "universe_runs_total"
RESEARCH_RUNS_TOTAL: Final = "research_runs_total"
STRATEGY_DECISIONS_TOTAL: Final = "strategy_decisions_total"
CONTRACT_SELECTIONS_TOTAL: Final = "contract_selections_total"

RISK_APPROVED_TOTAL: Final = "risk_approved_total"
RISK_REJECTED_TOTAL: Final = "risk_rejected_total"
ALLOCATIONS_TOTAL: Final = "allocations_total"

EXECUTION_SUBMISSIONS_TOTAL: Final = "execution_submissions_total"
EXECUTION_REJECTIONS_TOTAL: Final = "execution_rejections_total"
EXECUTION_FILLS_TOTAL: Final = "execution_fills_total"
EXECUTION_CANCELLATIONS_TOTAL: Final = "execution_cancellations_total"
EXECUTION_UNKNOWN_TOTAL: Final = "execution_unknown_total"

POSITIONS_OPENED_TOTAL: Final = "positions_opened_total"
POSITIONS_CLOSED_TOTAL: Final = "positions_closed_total"

EXIT_EVALUATIONS_TOTAL: Final = "exit_evaluations_total"
EXIT_WAIT_TOTAL: Final = "exit_wait_total"
EXIT_BLOCK_TOTAL: Final = "exit_block_total"
EXIT_TRIGGERED_TOTAL: Final = "exit_triggered_total"

TRAILING_TRIGGERED_TOTAL: Final = "trailing_triggered_total"
EXPIRATION_EXITS_TOTAL: Final = "expiration_exits_total"
THESIS_INVALIDATIONS_TOTAL: Final = "thesis_invalidations_total"
TAKE_PROFIT_EXITS_TOTAL: Final = "take_profit_exits_total"
MAX_LOSS_EXITS_TOTAL: Final = "max_loss_exits_total"

BROKER_CONNECTION_ERRORS_TOTAL: Final = "broker_connection_errors_total"
BROKER_TIMEOUTS_TOTAL: Final = "broker_timeouts_total"
BROKER_REQUESTS_TOTAL: Final = "broker_requests_total"

LLM_REQUESTS_TOTAL: Final = "llm_requests_total"
LLM_ERRORS_TOTAL: Final = "llm_errors_total"
LLM_TOKENS_TOTAL: Final = "llm_tokens_total"

RECONCILIATION_RUNS_TOTAL: Final = "reconciliation_runs_total"
RECONCILIATION_FINDINGS_TOTAL: Final = "reconciliation_findings_total"

PNL_RESULTS_TOTAL: Final = "pnl_results_total"
SETTLEMENTS_TOTAL: Final = "settlements_total"
SETTLEMENTS_BLOCKED_TOTAL: Final = "settlements_blocked_total"

SCHEDULER_JOBS_TOTAL: Final = "scheduler_jobs_total"
SCHEDULER_TICKS_TOTAL: Final = "scheduler_ticks_total"
ALERTS_RAISED_TOTAL: Final = "alerts_raised_total"

# --- histograms -------------------------------------------------------------
WORKFLOW_DURATION: Final = "trading_workflow_duration_seconds"
RESEARCH_DURATION: Final = "research_duration_seconds"
STRATEGY_DURATION: Final = "strategy_duration_seconds"
CONTRACT_SELECTION_DURATION: Final = "contract_selection_duration_seconds"
RISK_DURATION: Final = "risk_duration_seconds"
ALLOCATION_DURATION: Final = "allocation_duration_seconds"
EXECUTION_DURATION: Final = "execution_duration_seconds"
BROKER_SUBMISSION_DURATION: Final = "broker_submission_duration_seconds"
BROKER_REQUEST_DURATION: Final = "broker_request_duration_seconds"
POSITION_MONITOR_DURATION: Final = "position_monitor_duration_seconds"
EXIT_EVALUATION_DURATION: Final = "exit_evaluation_duration_seconds"
RECONCILIATION_DURATION: Final = "reconciliation_duration_seconds"
PNL_DURATION: Final = "pnl_duration_seconds"
SCHEDULER_JOB_DURATION: Final = "scheduler_job_duration_seconds"
LLM_LATENCY: Final = "llm_latency_seconds"

#: Every instrument this system emits. A closed list so a dashboard query can
#: be checked against it, and so a typo produces a test failure rather than a
#: panel that is permanently empty.
METRIC_NAMES: Final = (
    WORKFLOWS_TOTAL,
    WORKFLOWS_FAILED_TOTAL,
    UNIVERSE_RUNS_TOTAL,
    RESEARCH_RUNS_TOTAL,
    STRATEGY_DECISIONS_TOTAL,
    CONTRACT_SELECTIONS_TOTAL,
    RISK_APPROVED_TOTAL,
    RISK_REJECTED_TOTAL,
    ALLOCATIONS_TOTAL,
    EXECUTION_SUBMISSIONS_TOTAL,
    EXECUTION_REJECTIONS_TOTAL,
    EXECUTION_FILLS_TOTAL,
    EXECUTION_CANCELLATIONS_TOTAL,
    EXECUTION_UNKNOWN_TOTAL,
    POSITIONS_OPENED_TOTAL,
    POSITIONS_CLOSED_TOTAL,
    EXIT_EVALUATIONS_TOTAL,
    EXIT_WAIT_TOTAL,
    EXIT_BLOCK_TOTAL,
    EXIT_TRIGGERED_TOTAL,
    TRAILING_TRIGGERED_TOTAL,
    EXPIRATION_EXITS_TOTAL,
    THESIS_INVALIDATIONS_TOTAL,
    TAKE_PROFIT_EXITS_TOTAL,
    MAX_LOSS_EXITS_TOTAL,
    BROKER_CONNECTION_ERRORS_TOTAL,
    BROKER_TIMEOUTS_TOTAL,
    BROKER_REQUESTS_TOTAL,
    LLM_REQUESTS_TOTAL,
    LLM_ERRORS_TOTAL,
    LLM_TOKENS_TOTAL,
    RECONCILIATION_RUNS_TOTAL,
    RECONCILIATION_FINDINGS_TOTAL,
    PNL_RESULTS_TOTAL,
    SETTLEMENTS_TOTAL,
    SETTLEMENTS_BLOCKED_TOTAL,
    SCHEDULER_JOBS_TOTAL,
    SCHEDULER_TICKS_TOTAL,
    ALERTS_RAISED_TOTAL,
    WORKFLOW_DURATION,
    RESEARCH_DURATION,
    STRATEGY_DURATION,
    CONTRACT_SELECTION_DURATION,
    RISK_DURATION,
    ALLOCATION_DURATION,
    EXECUTION_DURATION,
    BROKER_SUBMISSION_DURATION,
    BROKER_REQUEST_DURATION,
    POSITION_MONITOR_DURATION,
    EXIT_EVALUATION_DURATION,
    RECONCILIATION_DURATION,
    PNL_DURATION,
    SCHEDULER_JOB_DURATION,
    LLM_LATENCY,
)

#: Labels refused at the point of recording, whatever configuration says.
#:
#: Every one is a domain identifier, and a domain identifier as a metric label
#: is one time series per trade. The guard is in code as well as in
#: ``config/observability.yaml`` because a metrics explosion caused by a
#: mis-edited YAML file is still a metrics explosion — and because the
#: consequence lands on a system this one does not own.
FORBIDDEN_LABELS: Final = frozenset(
    {
        "trace_id",
        "span_id",
        "campaign_id",
        "universe_selection_id",
        "universe_id",
        "research_run_id",
        "research_id",
        "strategy_decision_id",
        "contract_selection_id",
        "risk_evaluation_id",
        "risk_id",
        "allocation_id",
        "execution_id",
        "position_id",
        "exit_id",
        "broker_order_id",
        "reconciliation_id",
        "pnl_id",
        "settlement_id",
        "job_run_id",
        "opportunity_id",
        "account",
        "account_id",
        "account_reference",
        "symbol",
        "underlying",
        # The dotted forms, so an attribute constant used as a label by mistake
        # is refused under either spelling.
        "trading.campaign.id",
        "trading.universe.id",
        "trading.research.id",
        "trading.strategy.id",
        "trading.contract.id",
        "trading.risk.id",
        "trading.allocation.id",
        "trading.execution.id",
        "trading.position.id",
        "trading.exit.id",
        "trading.broker_order.id",
        "trading.reconciliation.id",
        "trading.pnl.id",
        "trading.symbol",
    }
)


def safe_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    """Drop every high-cardinality label. Never raises.

    Dropped rather than rejected: refusing the whole measurement would lose an
    operational signal because of a labelling mistake, and the signal is worth
    more than the label. What must not happen is the label reaching the
    backend, and that is what this guarantees.
    """
    if not labels:
        return {}
    safe: dict[str, str] = {}
    for name, value in labels.items():
        try:
            if name.lower() in FORBIDDEN_LABELS:
                continue
            if value is None:
                continue
            safe[name] = str(value)
        except Exception:  # pragma: no cover - a guard must never raise
            continue
    return safe


def record_count(
    instrument: str, value: int = 1, *, labels: Mapping[str, str] | None = None
) -> None:
    """Increment a counter, with the cardinality guard applied."""
    tracing.record_count(instrument, value, labels=safe_labels(labels))


def record_duration(
    instrument: str, seconds: float, *, labels: Mapping[str, str] | None = None
) -> None:
    """Record a duration in seconds, with the cardinality guard applied."""
    tracing.record_duration(instrument, seconds, labels=safe_labels(labels))
