"""The scheduler: cadence, isolation, idempotency and restart safety.

Milestone 10 built ``ExitService.monitor`` and deliberately not the loop that
calls it. These are the tests that make the loop safe to leave running:

* a job runs when it is due, and not otherwise;
* a market-hours job does not run on a guess about an unverified day;
* a failing job does not stop an unrelated one;
* the same firing does not run twice, and the protection is *persisted state*
  rather than a flag in this process;
* a job whose completion was never recorded becomes ``UNKNOWN`` — not
  ``FAILED``, which claims we know it did not finish, and not ``SUCCESS``;
* an over-running job costs one job, not the tick.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.operations.conftest import BEFORE_OPEN, NOW, WEEKEND
from trading_system.domain.enums import (
    JobSkipReason,
    JobStatus,
    SchedulerRunStatus,
    TradingMode,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.operations.jobs import JobOutcome
from trading_system.operations.models import job_run_identifier

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_every_configured_job_has_an_implementation(build_scheduler) -> None:
    """A configured job with nothing behind it would look scheduled and never
    run — a failure mode that is invisible until somebody asks why nothing
    happened."""
    scheduler = build_scheduler()

    assert set(scheduler.registry) == set(scheduler._config.schedules.jobs)


def test_a_job_with_no_implementation_fails_at_build(build_scheduler, system_config) -> None:
    jobs = dict(system_config.schedules.jobs)
    jobs["invented_job"] = jobs["reconciliation"]
    config = system_config.model_copy(
        update={"schedules": system_config.schedules.model_copy(update={"jobs": jobs})}
    )

    with pytest.raises(KeyError, match="invented_job"):
        build_scheduler(config=config)


def test_exactly_one_registered_job_can_submit_an_order(build_scheduler) -> None:
    """The blast radius, asserted rather than documented."""
    scheduler = build_scheduler()
    submitters = [
        name for name, definition in scheduler.registry.items() if definition.can_submit_orders
    ]

    assert submitters == ["exit_management"]


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
def test_a_job_that_is_not_due_does_not_run(build_scheduler, recording_job) -> None:
    job = recording_job()
    scheduler = build_scheduler(jobs={"universe_refresh": job})

    scheduler.tick()

    assert job.call_count == 0


def test_a_due_job_runs(build_scheduler, recording_job, enabled_config) -> None:
    job = recording_job()
    scheduler = build_scheduler(jobs={"reconciliation": job}, config=enabled_config)

    # 14:35 matches the */10 reconciliation cadence? It does not — 35 is not a
    # multiple of 10 — so use the */5 position monitor, which does.
    scheduler = build_scheduler(jobs={"position_monitor": job}, config=enabled_config)
    result = scheduler.tick()

    assert job.call_count == 1
    assert any(run.job == "position_monitor" for run in result.runs)


def test_a_disabled_job_is_skipped_with_a_reason(build_scheduler, recording_job) -> None:
    job = recording_job()
    scheduler = build_scheduler(jobs={"exit_management": job})

    record = scheduler.run_job("exit_management")

    assert record.status is JobStatus.SKIPPED
    assert record.skip_reason is JobSkipReason.DISABLED
    assert job.call_count == 0


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------
def test_a_market_hours_job_does_not_run_outside_the_session(
    build_scheduler, recording_job, system_config
) -> None:
    job = recording_job()
    enabled = system_config.schedules.jobs["position_monitor"].model_copy(update={"enabled": True})
    jobs = dict(system_config.schedules.jobs) | {"position_monitor": enabled}
    config = system_config.model_copy(
        update={"schedules": system_config.schedules.model_copy(update={"jobs": jobs})}
    )
    scheduler = build_scheduler(
        jobs={"position_monitor": job}, config=config, clock=FixedClock(BEFORE_OPEN)
    )

    record = scheduler.run_job("position_monitor", at=BEFORE_OPEN)

    assert record.status is JobStatus.SKIPPED
    assert record.skip_reason is JobSkipReason.MARKET_CLOSED
    assert job.call_count == 0


def test_a_market_hours_job_does_not_run_at_the_weekend(build_scheduler, recording_job) -> None:
    job = recording_job()
    scheduler = build_scheduler(jobs={"position_monitor": job}, clock=FixedClock(WEEKEND))

    record = scheduler.run_job("position_monitor", at=WEEKEND)

    assert record.status is JobStatus.SKIPPED
    assert record.skip_reason is JobSkipReason.MARKET_CLOSED


def test_an_uncovered_calendar_year_is_not_treated_as_a_trading_day(
    build_scheduler, recording_job
) -> None:
    """The Milestone 3 rule, applied here: an unverified day must not silently
    become a trading day, and a market-hours job that fired on a guess would be
    evaluating positions against a closed book."""
    from datetime import UTC, datetime

    uncovered = datetime(2030, 8, 12, 14, 35, tzinfo=UTC)
    job = recording_job()
    scheduler = build_scheduler(jobs={"position_monitor": job}, clock=FixedClock(uncovered))

    record = scheduler.run_job("position_monitor", at=uncovered)

    assert record.status is JobStatus.SKIPPED
    assert record.skip_reason is JobSkipReason.CALENDAR_UNKNOWN
    assert job.call_count == 0


def test_a_round_the_clock_job_runs_outside_the_session(
    build_scheduler, recording_job, enabled_config
) -> None:
    """Reconciliation is most valuable exactly when nobody is watching."""
    job = recording_job()
    scheduler = build_scheduler(
        jobs={"reconciliation": job}, config=enabled_config, clock=FixedClock(BEFORE_OPEN)
    )

    record = scheduler.run_job("reconciliation", at=BEFORE_OPEN)

    assert record.status is JobStatus.SUCCESS
    assert job.call_count == 1


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
def test_a_failing_job_does_not_stop_an_unrelated_one(
    build_scheduler, recording_job, enabled_config
) -> None:
    """The claim the brief makes explicitly: research collection can fail all
    morning and reconciliation still runs."""
    failing = recording_job(raises=RuntimeError("the provider is down"))
    healthy = recording_job()
    # Both fire on the */5 cadence at 14:35, so this is a test about isolation
    # rather than about which job happened to be due.
    scheduler = build_scheduler(
        jobs={"operational_health": failing, "position_monitor": healthy},
        config=enabled_config,
    )

    result = scheduler.tick()

    assert failing.call_count == 1
    assert healthy.call_count == 1
    statuses = {run.job: run.status for run in result.runs}
    assert statuses["operational_health"] is JobStatus.FAILED
    assert statuses["position_monitor"] is JobStatus.SUCCESS


def test_a_partly_failing_tick_is_partial_rather_than_failed(
    build_scheduler, recording_job, enabled_config
) -> None:
    """Isolation working as intended is not a degraded state, and a tick
    reported FAILED because one of nine jobs raised would teach an operator to
    ignore the word."""
    scheduler = build_scheduler(
        jobs={
            "operational_health": recording_job(raises=RuntimeError("down")),
            "position_monitor": recording_job(),
        },
        config=enabled_config,
    )

    result = scheduler.tick()

    assert result.status is SchedulerRunStatus.PARTIAL


def test_a_failure_is_classified_rather_than_dumped(
    build_scheduler, recording_job, enabled_config
) -> None:
    """An operational record is read by a person deciding whether to intervene;
    the stack trace belongs in the log."""
    scheduler = build_scheduler(
        jobs={"position_monitor": recording_job(raises=ValueError("bad input"))},
        config=enabled_config,
    )

    record = scheduler.run_job("position_monitor")

    assert record.status is JobStatus.FAILED
    assert record.error_type == "ValueError"
    assert record.error_message == "bad input"
    assert "\n" not in (record.error_message or "")


def test_a_job_that_over_runs_is_unknown_rather_than_failed(
    build_scheduler, recording_job, system_config
) -> None:
    """Python cannot kill the thread, so the work may still be running.

    Claiming it failed would be a statement nobody can support — and, worse,
    would invite a retry policy built on it.
    """
    jobs = dict(system_config.schedules.jobs)
    jobs["reconciliation"] = jobs["reconciliation"].model_copy(
        update={"timeout_seconds": 0.05, "market_hours_only": False}
    )
    config = system_config.model_copy(
        update={"schedules": system_config.schedules.model_copy(update={"jobs": jobs})}
    )
    scheduler = build_scheduler(jobs={"reconciliation": recording_job(blocks=0.5)}, config=config)

    record = scheduler.run_job("reconciliation")

    assert record.status is JobStatus.UNKNOWN
    assert record.summary is not None
    assert "UNKNOWN rather than FAILED" in record.summary


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_the_same_firing_does_not_run_twice(build_scheduler, recording_job, enabled_config) -> None:
    job = recording_job()
    scheduler = build_scheduler(jobs={"position_monitor": job}, config=enabled_config)

    first = scheduler.run_job("position_monitor")
    second = scheduler.run_job("position_monitor")

    assert first.status is JobStatus.SUCCESS
    # The stored run comes back rather than a second, contradictory line about
    # the same firing. Writing a SKIPPED over a SUCCESS would erase what
    # actually happened.
    assert second.job_run_id == first.job_run_id
    assert second.status is JobStatus.SUCCESS
    assert job.call_count == 1


def test_the_duplicate_protection_is_persisted_not_process_local(
    build_scheduler, recording_job, enabled_config, ops_repository
) -> None:
    """Two processes waking for the same firing must not both run it.

    Simulated by building a *second* scheduler over the same store — which is
    exactly what a restarted process, or a second container, would be.
    """
    job = recording_job()
    first_process = build_scheduler(jobs={"position_monitor": job}, config=enabled_config)
    first_process.run_job("position_monitor")

    second_process = build_scheduler(
        jobs={"position_monitor": job}, config=enabled_config, repository=ops_repository
    )
    record = second_process.run_job("position_monitor")

    assert record.status is JobStatus.SUCCESS
    assert job.call_count == 1


def test_a_later_firing_runs_again(build_scheduler, recording_job, enabled_config) -> None:
    """Idempotency is per *firing*, not per job. The 14:40 cycle is a new one."""
    job = recording_job()
    scheduler = build_scheduler(jobs={"position_monitor": job}, config=enabled_config)

    scheduler.run_job("position_monitor", at=NOW)
    scheduler.run_job("position_monitor", at=NOW + timedelta(minutes=5))

    assert job.call_count == 2


def test_the_run_identity_comes_from_the_scheduled_instant(ops_settings) -> None:
    """Not from the start time. An id built from the start time would make
    every duplicate look new, which is the bug this prevents."""
    first = job_run_identifier(
        job="position_monitor", scheduled_for=NOW, trading_mode=TradingMode.PAPER
    )
    second = job_run_identifier(
        job="position_monitor", scheduled_for=NOW, trading_mode=TradingMode.PAPER
    )
    later = job_run_identifier(
        job="position_monitor",
        scheduled_for=NOW + timedelta(minutes=5),
        trading_mode=TradingMode.PAPER,
    )

    assert first == second
    assert first != later


# ---------------------------------------------------------------------------
# Restart safety
# ---------------------------------------------------------------------------
def test_a_run_is_recorded_before_the_work_starts(
    build_scheduler, recording_job, enabled_config, ops_repository
) -> None:
    """A process that dies mid-job must leave a question, not silence."""
    observed: list[str] = []

    def peek(context):
        stored = ops_repository.job_runs(job="position_monitor")
        observed.extend(run.status.value for run in stored)
        return JobOutcome(summary="ok")

    scheduler = build_scheduler(jobs={"position_monitor": peek}, config=enabled_config)
    scheduler.run_job("position_monitor")

    assert observed == ["RUNNING"]


def test_an_unfinished_run_is_reclassified_unknown_on_restart(
    build_scheduler, enabled_config, ops_repository
) -> None:
    def dies(context):
        raise SystemExit("the process ended")

    scheduler = build_scheduler(jobs={"position_monitor": dies}, config=enabled_config)
    with pytest.raises(SystemExit):
        scheduler.run_job("position_monitor")

    # A new process starts and asks what the last one left behind.
    restarted = build_scheduler(config=enabled_config, repository=ops_repository)
    recovered = restarted.recover()

    assert [run.status for run in recovered] == [JobStatus.UNKNOWN]
    assert recovered[0].error_type == "IncompleteRun"


def test_an_unfinished_run_is_never_assumed_to_have_succeeded(
    build_scheduler, enabled_config, ops_repository
) -> None:
    def dies(context):
        raise SystemExit("the process ended")

    scheduler = build_scheduler(jobs={"position_monitor": dies}, config=enabled_config)
    with pytest.raises(SystemExit):
        scheduler.run_job("position_monitor")

    restarted = build_scheduler(config=enabled_config, repository=ops_repository)
    recovered = restarted.recover()

    assert recovered[0].status is not JobStatus.SUCCESS
    assert recovered[0].status is not JobStatus.FAILED


def test_recovery_is_idempotent(build_scheduler, enabled_config, ops_repository) -> None:
    def dies(context):
        raise SystemExit("the process ended")

    scheduler = build_scheduler(jobs={"position_monitor": dies}, config=enabled_config)
    with pytest.raises(SystemExit):
        scheduler.run_job("position_monitor")

    restarted = build_scheduler(config=enabled_config, repository=ops_repository)
    assert len(restarted.recover()) == 1
    assert restarted.recover() == []


# ---------------------------------------------------------------------------
# The master switch and the mode
# ---------------------------------------------------------------------------
def test_a_disabled_scheduler_runs_nothing(build_scheduler, recording_job, enabled_config) -> None:
    job = recording_job()
    config = enabled_config.model_copy(
        update={"schedules": enabled_config.schedules.model_copy(update={"enabled": False})}
    )
    scheduler = build_scheduler(jobs={"position_monitor": job}, config=config)

    result = scheduler.tick()

    assert result.status is SchedulerRunStatus.BLOCKED
    assert job.call_count == 0


def test_a_tick_with_nothing_due_is_idle_rather_than_successful(build_scheduler) -> None:
    from datetime import UTC, datetime

    quiet = datetime(2026, 8, 10, 14, 37, tzinfo=UTC)  # not a multiple of 5 or 10
    scheduler = build_scheduler(clock=FixedClock(quiet))

    result = scheduler.tick(at=quiet)

    assert result.status is SchedulerRunStatus.IDLE


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def test_a_tick_reports_the_order_count_its_jobs_read(
    build_scheduler, recording_job, enabled_config
) -> None:
    """Read off the services, never asserted. The line is evidence."""
    scheduler = build_scheduler(jobs={"position_monitor": recording_job()}, config=enabled_config)

    result = scheduler.tick()

    assert result.orders_submitted == 0


def test_a_skipped_job_cannot_report_a_submitted_order(build_scheduler, recording_job) -> None:
    """The model refuses it: a job that did not run cannot have sent one."""
    from trading_system.operations.models import JobRun

    with pytest.raises(ValueError, match="did not run"):
        JobRun(
            job_run_id="jobrun-1",
            job="exit_management",
            scheduled_for=NOW,
            started_at=NOW,
            finished_at=NOW,
            status=JobStatus.SKIPPED,
            skip_reason=JobSkipReason.DISABLED,
            trading_mode=TradingMode.PAPER,
            orders_submitted=1,
        )


def test_the_tick_order_count_is_the_sum_of_its_jobs(
    build_scheduler, recording_job, enabled_config
) -> None:
    """Evidence, not a summary written by hand."""
    from trading_system.operations.models import SchedulerRun

    scheduler = build_scheduler(jobs={"position_monitor": recording_job()}, config=enabled_config)
    result = scheduler.tick()

    with pytest.raises(ValueError, match="submitted order"):
        SchedulerRun(
            scheduler_run_id="tick-1",
            scheduled_for=NOW,
            started_at=NOW,
            finished_at=NOW,
            status=result.status,
            trading_mode=TradingMode.PAPER,
            runs=list(result.runs),
            orders_submitted=3,
        )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def test_serve_runs_a_bounded_number_of_ticks(
    build_scheduler, recording_job, enabled_config
) -> None:
    """Bounded and with an injected sleep, so the loop is testable without
    waiting. There is no wall-clock dependency in the decision path."""
    slept: list[float] = []

    job = recording_job()
    scheduler = build_scheduler(jobs={"position_monitor": job}, config=enabled_config)
    ticks = scheduler.serve(max_ticks=3, sleep=slept.append)

    assert len(ticks) == 3
    # Two sleeps between three ticks; never one after the last.
    assert len(slept) == 2
    # Under a fixed clock every tick is the SAME firing, so the work happens
    # once. That is the duplicate protection doing its job inside the loop.
    assert job.call_count == 1


def test_serve_stops_when_asked(build_scheduler, recording_job, enabled_config) -> None:
    scheduler = build_scheduler(jobs={"position_monitor": recording_job()}, config=enabled_config)

    ticks = scheduler.serve(max_ticks=5, sleep=lambda _: None, stop=lambda: True)

    assert ticks == []


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def test_planning_has_no_side_effects(
    build_scheduler, recording_job, enabled_config, ops_repository
) -> None:
    job = recording_job()
    scheduler = build_scheduler(jobs={"position_monitor": job}, config=enabled_config)

    scheduler.plan()

    assert job.call_count == 0
    assert ops_repository.job_runs() == []


def test_the_plan_says_why_each_job_will_not_run(build_scheduler) -> None:
    scheduler = build_scheduler()
    plans = {plan.job: plan for plan in scheduler.plan()}

    assert plans["exit_management"].skip_reason is JobSkipReason.DISABLED
    assert plans["universe_refresh"].skip_reason is JobSkipReason.NOT_DUE
