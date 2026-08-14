"""The scheduled jobs: orchestration, and nothing else (Milestone 11).

Every job in this module is a *call to an existing service method*. There is no
trading logic here — no exit policy, no risk check, no sizing, no order
construction — and there must never be. Milestone 10 built ``ExitService.monitor``
deliberately without the loop that calls it; this is the loop, and the whole
point of separating them is that the decision stays somewhere it was tested.

.. code-block:: text

    scheduler  ->  JobDefinition.run()  ->  ExitService.monitor()   (M10)
                                        ->  ReconciliationService.run()  (M9)
                                        ->  PnLService.run()        (M11)
                                        ->  DataService / UniverseSelectionService

Four properties hold for every job here:

* **Idempotent against persisted state.** Running one twice moves nothing the
  first run did not. That is not a property of the job — it is a property of
  the service it calls, which is exactly why the job may not contain logic of
  its own. Nothing here uses a process-local flag as protection.
* **It reports its own order count.** Every job reads ``orders_submitted`` off
  whatever it invoked rather than asserting zero. Two jobs can be non-zero:
  ``exit_management`` when both switches are on, and nothing else in the
  registry has an order path at all.
* **A refusal is a first-class outcome.** ``SKIPPED`` with a reason, not an
  exception and not a silent success. ``NOT_IMPLEMENTED`` is one of those
  reasons and is used exactly once, honestly, for the specification's separate
  thesis monitor — which does not exist and is not faked.
* **It fails alone.** An exception is caught by the scheduler, classified, and
  recorded against this job. The next job runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_system.domain.enums import JobSkipReason, TradingMode
from trading_system.infrastructure.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.infrastructure.clock import Clock
    from trading_system.infrastructure.settings import Settings, SystemConfig

__all__ = [
    "JOB_BUILDERS",
    "JobContext",
    "JobDefinition",
    "JobOutcome",
    "build_registry",
]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class JobContext:
    """Everything a job is allowed to reach.

    Deliberately a bundle of *settings and factories* rather than of live
    services: a job that held a connected broker would be a job that holds a
    connection between firings, and Milestone 2's one-reliable-round-trip
    constraint makes that unsafe. Services are built per run, use a short-lived
    read-only connection where they need one, and are discarded.
    """

    settings: Settings
    config: SystemConfig
    clock: Clock
    scheduled_for: datetime
    #: Where every artifact this job reads or writes lives. Carried explicitly
    #: rather than left to ``project_root()``, so a scheduler rooted somewhere
    #: else — a test at ``tmp_path``, a container with a mounted volume — has
    #: jobs that write *there*. A job that silently used the installed
    #: package's own directory would be a scheduled process quietly editing a
    #: ledger nobody pointed it at.
    data_root: Path
    #: Overrides for tests and for the CLI, keyed by service name. A supplied
    #: service is used as-is; anything absent is constructed from settings.
    services: dict[str, Any]

    @property
    def root(self) -> Path:
        """The directory the data root sits in, which is what services take."""
        return self.data_root.parent

    def service(self, name: str, build: Callable[[], Any]) -> Any:
        """The named service, supplied or freshly built."""
        existing = self.services.get(name)
        if existing is not None:
            return existing
        return build()


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """What one job's work produced.

    ``skipped`` is a first-class result rather than an exception, because a job
    that deliberately did nothing is not an error and must not be counted as
    one. ``orders_submitted`` is read off the service, never asserted.
    """

    summary: str
    orders_submitted: int = 0
    artifact_ids: tuple[str, ...] = ()
    skipped: JobSkipReason | None = None

    @property
    def did_run(self) -> bool:
        return self.skipped is None


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """One registered job: its name, what it calls, and what it may do."""

    name: str
    description: str
    run: Callable[[JobContext], JobOutcome]
    #: Whether this job has any path to a broker order at all. Used by
    #: ``ops jobs`` to show the blast radius before anything runs, and by a
    #: test that asserts exactly one registered job can submit.
    can_submit_orders: bool = False


# ---------------------------------------------------------------------------
# The jobs
# ---------------------------------------------------------------------------
def _position_monitor(context: JobContext) -> JobOutcome:
    """Capture broker positions and evaluate the exit policy. Submits nothing.

    Calls :meth:`~trading_system.exit.service.ExitService.monitor` with
    ``authorized=False``, which is the same call ``positions monitor`` makes
    from the CLI. Evaluation and submission are separate jobs for the same
    reason they are separate commands: judging whether a position should close
    must not close one.
    """
    service = context.service("exit", lambda: _exit_service(context))
    run = service.monitor(as_of=context.scheduled_for, capture=True, authorized=False)
    result = run.result
    return JobOutcome(
        summary=(
            f"{len(result.evaluations)} position(s): {result.counts.waiting} waiting, "
            f"{result.counts.exiting} exiting, {result.counts.blocked} blocked"
        ),
        orders_submitted=result.orders_submitted,
        artifact_ids=(result.run_id,),
    )


def _exit_management(context: JobContext) -> JobOutcome:
    """Submit exits for positions Milestone 10 decided should close.

    The only job in this registry with a path to an order, and it needs **two**
    switches: ``schedules.yaml`` must set ``authorize_exits`` on this job *and*
    ``config/execution.yaml`` must set ``execution.enabled``. Neither implies
    the other, exactly as an entry needs both ``execution.enabled`` and an
    explicit ``--confirm``.

    The decision is Milestone 10's and the order is Milestone 8's. This
    function chooses nothing.
    """
    job = context.config.schedules.jobs.get("exit_management")
    if job is None or not job.authorize_exits:
        return JobOutcome(
            summary=(
                "exit_management is not authorised to submit in schedules.yaml, so nothing "
                "was sent. Evaluation happens in position_monitor"
            ),
            skipped=JobSkipReason.DISABLED,
        )
    if not context.config.execution.enabled:
        return JobOutcome(
            summary=(
                "execution.enabled is false in config/execution.yaml. Both switches are "
                "required and neither implies the other"
            ),
            skipped=JobSkipReason.DISABLED,
        )
    if context.settings.trading_mode is TradingMode.LIVE:
        return JobOutcome(
            summary=(
                "the trading mode is LIVE. Scheduled submission is refused: live trading is "
                "delivered behind a signed-off readiness checklist, not behind a cron entry"
            ),
            skipped=JobSkipReason.TRADING_MODE_NOT_PERMITTED,
        )

    service = context.service("exit", lambda: _exit_service(context))
    run = service.monitor(as_of=context.scheduled_for, capture=True, authorized=True)
    result = run.result
    return JobOutcome(
        summary=(
            f"{result.counts.exiting} position(s) triggered; "
            f"{result.orders_submitted} order(s) submitted"
        ),
        orders_submitted=result.orders_submitted,
        artifact_ids=(result.run_id, *result.exit_execution_ids),
    )


def _reconciliation(context: JobContext) -> JobOutcome:
    """Compare internal records against broker reality. Places no orders.

    Milestone 9's own guarantee carries: reconciliation *reports*. It cannot
    place, cancel or modify an order, every run prints zero submitted and zero
    corrective orders, and both counts are read off the broker rather than
    asserted here.
    """
    service = context.service("reconciliation", lambda: _reconciliation_service(context))
    run = service.run(as_of=context.scheduled_for)
    result = run.result
    return JobOutcome(
        summary=(
            f"{result.status.value}: {len(result.findings)} finding(s), "
            f"{run.corrective_orders} corrective order(s)"
        ),
        orders_submitted=run.orders_submitted,
        artifact_ids=(result.reconciliation_id,),
    )


def _pnl_settlement(context: JobContext) -> JobOutcome:
    """Compute realised results and settle the capital behind closed positions.

    Capital returns to the campaign on broker-confirmed closure and on nothing
    weaker. Idempotent by construction: results are content-addressed and
    settlement outcomes are deltas, so the second run over unchanged evidence
    returns nothing.
    """
    if not context.config.pnl.enabled:
        return JobOutcome(
            summary="pnl.enabled is false in config/pnl.yaml",
            skipped=JobSkipReason.DISABLED,
        )
    service = context.service("pnl", lambda: _pnl_service(context))
    run = service.run(as_of=context.scheduled_for)
    result = run.result
    if not result.positions_examined:
        return JobOutcome(
            summary="no confirmed-closed position is waiting for a result",
            skipped=JobSkipReason.NOTHING_TO_DO,
        )
    return JobOutcome(
        summary=(
            f"{result.results_computed} result(s), {result.results_unavailable} unavailable, "
            f"{result.settlements_applied} settled returning {result.capital_returned} "
            f"{result.currency}, {result.settlements_blocked} blocked"
        ),
        orders_submitted=0,
        artifact_ids=(result.run_id, *result.pnl_ids, *result.settlement_ids),
    )


def _operational_health(context: JobContext) -> JobOutcome:
    """Record trading health and observability health, and evaluate the alerts.

    Reads only, notifies only. The two health verdicts are computed from
    separate inputs and kept in separate fields — an unreachable Grafana
    degrades observability health and leaves trading health untouched.
    """
    service = context.service("operations", lambda: _operations_service(context))
    report = service.health(as_of=context.scheduled_for)
    alerts = service.evaluate_alerts(as_of=context.scheduled_for, health=report)
    return JobOutcome(
        summary=(
            f"trading {report.trading_status.value}, observability "
            f"{report.observability_status.value}, {len(alerts)} alert(s)"
        ),
        artifact_ids=(report.health_id, *(alert.alert_id for alert in alerts)),
    )


def _end_of_day_report(context: JobContext) -> JobOutcome:
    """The day's realised profit and loss, rolled up from the ledger.

    Real work over real artifacts: it aggregates whatever
    :mod:`trading_system.pnl` recorded for the exchange-local session. A day on
    which nothing closed produces no roll-up rather than a zero — a flat day
    and a day with no trades are different facts.
    """
    if not context.config.pnl.enabled:
        return JobOutcome(
            summary="pnl.enabled is false in config/pnl.yaml",
            skipped=JobSkipReason.DISABLED,
        )
    service = context.service("pnl", lambda: _pnl_service(context))
    daily = service.daily_rollup(as_of=context.scheduled_for)
    if daily is None:
        return JobOutcome(
            summary=(
                "no position closed in this session, so no daily result was recorded. A day "
                "with no trades is not a day that broke even"
            ),
            skipped=JobSkipReason.NOTHING_TO_DO,
        )
    return JobOutcome(
        summary=(
            f"{daily.session_date.isoformat()}: {daily.status.value}, "
            f"{daily.positions_closed} closed, realised "
            f"{daily.realized_pnl if daily.realized_pnl is not None else 'UNKNOWN'}"
        ),
        artifact_ids=(daily.daily_pnl_id,),
    )


def _data_collection(context: JobContext) -> JobOutcome:
    """Collect and persist market and option snapshots for the session."""
    service = context.service("data", lambda: _data_service(context))
    reports = service.collect_all()
    collected = sum(1 for report in reports if getattr(report, "stored", False))
    return JobOutcome(
        summary=f"{collected} of {len(reports)} collection(s) stored a snapshot",
        artifact_ids=tuple(
            snapshot_id
            for report in reports
            if (snapshot_id := getattr(report, "snapshot_id", None)) is not None
        ),
    )


def _universe_refresh(context: JobContext) -> JobOutcome:
    """Rebuild the candidate universe from stored data. Opens no broker."""
    service = context.service("universe", lambda: _universe_service(context))
    run = service.run(as_of=context.scheduled_for)
    result = run.result
    return JobOutcome(
        summary=f"{result.status.value}: {len(result.selected)} underlying(s) selected",
        artifact_ids=(result.run_id,),
    )


def _opportunity_scan(context: JobContext) -> JobOutcome:
    """The slow loop: research, strategy, contract, risk and allocation.

    Ends at an **authorisation**, never an order — there is no execution call
    in this function and no configuration that adds one. Ships disabled in
    ``schedules.yaml``: opening positions on a cadence is a decision an
    operator makes deliberately, not one inherited from a default.

    Each stage is a call to its own composition root. A stage that produces
    nothing ends the job early with a summary rather than an error: a universe
    that selected nothing is the ordinary answer, not a failure.
    """
    stages: list[str] = []
    artifacts: list[str] = []

    research = context.service("research", lambda: _research_service(context))
    research_run = research.run(as_of=context.scheduled_for)
    stages.append(f"research {research_run.result.status.value}")
    artifacts.append(research_run.result.run_id)
    if not research_run.result.reports:
        return JobOutcome(summary="; ".join(stages) + " — nothing researched", artifact_ids=())

    strategy = context.service("strategy", lambda: _strategy_service(context))
    strategy_run = strategy.run(run_id=research_run.result.run_id)
    stages.append(f"strategy {strategy_run.result.status.value}")
    artifacts.append(strategy_run.result.run_id)

    selector = context.service("contracts", lambda: _contract_service(context))
    contract_run = selector.select(run_id=strategy_run.result.run_id)
    stages.append(f"contracts {contract_run.result.status.value}")
    artifacts.append(contract_run.result.run_id)

    allocation = context.service("allocation", lambda: _allocation_service(context))
    allocation_run = allocation.run(contract_run_id=contract_run.result.run_id)
    stages.append(f"allocation {allocation_run.result.status.value}")
    artifacts.append(allocation_run.result.run_id)

    return JobOutcome(
        summary="; ".join(stages) + " — authorisations only, no order path",
        orders_submitted=0,
        artifact_ids=tuple(artifacts),
    )


def _thesis_monitor(context: JobContext) -> JobOutcome:
    """The specification's separate thesis monitor. **Not built.**

    Registered, disabled, and honest. Milestone 10 evaluates a stored thesis
    deterministically inside the exit policy and never returns ``WEAKENING`` —
    deciding a thesis has weakened without being falsified is a judgement, and
    no milestone has made it. A job that returned a verdict here would be
    fabricating one, so this records ``SKIPPED`` with ``NOT_IMPLEMENTED`` and
    the gap stays visible in ``ops jobs``.
    """
    return JobOutcome(
        summary=(
            "the separate thesis monitor (VALID / WEAKENING / INVALIDATED / UNKNOWN) is not "
            "built. Milestone 10 checks stored invalidation conditions deterministically as "
            "part of the exit policy; nothing here fabricates a verdict"
        ),
        skipped=JobSkipReason.NOT_IMPLEMENTED,
    )


#: Every job the scheduler knows how to run, by the name used in
#: ``config/schedules.yaml``. A cadence naming a job absent from this mapping
#: is a configuration error surfaced at registry build, never a job that
#: silently never fires.
JOB_BUILDERS: dict[str, tuple[str, Callable[[JobContext], JobOutcome], bool]] = {
    "data_collection": (
        "Collect and persist market/option snapshots during the session.",
        _data_collection,
        False,
    ),
    "universe_refresh": (
        "Rebuild the candidate underlying universe from stored data.",
        _universe_refresh,
        False,
    ),
    "opportunity_scan": (
        "Research, strategy, contract selection and allocation. Authorises; never sends.",
        _opportunity_scan,
        False,
    ),
    "position_monitor": (
        "Capture broker positions and evaluate the exit policy. Submits nothing.",
        _position_monitor,
        False,
    ),
    "exit_management": (
        "Submit exits Milestone 10 decided on. Needs execution.enabled AND authorize_exits.",
        _exit_management,
        True,
    ),
    "thesis_monitor": (
        "The specification's separate thesis monitor. NOT IMPLEMENTED.",
        _thesis_monitor,
        False,
    ),
    "reconciliation": (
        "Compare internal records against broker reality. Reports; never repairs.",
        _reconciliation,
        False,
    ),
    "pnl_settlement": (
        "Realised profit and loss, and settlement of confirmed-closed positions.",
        _pnl_settlement,
        False,
    ),
    "operational_health": (
        "Record trading and observability health, and evaluate the alert rules.",
        _operational_health,
        False,
    ),
    "end_of_day_report": (
        "Daily realised profit and loss roll-up from the P&L ledger.",
        _end_of_day_report,
        False,
    ),
}


def build_registry(config: SystemConfig) -> dict[str, JobDefinition]:
    """Every job named in the configuration, resolved to something callable.

    A configured job with no implementation is a :class:`KeyError` at build
    time rather than a job that silently never runs. That is the same choice
    the strategy registry makes about an unknown strategy, for the same reason:
    the failure mode of the alternative is invisible.
    """
    registry: dict[str, JobDefinition] = {}
    for name in config.schedules.jobs:
        if name not in JOB_BUILDERS:
            raise KeyError(
                f"config/schedules.yaml defines a job named {name!r} that no implementation "
                f"exists for. Known jobs: {', '.join(sorted(JOB_BUILDERS))}. A configured job "
                f"with nothing behind it would look scheduled and never run."
            )
        description, run, can_submit = JOB_BUILDERS[name]
        registry[name] = JobDefinition(
            name=name, description=description, run=run, can_submit_orders=can_submit
        )
    return registry


# ---------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------
#
# Imported inside each builder rather than at module scope. The scheduler must
# be importable — and ``ops jobs`` must be answerable — without constructing a
# broker, and an eager import here would put every service's import graph
# behind the mere act of listing the jobs.
def _exit_service(context: JobContext) -> Any:
    from trading_system.exit.service import ExitService

    return ExitService(
        settings=context.settings,
        config=context.config,
        clock=context.clock,
        root=context.root,
    )


def _reconciliation_service(context: JobContext) -> Any:
    from trading_system.reconciliation.service import ReconciliationService

    return ReconciliationService(
        settings=context.settings,
        config=context.config,
        clock=context.clock,
        root=context.root,
    )


def _pnl_service(context: JobContext) -> Any:
    from trading_system.pnl.service import PnLService

    return PnLService(
        settings=context.settings,
        config=context.config,
        clock=context.clock,
        root=context.root,
    )


def _operations_service(context: JobContext) -> Any:
    from trading_system.operations.service import OperationsService

    return OperationsService(
        settings=context.settings,
        config=context.config,
        clock=context.clock,
        root=context.root,
    )


def _data_service(context: JobContext) -> Any:
    from trading_system.data.service import DataService

    return DataService(settings=context.settings, config=context.config)


def _universe_service(context: JobContext) -> Any:
    from trading_system.universe.service import UniverseSelectionService

    return UniverseSelectionService(
        settings=context.settings, config=context.config, root=context.root
    )


def _research_service(context: JobContext) -> Any:
    from trading_system.research.service import ResearchService

    return ResearchService(
        settings=context.settings,
        config=context.config,
        clock=context.clock,
        root=context.root,
    )


def _strategy_service(context: JobContext) -> Any:
    from trading_system.strategies.service import StrategyService

    return StrategyService(
        settings=context.settings,
        config=context.config,
        clock=context.clock,
        root=context.root,
    )


def _contract_service(context: JobContext) -> Any:
    from trading_system.strategies.service import ContractSelectionService

    return ContractSelectionService(
        settings=context.settings,
        config=context.config,
        clock=context.clock,
        root=context.root,
    )


def _allocation_service(context: JobContext) -> Any:
    from trading_system.allocation.service import AllocationService

    return AllocationService(
        settings=context.settings,
        config=context.config,
        clock=context.clock,
        root=context.root,
    )
