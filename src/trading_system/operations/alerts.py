"""Operational alert rules. Pure evaluation over captured state (Milestone 11).

An alert is a **notification**. Nothing in this module places, cancels or
modifies an order; nothing it imports can either, and
``tests/operations/test_boundaries.py`` walks the transitive graph to prove it.
Safety is enforced by the domain — the risk engine refuses a trade, the exit
engine blocks a position, reconciliation reports a mismatch, the reservation
ledger refuses to release ``UNKNOWN`` capital. This module is how a person
finds out that one of those happened.

.. code-block:: text

    captured operational facts
          |
    AlertRules (pure, config-driven thresholds)
          |
    Alert                       stored, then offered to channels
          |
    NotificationProvider        best-effort; a failure to send is recorded

Three rules govern it:

* **A threshold is a count in a window.** One broker timeout on a Monday
  morning is weather; three in half an hour is a problem. Thresholds live in
  ``config/alerts.yaml`` and nowhere else.
* **A safety alert cannot be muted.** A ``CRITICAL`` rule with
  ``enabled: false`` fails to load, because turning off the notification does
  not turn off the condition.
* **Identity is stable across re-firings.** An alert's id derives from its
  code, its subject and its window start, so a condition that is still true on
  the next health tick is the *same* alert rather than a new one every five
  minutes. Re-firing an unresolved condition is how a channel becomes noise
  and then becomes ignored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading_system.domain.enums import (
    AlertCategory,
    AlertCode,
    AlertSeverity,
    DailyPnLStatus,
    TradingMode,
)
from trading_system.infrastructure.settings import AlertsConfig
from trading_system.operations.models import Alert, alert_identifier

__all__ = ["CATEGORY_OF", "AlertFacts", "AlertRules"]

#: Which category each alert belongs to. A mapping rather than a field on the
#: enum so the vocabulary stays a plain list of names and the grouping can
#: change without rewriting stored records.
CATEGORY_OF: dict[AlertCode, AlertCategory] = {
    AlertCode.BROKER_CONNECTION_ERRORS: AlertCategory.BROKER,
    AlertCode.BROKER_TIMEOUTS: AlertCategory.BROKER,
    AlertCode.BROKER_UNAVAILABLE: AlertCategory.BROKER,
    AlertCode.EXECUTION_REJECTION_RATE: AlertCategory.EXECUTION,
    AlertCode.EXECUTION_UNKNOWN: AlertCategory.EXECUTION,
    AlertCode.EXECUTION_DUPLICATE_ATTEMPT: AlertCategory.EXECUTION,
    AlertCode.EXECUTION_STUCK: AlertCategory.EXECUTION,
    AlertCode.LLM_ERROR_RATE: AlertCategory.RESEARCH,
    AlertCode.RESEARCH_FAILURE_RATE: AlertCategory.RESEARCH,
    AlertCode.WORKFLOW_FAILURES: AlertCategory.WORKFLOW,
    AlertCode.SCHEDULER_JOB_FAILED: AlertCategory.WORKFLOW,
    AlertCode.SCHEDULER_JOB_UNKNOWN: AlertCategory.WORKFLOW,
    AlertCode.LIVE_EXECUTION_ATTEMPT: AlertCategory.SAFETY,
    AlertCode.ORDER_WITHOUT_ALLOCATION: AlertCategory.SAFETY,
    AlertCode.ORDER_WITHOUT_EXECUTION_RECORD: AlertCategory.SAFETY,
    AlertCode.EXECUTION_OUTSIDE_AUTHORIZED_PATH: AlertCategory.SAFETY,
    AlertCode.RECONCILIATION_MISMATCH: AlertCategory.SAFETY,
    AlertCode.POSITION_CLOSE_NOT_CONFIRMED: AlertCategory.POSITION,
    AlertCode.EXPIRATION_APPROACHING: AlertCategory.POSITION,
    AlertCode.EXIT_UNKNOWN: AlertCategory.POSITION,
    AlertCode.DAILY_LOSS_THRESHOLD_EXCEEDED: AlertCategory.CAPITAL,
    AlertCode.DAILY_LOSS_UNAVAILABLE: AlertCategory.CAPITAL,
    AlertCode.SETTLEMENT_BLOCKED: AlertCategory.CAPITAL,
    AlertCode.TELEMETRY_EXPORT_FAILING: AlertCategory.TELEMETRY,
}


@dataclass(frozen=True, slots=True)
class AlertFacts:
    """Everything the rules are evaluated against. All captured, all counts.

    Deliberately counts and identifiers rather than objects. An alert says
    *how many* and *which*, and a rule that held a whole execution record would
    be tempted to reason about it — which is how alerting turns into a second,
    untested decision layer.
    """

    as_of: datetime
    trading_mode: TradingMode

    # --- broker ------------------------------------------------------------
    broker_connection_errors: int = 0
    broker_timeouts: int = 0
    broker_unavailable: bool = False

    # --- execution ---------------------------------------------------------
    execution_rejections: int = 0
    unknown_execution_ids: tuple[str, ...] = ()
    duplicate_execution_attempts: int = 0
    #: Executions that reached the broker and have been working far longer than
    #: the configured submission window without resolving.
    stuck_execution_ids: tuple[str, ...] = ()

    # --- research / AI -----------------------------------------------------
    llm_errors: int = 0
    research_failures: int = 0

    # --- workflow ----------------------------------------------------------
    failed_jobs: tuple[str, ...] = ()
    unknown_jobs: tuple[str, ...] = ()
    workflow_failures: int = 0

    # --- safety ------------------------------------------------------------
    live_execution_attempts: int = 0
    orders_without_allocation: tuple[str, ...] = ()
    orders_without_execution_record: tuple[str, ...] = ()
    executions_outside_authorized_path: tuple[str, ...] = ()
    critical_reconciliation_findings: tuple[str, ...] = ()

    # --- position ----------------------------------------------------------
    positions_close_not_confirmed: tuple[str, ...] = ()
    positions_near_expiration: tuple[str, ...] = ()
    exit_unknown_position_ids: tuple[str, ...] = ()

    # --- capital -----------------------------------------------------------
    daily_pnl_status: DailyPnLStatus = DailyPnLStatus.NOT_TRACKED
    daily_loss: str | None = None
    daily_loss_limit: str | None = None
    daily_loss_exceeded: bool = False
    blocked_settlements: tuple[str, ...] = ()

    # --- telemetry ---------------------------------------------------------
    telemetry_export_failures: int = 0

    references: dict[str, str] = field(default_factory=dict)


class AlertRules:
    """Evaluates the configured rules against captured facts. Pure.

    No clock, no store, no broker, no notification. The rules produce alerts;
    delivering them is :mod:`trading_system.operations.notifications`, and
    persisting them is the operations store. Keeping the three apart is what
    makes "an alert that nobody could be told about is still an alert that
    happened" implementable rather than aspirational.
    """

    def __init__(self, config: AlertsConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def evaluate(self, facts: AlertFacts) -> list[Alert]:
        """Every alert whose condition is currently met, most severe first."""
        if not self._config.enabled:
            return []

        alerts: list[Alert] = []
        for builder in (
            self._broker,
            self._execution,
            self._research,
            self._workflow,
            self._safety,
            self._position,
            self._capital,
            self._telemetry,
        ):
            alerts.extend(builder(facts))

        severity_order = {
            AlertSeverity.CRITICAL: 0,
            AlertSeverity.WARNING: 1,
            AlertSeverity.INFO: 2,
        }
        return sorted(alerts, key=lambda alert: (severity_order[alert.severity], alert.code.value))

    # --- rule groups -------------------------------------------------------
    def _broker(self, facts: AlertFacts) -> list[Alert]:
        alerts = []
        alerts += self._maybe(
            AlertCode.BROKER_CONNECTION_ERRORS,
            facts,
            occurrences=facts.broker_connection_errors,
            subject="broker",
            summary=f"{facts.broker_connection_errors} broker connection error(s)",
            action="Check the gateway is running and reachable, then re-run 'test ibkr-connection'",
        )
        alerts += self._maybe(
            AlertCode.BROKER_TIMEOUTS,
            facts,
            occurrences=facts.broker_timeouts,
            subject="broker",
            summary=f"{facts.broker_timeouts} broker request timeout(s)",
            action=(
                "A second uncached round trip on one connection can go unanswered "
                "indefinitely against this TWS build. Check whether a caller is batching "
                "requests onto one connection"
            ),
        )
        alerts += self._maybe(
            AlertCode.BROKER_UNAVAILABLE,
            facts,
            occurrences=1 if facts.broker_unavailable else 0,
            subject="broker",
            summary="the broker is unreachable",
            action="Nothing can be observed or sent until it is back. Investigate the gateway",
        )
        return alerts

    def _execution(self, facts: AlertFacts) -> list[Alert]:
        alerts = []
        alerts += self._maybe(
            AlertCode.EXECUTION_REJECTION_RATE,
            facts,
            occurrences=facts.execution_rejections,
            subject="execution",
            summary=f"{facts.execution_rejections} execution(s) rejected by the broker",
            action="Review the rejection reasons; a new Milestone 7 authorisation is required",
        )
        alerts += self._maybe(
            AlertCode.EXECUTION_UNKNOWN,
            facts,
            occurrences=len(facts.unknown_execution_ids),
            subject="execution",
            summary=(
                f"{len(facts.unknown_execution_ids)} execution(s) have an unknown outcome: "
                f"{', '.join(facts.unknown_execution_ids[:5])}"
            ),
            action=(
                "Resolve by OBSERVING the broker — 'execution explain --resolve' or a "
                "reconciliation run. There is no retry path, and the capital stays locked "
                "until the broker settles what happened"
            ),
            references={"execution_ids": ",".join(facts.unknown_execution_ids[:10])},
        )
        alerts += self._maybe(
            AlertCode.EXECUTION_DUPLICATE_ATTEMPT,
            facts,
            occurrences=facts.duplicate_execution_attempts,
            subject="execution",
            summary=f"{facts.duplicate_execution_attempts} duplicate submission attempt(s)",
            action="The idempotency check refused them. Investigate what tried twice",
        )
        alerts += self._maybe(
            AlertCode.EXECUTION_STUCK,
            facts,
            occurrences=len(facts.stuck_execution_ids),
            subject="execution",
            summary=(f"{len(facts.stuck_execution_ids)} order(s) working far longer than expected"),
            action="Observe the broker; consider an explicit cancel if the order is stale",
            references={"execution_ids": ",".join(facts.stuck_execution_ids[:10])},
        )
        return alerts

    def _research(self, facts: AlertFacts) -> list[Alert]:
        alerts = []
        alerts += self._maybe(
            AlertCode.LLM_ERROR_RATE,
            facts,
            occurrences=facts.llm_errors,
            subject="llm",
            summary=f"{facts.llm_errors} model call(s) failed",
            action=(
                "Research fails closed: an unreachable model produces no outlook rather than "
                "a synthesised one. Check the credential and the provider"
            ),
        )
        alerts += self._maybe(
            AlertCode.RESEARCH_FAILURE_RATE,
            facts,
            occurrences=facts.research_failures,
            subject="research",
            summary=f"{facts.research_failures} research report(s) failed",
            action="Inspect 'research show' for the per-symbol status",
        )
        return alerts

    def _workflow(self, facts: AlertFacts) -> list[Alert]:
        alerts = []
        alerts += self._maybe(
            AlertCode.SCHEDULER_JOB_FAILED,
            facts,
            occurrences=len(facts.failed_jobs),
            subject="scheduler",
            summary=f"scheduled job(s) failed: {', '.join(sorted(set(facts.failed_jobs))[:5])}",
            action="Jobs are isolated, so unrelated jobs kept running. Check 'ops jobs'",
            references={"jobs": ",".join(sorted(set(facts.failed_jobs))[:10])},
        )
        alerts += self._maybe(
            AlertCode.SCHEDULER_JOB_UNKNOWN,
            facts,
            occurrences=len(facts.unknown_jobs),
            subject="scheduler",
            summary=(
                f"scheduled job(s) never recorded a result: "
                f"{', '.join(sorted(set(facts.unknown_jobs))[:5])}"
            ),
            action=(
                "This is a question rather than a failure: the work may have completed. "
                "Every job is idempotent, so the next firing establishes the answer"
            ),
            references={"jobs": ",".join(sorted(set(facts.unknown_jobs))[:10])},
        )
        alerts += self._maybe(
            AlertCode.WORKFLOW_FAILURES,
            facts,
            occurrences=facts.workflow_failures,
            subject="workflow",
            summary=f"{facts.workflow_failures} workflow run(s) failed",
            action="Check the per-stage run records for the failing stage",
        )
        return alerts

    def _safety(self, facts: AlertFacts) -> list[Alert]:
        alerts = []
        alerts += self._maybe(
            AlertCode.LIVE_EXECUTION_ATTEMPT,
            facts,
            occurrences=facts.live_execution_attempts,
            subject="safety",
            summary=f"{facts.live_execution_attempts} attempt(s) to execute in LIVE mode",
            action=(
                "LIVE is refused in the configuration, in the broker factory and in the "
                "adapter. Establish what attempted it before anything else"
            ),
        )
        alerts += self._maybe(
            AlertCode.ORDER_WITHOUT_ALLOCATION,
            facts,
            occurrences=len(facts.orders_without_allocation),
            subject="safety",
            summary=(
                f"{len(facts.orders_without_allocation)} order(s) with no allocation behind them"
            ),
            action="Every order descends from a Milestone 7 authorisation. Investigate at once",
            references={"orders": ",".join(facts.orders_without_allocation[:10])},
        )
        alerts += self._maybe(
            AlertCode.ORDER_WITHOUT_EXECUTION_RECORD,
            facts,
            occurrences=len(facts.orders_without_execution_record),
            subject="safety",
            summary=(
                f"{len(facts.orders_without_execution_record)} broker order(s) this system "
                f"has no execution record for"
            ),
            action=(
                "An order at the broker with nothing behind it is the most serious finding "
                "this system can make. Reconciliation reports it; resolving it is a person's "
                "decision"
            ),
            references={"orders": ",".join(facts.orders_without_execution_record[:10])},
        )
        alerts += self._maybe(
            AlertCode.EXECUTION_OUTSIDE_AUTHORIZED_PATH,
            facts,
            occurrences=len(facts.executions_outside_authorized_path),
            subject="safety",
            summary=(
                f"{len(facts.executions_outside_authorized_path)} execution(s) did not come "
                f"through the authorised path"
            ),
            action="Establish the origin before permitting any further execution",
        )
        alerts += self._maybe(
            AlertCode.RECONCILIATION_MISMATCH,
            facts,
            occurrences=len(facts.critical_reconciliation_findings),
            subject="reconciliation",
            summary=(
                f"{len(facts.critical_reconciliation_findings)} critical reconciliation "
                f"finding(s): "
                f"{', '.join(sorted(set(facts.critical_reconciliation_findings))[:4])}"
            ),
            action=(
                "IBKR is authoritative. New executions should not proceed until this is "
                "resolved; reconciliation reports and never repairs"
            ),
        )
        return alerts

    def _position(self, facts: AlertFacts) -> list[Alert]:
        alerts = []
        alerts += self._maybe(
            AlertCode.POSITION_CLOSE_NOT_CONFIRMED,
            facts,
            occurrences=len(facts.positions_close_not_confirmed),
            subject="position",
            summary=(
                f"{len(facts.positions_close_not_confirmed)} position(s) were expected to "
                f"close and the broker still reports them"
            ),
            action="Only broker reality closes a position. Observe, do not re-send",
            references={"positions": ",".join(facts.positions_close_not_confirmed[:10])},
        )
        alerts += self._maybe(
            AlertCode.EXPIRATION_APPROACHING,
            facts,
            occurrences=len(facts.positions_near_expiration),
            subject="position",
            summary=(
                f"{len(facts.positions_near_expiration)} position(s) are inside the "
                f"expiration window"
            ),
            action=(
                "The exit policy force-exits at the configured DTE. This is notice that it "
                "is about to, not a request to act"
            ),
            references={"positions": ",".join(facts.positions_near_expiration[:10])},
        )
        alerts += self._maybe(
            AlertCode.EXIT_UNKNOWN,
            facts,
            occurrences=len(facts.exit_unknown_position_ids),
            subject="position",
            summary=(f"{len(facts.exit_unknown_position_ids)} exit(s) have an unknown outcome"),
            action=(
                "The order may be live right now. It is never re-sent; resolution is by "
                "observing the broker"
            ),
            references={"positions": ",".join(facts.exit_unknown_position_ids[:10])},
        )
        return alerts

    def _capital(self, facts: AlertFacts) -> list[Alert]:
        alerts = []
        alerts += self._maybe(
            AlertCode.DAILY_LOSS_THRESHOLD_EXCEEDED,
            facts,
            occurrences=1 if facts.daily_loss_exceeded else 0,
            subject="capital",
            summary=(
                f"the day's realised loss {facts.daily_loss} has reached the configured "
                f"limit {facts.daily_loss_limit}"
            ),
            action=(
                "The risk engine refuses new capital on its own. This is notification, not "
                "enforcement"
            ),
        )
        alerts += self._maybe(
            AlertCode.DAILY_LOSS_UNAVAILABLE,
            facts,
            occurrences=1 if facts.daily_pnl_status is DailyPnLStatus.UNKNOWN else 0,
            subject="capital",
            summary=("positions closed today and the day's realised result could not be computed"),
            action=(
                "An unknown loss is not a zero loss. Check 'pnl show' for which position "
                "produced no figure — usually a missing commission report or an unresolved "
                "execution"
            ),
        )
        alerts += self._maybe(
            AlertCode.SETTLEMENT_BLOCKED,
            facts,
            occurrences=len(facts.blocked_settlements),
            subject="capital",
            summary=(f"{len(facts.blocked_settlements)} settlement(s) refused to return capital"),
            action=(
                "Each names the evidence that was missing. Capital returns on "
                "broker-confirmed closure and nothing weaker"
            ),
            references={"reservations": ",".join(facts.blocked_settlements[:10])},
        )
        return alerts

    def _telemetry(self, facts: AlertFacts) -> list[Alert]:
        return self._maybe(
            AlertCode.TELEMETRY_EXPORT_FAILING,
            facts,
            occurrences=facts.telemetry_export_failures,
            subject="telemetry",
            summary=f"{facts.telemetry_export_failures} telemetry export failure(s)",
            action=(
                "Observability only. No trading decision depends on the collector being reachable"
            ),
        )

    # --- internals ---------------------------------------------------------
    def _maybe(
        self,
        code: AlertCode,
        facts: AlertFacts,
        *,
        occurrences: int,
        subject: str,
        summary: str,
        action: str,
        references: dict[str, str] | None = None,
    ) -> list[Alert]:
        """One alert, if its rule is enabled and its threshold is met."""
        rule = self._config.rules.get(code.value)
        if rule is None or not rule.enabled:
            return []
        if occurrences < rule.threshold:
            return []

        window_start = facts.as_of - timedelta(minutes=rule.window_minutes)
        return [
            Alert(
                alert_id=alert_identifier(code=code, subject=subject, window_start=window_start),
                code=code,
                category=CATEGORY_OF[code],
                severity=rule.severity,
                subject=subject,
                summary=summary,
                raised_at=facts.as_of,
                window_start=window_start,
                window_end=facts.as_of,
                occurrences=occurrences,
                threshold=rule.threshold,
                trading_mode=facts.trading_mode,
                references={**facts.references, **(references or {})},
                recommended_action=action,
            )
        ]


def most_severe(alerts: Sequence[Alert]) -> AlertSeverity | None:
    """The worst severity among some alerts, or ``None`` when there are none."""
    if not alerts:
        return None
    order = {AlertSeverity.INFO: 0, AlertSeverity.WARNING: 1, AlertSeverity.CRITICAL: 2}
    return max(alerts, key=lambda alert: order[alert.severity]).severity
