"""Canonical operational artifacts: job runs, health and alerts (Milestone 11).

Four artifacts, and the boundaries between them are the design:

:class:`JobRun`
    One invocation of one scheduled job: which job, which scheduled instant,
    when it started, when it ended, what happened, and — where something went
    wrong — a *classified* error rather than a stack trace. Written before the
    job runs and completed afterwards, so a process that dies mid-job leaves a
    ``RUNNING`` record the next start reads as "we do not know", never as
    silence and never as success.
:class:`SchedulerRun`
    One tick: what was due, what ran, what was skipped and why. Isolation is
    visible here — a failed job sits next to the successful ones it did not
    stop.
:class:`OperationalHealth`
    The machine-readable health model, with **trading health and observability
    health kept apart**. An unreachable Grafana is not a trading fault, and a
    system that reported it as one would train its operators to ignore the
    alert that matters.
:class:`Alert`
    One notification. Nothing here can place, cancel or modify an order, and a
    boundary test asserts the package cannot reach anything that could.

What is deliberately absent:

* **No trading decision.** No quantity, no price, no permission, no order. The
  scheduler orchestrates services that already made those decisions.
* **No second copy of financial truth.** A job run references artifact ids; it
  never embeds an execution, a position or a result.
* **No secrets.** Account identifiers are masked before they reach any of these
  records, exactly as they are in every Milestone 9 artifact.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    JOB_ATTENTION_STATUSES,
    AlertCategory,
    AlertCode,
    AlertSeverity,
    HealthComponent,
    HealthDomain,
    HealthStatus,
    JobSkipReason,
    JobStatus,
    SchedulerRunStatus,
    TradingMode,
)
from trading_system.domain.models import (
    Identifier,
    ImmutableModel,
    Money,
    UtcDatetime,
)

__all__ = [
    "OPERATIONS_SCHEMA_VERSION",
    "Alert",
    "ComponentHealth",
    "JobRun",
    "OperationalHealth",
    "SchedulerRun",
    "alert_identifier",
    "job_run_identifier",
    "scheduler_run_identifier",
]

#: Bumped when a stored operational artifact changes shape.
OPERATIONS_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def job_run_identifier(
    *,
    job: str,
    scheduled_for: datetime,
    trading_mode: TradingMode,
    schema_version: str = OPERATIONS_SCHEMA_VERSION,
) -> str:
    """Derive one job run's identity from *which firing it is*.

    Deliberately derived from the **scheduled instant**, not from the moment
    the job actually started. That is what makes duplicate protection real: two
    processes that both wake for the 14:35 monitoring cycle derive the same id,
    the store recognises the second as a replay, and the job does not run twice.
    An id built from the start time would make every duplicate look new — which
    is precisely the bug this exists to prevent.
    """
    digest = stable_hash(
        ["JOB_RUN", schema_version, job, scheduled_for.isoformat(), trading_mode.value]
    )
    return f"jobrun-{digest[:20]}"


def scheduler_run_identifier(
    *,
    scheduled_for: datetime,
    jobs: list[str],
    trading_mode: TradingMode,
    schema_version: str = OPERATIONS_SCHEMA_VERSION,
) -> str:
    """Derive one tick's identity from the instant and the jobs it considered."""
    digest = stable_hash(
        [
            "SCHEDULER_RUN",
            schema_version,
            scheduled_for.isoformat(),
            sorted(jobs),
            trading_mode.value,
        ]
    )
    return f"tick-{digest[:20]}"


def alert_identifier(
    *,
    code: AlertCode,
    subject: str,
    window_start: datetime,
    schema_version: str = OPERATIONS_SCHEMA_VERSION,
) -> str:
    """Derive one alert's identity from what it is about and when.

    ``window_start`` rather than the current instant, so a condition that is
    still true on the next health tick produces the *same* alert rather than a
    new one every five minutes. Re-firing an unresolved condition is how an
    operator learns to filter the channel that matters.
    """
    digest = stable_hash(["ALERT", schema_version, code.value, subject, window_start.isoformat()])
    return f"alert-{digest[:20]}"


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------
class JobRun(ImmutableModel):
    """One invocation of one scheduled job.

    Written twice: once as ``RUNNING`` before the work starts, once complete
    when it ends. The first write is what makes restart safe — a process that
    dies mid-run leaves a ``RUNNING`` record, and the next start reads that as
    *we do not know whether this finished*, which is a question rather than
    either an error or a success.
    """

    job_run_id: Identifier
    job: Identifier
    schema_version: Identifier = OPERATIONS_SCHEMA_VERSION

    #: The cadence instant this run is *for*. Two processes waking for the same
    #: firing share this, which is what makes the duplicate check work.
    scheduled_for: UtcDatetime
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None

    status: JobStatus
    skip_reason: JobSkipReason | None = None
    trading_mode: TradingMode

    #: A short, stable classification — ``TimeoutError``, ``BrokerError``,
    #: ``ConfigError``. Never a stack trace: an operational record is read by a
    #: person deciding whether to intervene, and the trace belongs in the log.
    error_type: str | None = None
    error_message: str | None = None

    duration_seconds: float | None = Field(default=None, ge=0)
    #: Orders this job's work submitted, read off whatever service it invoked.
    #: Recorded on every run so "the scheduler placed no orders" is evidence.
    orders_submitted: int = Field(default=0, ge=0)

    #: Ids of the artifacts this run produced. References only — a job run is
    #: not a second copy of an execution, a position or a result.
    artifact_ids: list[str] = Field(default_factory=list)
    summary: str | None = None

    @model_validator(mode="after")
    def _a_finished_run_says_when(self) -> JobRun:
        if self.status is JobStatus.RUNNING:
            if self.finished_at is not None:
                raise ValueError(f"job run {self.job_run_id} is RUNNING but records a finish time")
            return self
        if self.finished_at is None:
            raise ValueError(
                f"job run {self.job_run_id} is {self.status.value} but never recorded when it "
                f"finished. A completed run is dated; an undated one is still in flight"
            )
        return self

    @model_validator(mode="after")
    def _a_skip_says_why_and_a_failure_says_what(self) -> JobRun:
        if self.status is JobStatus.SKIPPED and self.skip_reason is None:
            raise ValueError(
                f"job run {self.job_run_id} is SKIPPED without a reason. A job that did not "
                f"run is a fact somebody will ask about"
            )
        if self.status is JobStatus.FAILED and not self.error_type:
            raise ValueError(f"job run {self.job_run_id} is FAILED without an error classification")
        if self.status is not JobStatus.SKIPPED and self.skip_reason is not None:
            raise ValueError(
                f"job run {self.job_run_id} is {self.status.value} but carries a skip reason; "
                f"a job that ran was not skipped"
            )
        return self

    @model_validator(mode="after")
    def _a_job_that_did_not_run_submitted_nothing(self) -> JobRun:
        if self.status in (JobStatus.SKIPPED, JobStatus.BLOCKED) and self.orders_submitted:
            raise ValueError(
                f"job run {self.job_run_id} is {self.status.value} but reports "
                f"{self.orders_submitted} submitted order(s). A job that did not run cannot "
                f"have sent one"
            )
        return self

    @property
    def needs_attention(self) -> bool:
        return self.status in JOB_ATTENTION_STATUSES

    @property
    def complete(self) -> bool:
        return self.status is not JobStatus.RUNNING


class SchedulerRun(ImmutableModel):
    """One scheduler tick: what was due, what ran, and what each one did.

    Isolation is visible in the shape. A failed job appears alongside the
    successful ones, and ``status`` is ``PARTIAL`` rather than ``FAILED`` —
    because "research collection failed and reconciliation still ran" is the
    intended behaviour, not a degraded one.
    """

    scheduler_run_id: Identifier
    scheduled_for: UtcDatetime
    started_at: UtcDatetime
    finished_at: UtcDatetime
    schema_version: Identifier = OPERATIONS_SCHEMA_VERSION

    status: SchedulerRunStatus
    trading_mode: TradingMode
    timezone: str = "UTC"

    runs: list[JobRun] = Field(default_factory=list)
    duration_seconds: float = Field(default=0.0, ge=0)
    #: Summed from the job runs, which read it off the services they invoked.
    orders_submitted: int = Field(default=0, ge=0)
    detail: str | None = None

    @model_validator(mode="after")
    def _the_order_count_is_the_sum_of_its_jobs(self) -> SchedulerRun:
        total = sum(run.orders_submitted for run in self.runs)
        if self.orders_submitted != total:
            raise ValueError(
                f"scheduler run {self.scheduler_run_id} reports {self.orders_submitted} "
                f"submitted order(s) but its jobs report {total}. The count is evidence, not "
                f"a summary written by hand"
            )
        return self

    @property
    def failures(self) -> list[JobRun]:
        return [run for run in self.runs if run.status is JobStatus.FAILED]

    @property
    def executed(self) -> list[JobRun]:
        return [run for run in self.runs if run.status is not JobStatus.SKIPPED]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class ComponentHealth(ImmutableModel):
    """One component's state, and which health question it answers."""

    component: HealthComponent
    domain: HealthDomain
    status: HealthStatus
    summary: str = Field(min_length=1)
    detail: str | None = None
    #: Latency of the check itself, where the check measured one.
    checked_in_seconds: float | None = Field(default=None, ge=0)
    #: Structured facts a person would otherwise reconstruct by hand. Counts,
    #: statuses and references — never a payload, never a balance.
    facts: dict[str, str] = Field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.status is HealthStatus.HEALTHY


class OperationalHealth(ImmutableModel):
    """The machine-readable health report.

    Two overall verdicts, deliberately, and neither is derived from the other.
    ``trading_status`` answers *can this system safely trade?* and is computed
    from the trading components alone; ``observability_status`` answers *can we
    see what it is doing?*. A collector that is down degrades the second and
    leaves the first untouched — the milestone's central operational claim,
    made structural by putting the two in separate fields computed from
    separate inputs.
    """

    health_id: Identifier
    as_of: UtcDatetime
    schema_version: Identifier = OPERATIONS_SCHEMA_VERSION

    trading_status: HealthStatus
    observability_status: HealthStatus
    trading_mode: TradingMode
    application_version: str
    config_version: str

    components: list[ComponentHealth] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)
    detail: str | None = None

    @model_validator(mode="after")
    def _no_observability_component_decides_trading_health(self) -> OperationalHealth:
        """Trading health follows from trading components and nothing else.

        Enforced rather than trusted, because the failure it prevents is
        insidious: a Grafana outage that reported the trading domain as
        degraded would, after the third time, teach an operator to ignore
        exactly the banner that means a broker is unreachable.
        """
        trading = [c for c in self.components if c.domain is HealthDomain.TRADING]
        if not trading:
            return self
        worst = _worst(component.status for component in trading)
        if self.trading_status is not worst:
            raise ValueError(
                f"operational health {self.health_id} reports trading status "
                f"{self.trading_status.value} while its worst trading component is "
                f"{worst.value}. Trading health is derived from trading components; a "
                f"telemetry outage must never be able to move it"
            )
        return self

    def for_domain(self, domain: HealthDomain) -> list[ComponentHealth]:
        return [component for component in self.components if component.domain is domain]

    @property
    def healthy(self) -> bool:
        """Whether *trading* is healthy. Observability is reported separately."""
        return self.trading_status is HealthStatus.HEALTHY


def _worst(statuses: object) -> HealthStatus:
    """The most severe of a set of statuses.

    ``UNKNOWN`` outranks ``HEALTHY`` deliberately: a component nobody checked
    is not a component that is fine.
    """
    order = {
        HealthStatus.HEALTHY: 0,
        HealthStatus.UNKNOWN: 1,
        HealthStatus.DEGRADED: 2,
        HealthStatus.UNAVAILABLE: 3,
        HealthStatus.BLOCKED: 4,
    }
    worst = HealthStatus.HEALTHY
    for status in statuses:  # type: ignore[attr-defined]
        if order[status] > order[worst]:
            worst = status
    return worst


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
class Alert(ImmutableModel):
    """One operational notification.

    An alert *reports*. It cannot place, cancel or modify an order, there is no
    field here through which it could ask for one, and
    ``tests/operations/test_boundaries.py`` walks the import graph of the whole
    alerting path to prove nothing it can reach could either. Safety is
    enforced by the domain — the risk engine refuses a trade, the exit engine
    blocks a position, reconciliation reports a mismatch. This is how a person
    finds out one of those happened.
    """

    alert_id: Identifier
    code: AlertCode
    category: AlertCategory
    severity: AlertSeverity
    schema_version: Identifier = OPERATIONS_SCHEMA_VERSION

    #: What the alert is about: a job name, a component, a position id. Kept
    #: short and stable so the identity is stable across re-firings.
    subject: Identifier
    summary: str = Field(min_length=1)
    detail: str | None = None

    raised_at: UtcDatetime
    window_start: UtcDatetime
    window_end: UtcDatetime
    #: How many occurrences were counted in the window, and what fired it.
    occurrences: int = Field(default=1, ge=0)
    threshold: int = Field(default=1, ge=0)

    trading_mode: TradingMode
    #: Domain references, so an operator can navigate from the alert to the
    #: artifact. Ids only — an alert never carries a payload.
    references: dict[str, str] = Field(default_factory=dict)
    #: What a person should do. Never an instruction the system will carry out:
    #: this milestone notifies, and acting on a notification is a human's job.
    recommended_action: str | None = None
    #: Set when the alert was actually delivered somewhere. An alert that no
    #: channel accepted is still an alert that happened.
    notified_channels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _the_window_runs_forwards(self) -> Alert:
        if self.window_end < self.window_start:
            raise ValueError(f"alert {self.alert_id} has a window that ends before it starts")
        return self

    @model_validator(mode="after")
    def _no_alert_names_an_order_action(self) -> Alert:
        """A recommended action may not read as an instruction to trade.

        The same rule Milestone 9 applies to a reconciliation recommendation,
        for the same reason: the moment an operational message contains
        "sell 4 NVDA calls", somebody automates it, and the safety boundary
        this whole architecture rests on has a hole in it that nobody reviewed.
        """
        text = (self.recommended_action or "").lower()
        for phrase in ("place an order", "submit an order", "cancel the order", "buy ", "sell "):
            if phrase in text:
                raise ValueError(
                    f"alert {self.alert_id} recommends {phrase!r}. An alert names a condition "
                    f"and a person; it never names a trade"
                )
        return self


class AlertOccurrence(ImmutableModel):
    """One counted event behind an alert. Not stored separately; carried in memory.

    Kept as a model rather than a tuple so the *evidence* for an alert is
    inspectable: an operator asking "which three timeouts" gets three
    timestamps and three subjects rather than the number three.
    """

    occurred_at: UtcDatetime
    subject: Identifier
    detail: str | None = None
    value: Money | None = Field(default=None)


def alert_is_actionable(alert: Alert) -> bool:
    """Whether this alert crossed its own threshold. A rule, not a judgement."""
    return alert.occurrences >= alert.threshold


def total_of(values: list[Decimal]) -> Decimal:
    """Exact sum. Present so no caller reaches for ``sum`` over floats."""
    return sum(values, Decimal("0"))
