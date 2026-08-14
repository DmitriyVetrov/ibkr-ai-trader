"""The operational scheduler (Milestone 11).

Milestone 10 built ``ExitService.monitor`` and deliberately not the loop that
calls it. This is the loop.

.. code-block:: text

    tick(at)
      |
      +- due?            cron, in the schedule's own timezone
      +- enabled?        config, per job
      +- market open?    the Milestone 3 calendar; UNKNOWN never assumes open
      +- already ran?    the PERSISTED job run for this scheduled instant
      |
      +- write JobRun(RUNNING)     <- before the work, so a crash leaves a question
      +- run, bounded              <- a hang costs one job, not the tick
      +- write JobRun(final)       <- SUCCESS / FAILED / SKIPPED / BLOCKED / UNKNOWN
      |
      +- next job, whatever happened to the last one

Six properties, each with tests that fail loudly:

* **It contains no trading logic.** Every job is a call to an already-tested
  service method. Whether a position should close, whether an order may be
  sent, how much capital is free — none of those questions is asked here, and
  the scheduler could not answer them if it wanted to.
* **It holds no broker.** No connection, no factory, no import that reaches
  one. The services it calls open their own short-lived read-only connections
  where they need them, which is what keeps Milestone 2's
  one-reliable-round-trip constraint intact: a scheduler that held a persistent
  connection and polled through it is precisely the shape that constraint
  forbids.
* **Jobs are isolated.** An exception is caught, classified and recorded
  against the job that raised it. Research collection can fail all morning and
  reconciliation still runs — a claim
  ``tests/operations/test_scheduler.py`` makes by raising from one job and
  asserting the next one's record.
* **It is idempotent through persisted state.** A run's identity comes from
  the *scheduled instant*, so two processes waking for 14:35 derive the same id
  and the second recognises the first. There is no process-local flag anywhere
  in this file.
* **Restart is safe, and never optimistic.** A job run left ``RUNNING`` by a
  dead process is reclassified ``UNKNOWN`` — not ``FAILED``, which would claim
  we know it did not finish, and certainly not ``SUCCESS``.
* **A hang costs one job.** Each run is bounded by its configured timeout. An
  over-running job is recorded ``UNKNOWN`` and the tick moves on, because the
  work may still be in flight and calling that a failure would be a claim
  nobody can support.

**On bounded execution, honestly.** Python cannot kill a thread. The timeout
here stops the *scheduler* waiting, not the work: an over-running job may
continue in the background, which is why it is recorded as ``UNKNOWN`` rather
than terminated-and-failed, and why every job must be idempotent. The
underlying broker requests are separately bounded by
``IBKR_REQUEST_TIMEOUT_SECONDS``, which is where a genuinely stuck network call
is actually stopped.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_system.data.market_calendar import MarketCalendar, MarketCalendarError
from trading_system.domain.enums import (
    JobSkipReason,
    JobStatus,
    SchedulerRunStatus,
    TradingMode,
)
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import (
    ScheduleJob,
    Settings,
    SystemConfig,
    project_root,
)
from trading_system.operations.cron import CronError, matches, next_fire
from trading_system.operations.jobs import JobContext, JobDefinition, build_registry
from trading_system.operations.models import (
    JobRun,
    SchedulerRun,
    job_run_identifier,
    scheduler_run_identifier,
)
from trading_system.operations.store import (
    FilesystemOperationsRepository,
    OperationsRepository,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.observability.provider import TelemetryProvider

__all__ = ["JobPlan", "Scheduler", "SchedulerError"]

_logger = get_logger(__name__)


class SchedulerError(RuntimeError):
    """The scheduler itself could not start. Never raised by a job."""


@dataclass(frozen=True, slots=True)
class JobPlan:
    """What the scheduler decided about one job at one instant, before running it."""

    job: str
    definition: JobDefinition
    schedule: ScheduleJob
    scheduled_for: datetime
    due: bool
    skip_reason: JobSkipReason | None = None
    next_fire_at: datetime | None = None

    @property
    def will_run(self) -> bool:
        return self.due and self.skip_reason is None


class Scheduler:
    """Runs configured jobs on their cadence, one at a time, in isolation."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        repository: OperationsRepository | None = None,
        services: dict[str, Any] | None = None,
        telemetry: TelemetryProvider | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._repository = repository or FilesystemOperationsRepository(data_root / "operations")
        self._services = services or {}
        self._telemetry = telemetry
        self._calendar = MarketCalendar(config.data.market_calendar)
        self._registry = build_registry(config)
        self._validate_cadences()

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> OperationsRepository:
        return self._repository

    @property
    def registry(self) -> dict[str, JobDefinition]:
        return self._registry

    @property
    def enabled(self) -> bool:
        return self._config.schedules.enabled

    @property
    def timezone(self) -> str:
        return self._config.schedules.timezone

    def _validate_cadences(self) -> None:
        """Parse every cron expression at construction, not at fire time.

        A cadence nobody could parse must fail loudly when the scheduler starts
        rather than becoming a job that quietly never runs — which looks
        identical to a job that is working perfectly and finding nothing to do.
        """
        for name, job in self._config.schedules.jobs.items():
            try:
                next_fire(job.cron, self._clock.now(), timezone=self.timezone)
            except CronError as exc:
                raise SchedulerError(
                    f"job {name!r} has an unusable cadence {job.cron!r}: {exc}"
                ) from exc

    # --- planning ----------------------------------------------------------
    def plan(self, *, at: datetime | None = None) -> list[JobPlan]:
        """What would run at ``at``, and why each other job would not.

        Read-only and side-effect free, which is what ``ops scheduler plan``
        uses: an operator can see the next hour's cadence without a single job
        firing.
        """
        now = at or self._clock.now()
        scheduled_for = now.replace(second=0, microsecond=0)
        plans: list[JobPlan] = []
        for name, schedule in sorted(self._config.schedules.jobs.items()):
            definition = self._registry[name]
            due = matches(schedule.cron, scheduled_for, timezone=self.timezone)
            plans.append(
                JobPlan(
                    job=name,
                    definition=definition,
                    schedule=schedule,
                    scheduled_for=scheduled_for,
                    due=due,
                    skip_reason=self._skip_reason(name, schedule, scheduled_for, due=due),
                    next_fire_at=next_fire(schedule.cron, now, timezone=self.timezone),
                )
            )
        return plans

    def _skip_reason(
        self, name: str, schedule: ScheduleJob, scheduled_for: datetime, *, due: bool
    ) -> JobSkipReason | None:
        """Why this job will not run, in the order the reasons matter."""
        if not schedule.enabled:
            return JobSkipReason.DISABLED
        if not due:
            return JobSkipReason.NOT_DUE
        if schedule.market_hours_only:
            session = self._market_state(scheduled_for)
            if session is not None:
                return session
        if self._already_ran(name, scheduled_for):
            return JobSkipReason.ALREADY_RAN
        return None

    def _market_state(self, moment: datetime) -> JobSkipReason | None:
        """``None`` when the market is open. A reason when it is not.

        A year the calendar does not cover answers ``CALENDAR_UNKNOWN`` and the
        job does not run. That is the Milestone 3 rule applied here: an
        unverified day must not silently become a trading day, and a
        market-hours job that fired on a guess would be evaluating positions
        against a closed book.
        """
        try:
            if self._calendar.is_open(moment):
                return None
        except MarketCalendarError:
            return JobSkipReason.CALENDAR_UNKNOWN
        return JobSkipReason.MARKET_CLOSED

    def _already_ran(self, job: str, scheduled_for: datetime) -> bool:
        """Whether this exact firing already has a completed record.

        The protection is the *persisted* run, never a process-local flag: two
        processes, or one process restarted mid-minute, must not both run the
        14:35 monitoring cycle. A run left ``RUNNING`` does not count as having
        run — that is the ``UNKNOWN`` case, handled by :meth:`recover`.
        """
        identifier = job_run_identifier(
            job=job, scheduled_for=scheduled_for, trading_mode=self._settings.trading_mode
        )
        existing = self._repository.job_run(identifier)
        return existing is not None and existing.status is not JobStatus.RUNNING

    # --- restart safety ----------------------------------------------------
    def recover(self, *, at: datetime | None = None) -> list[JobRun]:
        """Reclassify runs a dead process left behind. Never optimistically.

        A job run still recorded ``RUNNING`` when the scheduler starts is one
        whose completion was never observed. It becomes ``UNKNOWN``: not
        ``FAILED``, which would claim we know it did not finish, and not
        ``SUCCESS``, which would claim the opposite on no evidence at all.

        Whether such a firing may be re-run is
        ``schedules.rerun_unknown_jobs`` — safe here because every registered
        job is idempotent against persisted state, and a switch rather than an
        assumption because a job that stops being idempotent should be
        excludable without editing code.
        """
        now = at or self._clock.now()
        recovered: list[JobRun] = []
        for run in self._repository.unfinished_job_runs():
            completed = run.model_copy(
                update={
                    "status": JobStatus.UNKNOWN,
                    "finished_at": now,
                    "error_type": "IncompleteRun",
                    "error_message": (
                        "the process ended before this job recorded a result. Its work may "
                        "have completed, partly completed or not started; nothing here "
                        "assumes which. Every scheduled job is idempotent against persisted "
                        "state, so re-running is safe — and re-running is how the answer is "
                        "actually established"
                    ),
                    "duration_seconds": max((now - run.started_at).total_seconds(), 0.0),
                    "summary": "completion never recorded",
                }
            )
            self._repository.save_job_run(completed)
            recovered.append(completed)
            _logger.warning(
                "scheduler.recovered_unknown_job",
                job=run.job,
                job_run_id=run.job_run_id,
                scheduled_for=run.scheduled_for.isoformat(),
            )
        return recovered

    # --- the tick ----------------------------------------------------------
    def tick(self, *, at: datetime | None = None) -> SchedulerRun:
        """Run everything due at ``at``, each in isolation. One tick, one record."""
        started = self._clock.now()
        now = at or started
        scheduled_for = now.replace(second=0, microsecond=0)
        plans = self.plan(at=scheduled_for)

        if not self.enabled:
            return self._empty(
                scheduled_for,
                started,
                SchedulerRunStatus.BLOCKED,
                "schedules.enabled is false: every job is described and individually runnable "
                "from the CLI, and nothing fires on a cadence",
            )

        runs: list[JobRun] = []
        for plan in plans:
            if not plan.due:
                continue
            runs.append(self.run_job(plan.job, at=scheduled_for, plan=plan))

        finished = self._clock.now()
        result = SchedulerRun(
            scheduler_run_id=scheduler_run_identifier(
                scheduled_for=scheduled_for,
                jobs=[run.job for run in runs],
                trading_mode=self._settings.trading_mode,
            ),
            scheduled_for=scheduled_for,
            started_at=started,
            finished_at=finished,
            status=_status_of(runs),
            trading_mode=self._settings.trading_mode,
            timezone=self.timezone,
            runs=runs,
            duration_seconds=max((finished - started).total_seconds(), 0.0),
            orders_submitted=sum(run.orders_submitted for run in runs),
        )
        self._repository.save_scheduler_run(result)
        _logger.info(
            "scheduler.tick",
            scheduled_for=scheduled_for.isoformat(),
            status=result.status.value,
            jobs=len(runs),
            failed=len(result.failures),
            orders_submitted=result.orders_submitted,
        )
        return result

    def run_job(
        self, job: str, *, at: datetime | None = None, plan: JobPlan | None = None
    ) -> JobRun:
        """Run one job, bounded and isolated. Never raises for a job failure.

        The record is written **before** the work starts. A process that dies
        mid-job therefore leaves a ``RUNNING`` record, which the next start
        reads as *we do not know whether this finished* — rather than as
        silence, which is indistinguishable from never having tried.
        """
        if job not in self._registry:
            raise SchedulerError(f"no job named {job!r} is registered")

        now = at or self._clock.now()
        scheduled_for = now.replace(second=0, microsecond=0)
        schedule = self._config.schedules.jobs[job]
        definition = self._registry[job]
        resolved = plan or JobPlan(
            job=job,
            definition=definition,
            schedule=schedule,
            scheduled_for=scheduled_for,
            due=True,
            skip_reason=self._skip_reason(job, schedule, scheduled_for, due=True),
        )

        identifier = job_run_identifier(
            job=job, scheduled_for=scheduled_for, trading_mode=self._settings.trading_mode
        )
        started_at = self._clock.now()

        if resolved.skip_reason is JobSkipReason.ALREADY_RAN:
            # Return what that firing actually did rather than recording a
            # second, contradictory line about it. The stored run *is* the
            # record: writing a SKIPPED over a SUCCESS would erase the evidence
            # of what happened, which is exactly the rewrite the store refuses.
            existing = self._repository.job_run(identifier)
            if existing is not None:
                return existing

        if resolved.skip_reason is not None:
            return self._record(
                JobRun(
                    job_run_id=identifier,
                    job=job,
                    scheduled_for=scheduled_for,
                    started_at=started_at,
                    finished_at=started_at,
                    status=JobStatus.SKIPPED,
                    skip_reason=resolved.skip_reason,
                    trading_mode=self._settings.trading_mode,
                    duration_seconds=0.0,
                    summary=_skip_summary(resolved.skip_reason, job),
                )
            )

        self._record(
            JobRun(
                job_run_id=identifier,
                job=job,
                scheduled_for=scheduled_for,
                started_at=started_at,
                status=JobStatus.RUNNING,
                trading_mode=self._settings.trading_mode,
                summary="started",
            )
        )

        context = JobContext(
            settings=self._settings,
            config=self._config,
            clock=self._clock,
            scheduled_for=scheduled_for,
            data_root=self._data_root,
            services=self._services,
        )
        began = time.perf_counter()
        with self._span(job, identifier, scheduled_for):
            status, outcome, error = self._execute(definition, context, schedule.timeout_seconds)
        duration = time.perf_counter() - began
        finished_at = self._clock.now()

        run = JobRun(
            job_run_id=identifier,
            job=job,
            scheduled_for=scheduled_for,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            skip_reason=outcome.skipped if outcome is not None else None,
            trading_mode=self._settings.trading_mode,
            error_type=None if error is None else type(error).__name__,
            error_message=None if error is None else str(error)[:500],
            duration_seconds=duration,
            orders_submitted=outcome.orders_submitted if outcome is not None else 0,
            artifact_ids=list(outcome.artifact_ids) if outcome is not None else [],
            summary=(
                outcome.summary
                if outcome is not None
                else _failure_summary(status, error, schedule.timeout_seconds)
            ),
        )
        _logger.info(
            "scheduler.job",
            job=job,
            job_run_id=identifier,
            status=run.status.value,
            duration_seconds=round(duration, 3),
            orders_submitted=run.orders_submitted,
            error=run.error_type,
        )
        return self._record(run)

    def _execute(
        self, definition: JobDefinition, context: JobContext, timeout: float
    ) -> tuple[JobStatus, Any, BaseException | None]:
        """Run one job's work, bounded. Never lets an exception escape.

        A timeout produces ``UNKNOWN``, not ``FAILED``. Python cannot kill the
        thread, so the work may still be running: claiming it failed would be a
        statement nobody can support, and — worse — would invite a retry policy
        built on it. ``UNKNOWN`` is the honest state and the one that keeps the
        idempotency requirement visible.
        """
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix=definition.name) as pool:
            future = pool.submit(definition.run, context)
            try:
                outcome = future.result(timeout=timeout)
            except FutureTimeout as exc:
                # Deliberately not cancelled and deliberately not waited for:
                # the tick must move on to the jobs this one is not allowed to
                # block, and the work is idempotent if it does complete.
                pool.shutdown(wait=False, cancel_futures=False)
                return JobStatus.UNKNOWN, None, exc
            except Exception as exc:
                return JobStatus.FAILED, None, exc
        if outcome.skipped is not None:
            return JobStatus.SKIPPED, outcome, None
        return JobStatus.SUCCESS, outcome, None

    def _span(self, job: str, job_run_id: str, scheduled_for: datetime) -> Any:
        """A telemetry span around one job, or a no-op when telemetry is off.

        Imported lazily and wrapped so that *nothing* about telemetry can
        affect whether a job runs. If the observability package is absent,
        misconfigured or throwing, this returns a null context and the job
        proceeds exactly as it would have.
        """
        try:
            from trading_system.observability.tracing import operation

            return operation(
                "trading.workflow",
                attributes={
                    "trading.job.name": job,
                    "trading.job.run_id": job_run_id,
                    "trading.job.scheduled_for": scheduled_for.isoformat(),
                },
            )
        except Exception:  # pragma: no cover - telemetry must never matter
            from contextlib import nullcontext

            return nullcontext()

    def _record(self, run: JobRun) -> JobRun:
        self._repository.save_job_run(run)
        return run

    def _empty(
        self, scheduled_for: datetime, started: datetime, status: SchedulerRunStatus, detail: str
    ) -> SchedulerRun:
        finished = self._clock.now()
        result = SchedulerRun(
            scheduler_run_id=scheduler_run_identifier(
                scheduled_for=scheduled_for, jobs=[], trading_mode=self._settings.trading_mode
            ),
            scheduled_for=scheduled_for,
            started_at=started,
            finished_at=finished,
            status=status,
            trading_mode=self._settings.trading_mode,
            timezone=self.timezone,
            runs=[],
            duration_seconds=max((finished - started).total_seconds(), 0.0),
            orders_submitted=0,
            detail=detail,
        )
        self._repository.save_scheduler_run(result)
        return result

    # --- the loop ----------------------------------------------------------
    def serve(
        self,
        *,
        max_ticks: int | None = None,
        sleep: Any = time.sleep,
        stop: Any = None,
    ) -> list[SchedulerRun]:
        """Tick on the configured cadence until told to stop.

        ``max_ticks`` and the injected ``sleep`` are what make this testable
        without waiting: a test runs three ticks against a
        :class:`~trading_system.infrastructure.clock.FixedClock` and asserts
        exactly what fired. There is no wall-clock dependency anywhere in the
        decision path — only in how long this waits between them.

        :meth:`recover` runs first, once. A restart must classify what the
        previous process left behind before deciding what is due.
        """
        self.recover()
        ticks: list[SchedulerRun] = []
        interval = self._config.schedules.tick_seconds
        count = 0
        while max_ticks is None or count < max_ticks:
            if stop is not None and stop():
                break
            ticks.append(self.tick())
            count += 1
            if max_ticks is not None and count >= max_ticks:
                break
            sleep(interval)
        return ticks

    def upcoming(self, *, at: datetime | None = None, within_minutes: int = 60) -> list[JobPlan]:
        """Every job that will fire in the next ``within_minutes``. Read-only."""
        now = at or self._clock.now()
        horizon = now + timedelta(minutes=within_minutes)
        upcoming: list[JobPlan] = []
        for plan in self.plan(at=now):
            if plan.next_fire_at is not None and plan.next_fire_at <= horizon:
                upcoming.append(plan)
        return sorted(upcoming, key=lambda plan: (plan.next_fire_at or horizon, plan.job))


def _status_of(runs: Sequence[JobRun]) -> SchedulerRunStatus:
    """One tick's verdict, derived from its jobs.

    ``PARTIAL`` where some failed and some did not — because that is the
    *intended* behaviour of isolation, not a degraded state. A tick reported as
    ``FAILED`` because one of nine jobs raised would teach an operator to
    ignore the word.
    """
    executed = [run for run in runs if run.status is not JobStatus.SKIPPED]
    if not executed:
        return SchedulerRunStatus.IDLE
    failed = [run for run in executed if run.status in (JobStatus.FAILED, JobStatus.UNKNOWN)]
    if not failed:
        return SchedulerRunStatus.SUCCESS
    if len(failed) == len(executed):
        return SchedulerRunStatus.FAILED
    return SchedulerRunStatus.PARTIAL


def _skip_summary(reason: JobSkipReason, job: str) -> str:
    return {
        JobSkipReason.DISABLED: f"{job} is disabled in config/schedules.yaml",
        JobSkipReason.NOT_DUE: f"{job} is not due at this instant",
        JobSkipReason.MARKET_CLOSED: f"{job} runs during the session only and the market is closed",
        JobSkipReason.CALENDAR_UNKNOWN: (
            f"{job} runs during the session only and the market calendar does not cover this "
            f"date. An unverified day is not treated as a trading day"
        ),
        JobSkipReason.ALREADY_RAN: (
            f"{job} already has a completed record for this scheduled instant. The protection "
            f"is the stored run, not a flag in this process"
        ),
        JobSkipReason.NOT_IMPLEMENTED: f"{job} names an operation that is not built",
        JobSkipReason.TRADING_MODE_NOT_PERMITTED: (
            f"{job} is not permitted in the current trading mode"
        ),
        JobSkipReason.NOTHING_TO_DO: f"{job} found nothing to act on",
    }[reason]


def _failure_summary(status: JobStatus, error: BaseException | None, timeout: float) -> str:
    if status is JobStatus.UNKNOWN:
        return (
            f"exceeded its {timeout:g}s bound. Recorded as UNKNOWN rather than FAILED: the "
            f"work may still be in flight, and every scheduled job is idempotent against "
            f"persisted state so the next firing is safe"
        )
    return f"failed: {type(error).__name__ if error else 'unknown error'}"


#: Trading modes in which the scheduler will start at all. LIVE is absent, and
#: the exit-submission job refuses it separately — the two refusals are
#: deliberate duplication for the one irreversible action here.
PERMITTED_MODES: frozenset[TradingMode] = frozenset({TradingMode.DRY_RUN, TradingMode.PAPER})
