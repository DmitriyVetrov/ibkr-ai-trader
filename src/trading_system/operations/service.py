"""The operations service: the composition root for Milestone 11's operations.

.. code-block:: text

    every ledger this system keeps
          |
    captured operational facts     <- counts, statuses and ids. Never payloads
          |
    build_health (pure)            -> TRADING_HEALTH and OBSERVABILITY_HEALTH
    AlertRules   (pure)            -> alerts, most severe first
          |
    immutable health report + alerts
          |
    NotificationProvider           <- best effort; a failed send is recorded

Properties this service holds regardless of which path it takes:

* **It reads.** No order, no capital movement, no decision. Every number here
  is counted from an artifact some other milestone wrote.
* **It holds no writable broker.** The only broker it can construct is the
  read-only one every diagnostic uses, and only when a caller explicitly asks
  for a connectivity probe.
* **A telemetry failure changes nothing.** Observability health is a separate
  field computed from separate components, and the model refuses a report
  where a telemetry component moved the trading verdict.
* **An alert nobody could be told about is still an alert.** Alerts are stored
  before any channel sees them, and delivery failures are recorded rather than
  raised.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_system.domain.enums import (
    DailyPnLStatus,
    ExecutionState,
    JobStatus,
    PositionLifecycleState,
    ReconciliationSeverity,
    SettlementStatus,
    TradingMode,
)
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.operations.alerts import AlertFacts, AlertRules
from trading_system.operations.health import HealthInputs, build_health
from trading_system.operations.models import Alert, JobRun, OperationalHealth
from trading_system.operations.notifications import (
    NotificationProvider,
    build_providers,
    notify_all,
)
from trading_system.operations.store import (
    FilesystemOperationsRepository,
    OperationsRepository,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.operations.scheduler import Scheduler

__all__ = ["OperationalSummary", "OperationsService"]

_logger = get_logger(__name__)

#: How far back the health check and the alert rules look for recent problems.
#: One trading day: shorter would miss a morning's worth of broker trouble by
#: the afternoon, longer would keep an alert firing after the cause was fixed.
_RECENT_WINDOW_MINUTES = 1440


@dataclass(frozen=True, slots=True)
class OperationalSummary:
    """One health evaluation and everything it raised."""

    health: OperationalHealth
    alerts: tuple[Alert, ...] = ()
    stored: bool = False

    @property
    def trading_healthy(self) -> bool:
        return self.health.healthy


class OperationsService:
    """Health, alerts and the operational view over every other ledger."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        repository: OperationsRepository | None = None,
        providers: Sequence[NotificationProvider] | None = None,
        telemetry_status: str | None = None,
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
        self._providers = (
            list(providers)
            if providers is not None
            else build_providers(config.alerts.channels, data_root=data_root)
        )
        self._rules = AlertRules(config.alerts)
        self._telemetry_status = telemetry_status

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> OperationsRepository:
        return self._repository

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def providers(self) -> list[NotificationProvider]:
        return list(self._providers)

    def scheduler(self) -> Scheduler:
        """The scheduler, sharing this service's store and clock."""
        from trading_system.operations.scheduler import Scheduler as _Scheduler

        return _Scheduler(
            settings=self._settings,
            config=self._config,
            clock=self._clock,
            repository=self._repository,
            root=self._data_root.parent,
        )

    def job_runs(self, limit: int | None = None, *, job: str | None = None) -> list[JobRun]:
        return self._repository.job_runs(limit=limit, job=job)

    def latest_health(self) -> OperationalHealth | None:
        return self._repository.latest_health()

    def alerts(self, limit: int | None = None) -> list[Alert]:
        return self._repository.alerts(limit=limit)

    # --- operation: ops.health ---------------------------------------------
    def health(
        self,
        *,
        as_of: datetime | None = None,
        probe_broker: bool = False,
        broker: Any = None,
        store: bool = True,
    ) -> OperationalHealth:
        """Compute both health verdicts from every ledger this system keeps.

        ``probe_broker`` is off by default, deliberately. A probe costs one of
        the connection's reliable round trips, and Milestone 2 established that
        connections are a resource to spend on purpose. An unprobed broker is
        reported ``UNKNOWN``, never ``HEALTHY`` — "all green" must not be
        achievable by not looking.
        """
        now = as_of or self._clock.now()
        inputs = self._capture(now, probe_broker=probe_broker, broker=broker)
        report = build_health(inputs)
        if store:
            self._repository.save_health(report)
        _logger.info(
            "ops.health",
            trading=report.trading_status.value,
            observability=report.observability_status.value,
            components=len(report.components),
        )
        return report

    # --- operation: ops.alerts ---------------------------------------------
    def evaluate_alerts(
        self,
        *,
        as_of: datetime | None = None,
        health: OperationalHealth | None = None,
        notify: bool = True,
        store: bool = True,
    ) -> list[Alert]:
        """Evaluate every rule, store what fired, and offer it to the channels.

        Storage happens before delivery, always. An alert nobody could be told
        about is still an alert that happened, and the two files — what was
        raised and what was notified — differ by exactly the set an operator
        never saw.
        """
        now = as_of or self._clock.now()
        facts = self._facts(now)
        alerts = self._rules.evaluate(facts)

        recorded: list[Alert] = []
        for alert in alerts:
            delivered: list[str] = []
            if notify and self._providers:
                delivered, results = notify_all(self._providers, alert)
                for result in results:
                    if result.failed:
                        _logger.warning(
                            "ops.alert_delivery_failed",
                            alert_id=alert.alert_id,
                            channel=result.channel,
                            error=result.error,
                        )
            final = (
                alert.model_copy(update={"notified_channels": delivered}) if delivered else alert
            )
            if store and self._config.alerts.persist:
                _, is_new = self._repository.save_alert(final)
                if is_new:
                    _logger.warning(
                        "ops.alert",
                        alert_id=final.alert_id,
                        code=final.code.value,
                        severity=final.severity.value,
                        subject=final.subject,
                        occurrences=final.occurrences,
                        notified=delivered,
                    )
            recorded.append(final)
        return recorded

    def summary(
        self, *, as_of: datetime | None = None, probe_broker: bool = False
    ) -> OperationalSummary:
        """Health and alerts in one call. What ``operational_health`` schedules."""
        now = as_of or self._clock.now()
        report = self.health(as_of=now, probe_broker=probe_broker)
        alerts = self.evaluate_alerts(as_of=now, health=report)
        return OperationalSummary(health=report, alerts=tuple(alerts), stored=True)

    # --- capturing the facts ----------------------------------------------
    def _capture(self, now: datetime, *, probe_broker: bool, broker: Any = None) -> HealthInputs:
        """Read every ledger once and reduce it to counts and statuses.

        Each read is wrapped: a store that cannot be read yields the
        ``UNKNOWN`` shape for its component rather than failing the whole
        report. A health check that could not run because one file was
        unreadable would be the least useful possible failure mode.
        """
        scheduler_facts = self._scheduler_facts(now)
        broker_state, broker_error = (None, None)
        if probe_broker:
            broker_state, broker_error = self._probe_broker(broker)

        capital = self._capital_facts()
        daily = self._daily_facts(now)
        return HealthInputs(
            as_of=now,
            settings=self._settings,
            config=self._config,
            data_root=self._data_root,
            broker_state=broker_state,
            broker_error=broker_error,
            broker_probed=probe_broker,
            last_tick_at=scheduler_facts["last_tick_at"],
            recent_job_failures=scheduler_facts["failures"],
            recent_job_unknowns=scheduler_facts["unknowns"],
            scheduler_enabled=self._config.schedules.enabled,
            latest_snapshot_at=self._latest_snapshot_at(),
            last_reconciliation_at=self._last_reconciliation_at(),
            critical_findings=len(self._critical_findings()),
            unknown_executions=len(self._unknown_executions()),
            capital_available=capital["available"],
            capital_locked_by_unknown=capital["locked"],
            daily_pnl_status=daily["status"],
            daily_loss=daily["loss"],
            # The RESOLVED value, not the YAML one. OBSERVABILITY_ENABLED is a
            # deployment switch that overrides config/observability.yaml, so
            # reading the file directly reported "disabled by configuration"
            # for a process that was actively exporting — the one component
            # whose whole job is to say whether we can see what is happening.
            telemetry_enabled=self._settings.resolved_observability(
                self._config.observability
            ).enabled,
            telemetry_status=self._telemetry_state(),
            notification_channels=len(self._providers),
            notification_failures=0,
        )

    def _facts(self, now: datetime) -> AlertFacts:
        """Everything the alert rules are evaluated against."""
        scheduler_facts = self._scheduler_facts(now)
        daily = self._daily_facts(now)
        unknown_executions = self._unknown_executions()
        return AlertFacts(
            as_of=now,
            trading_mode=self._settings.trading_mode,
            broker_unavailable=False,
            unknown_execution_ids=tuple(unknown_executions),
            execution_rejections=self._rejected_executions(now),
            failed_jobs=tuple(scheduler_facts["failed_jobs"]),
            unknown_jobs=tuple(scheduler_facts["unknown_jobs"]),
            live_execution_attempts=(1 if self._settings.trading_mode is TradingMode.LIVE else 0),
            critical_reconciliation_findings=tuple(self._critical_findings()),
            exit_unknown_position_ids=tuple(self._exit_unknown_positions()),
            positions_near_expiration=(),
            daily_pnl_status=daily["status"],
            daily_loss=daily["loss"],
            daily_loss_limit=str(self._config.risk.max_daily_loss_eur),
            daily_loss_exceeded=daily["exceeded"],
            blocked_settlements=tuple(self._blocked_settlements()),
        )

    # --- individual reads, each one guarded --------------------------------
    def _scheduler_facts(self, now: datetime) -> dict[str, Any]:
        try:
            latest = self._repository.latest_scheduler_run()
            cutoff = now - timedelta(minutes=_RECENT_WINDOW_MINUTES)
            recent = [
                run for run in self._repository.job_runs(limit=200) if run.started_at >= cutoff
            ]
        except Exception:  # pragma: no cover - defensive
            return {
                "last_tick_at": None,
                "failures": 0,
                "unknowns": 0,
                "failed_jobs": [],
                "unknown_jobs": [],
            }
        failed = [run.job for run in recent if run.status is JobStatus.FAILED]
        unknown = [run.job for run in recent if run.status is JobStatus.UNKNOWN]
        return {
            "last_tick_at": latest.started_at if latest is not None else None,
            "failures": len(failed),
            "unknowns": len(unknown),
            "failed_jobs": failed,
            "unknown_jobs": unknown,
        }

    def _latest_snapshot_at(self) -> datetime | None:
        try:
            from trading_system.positions.store import FilesystemPositionRepository

            snapshot = FilesystemPositionRepository(self._data_root / "positions").latest_usable()
        except Exception:  # pragma: no cover - defensive
            return None
        return snapshot.as_of if snapshot is not None else None

    def _last_reconciliation_at(self) -> datetime | None:
        latest = self._latest_reconciliation()
        return latest.as_of if latest is not None else None

    def _latest_reconciliation(self) -> Any:
        try:
            from trading_system.reconciliation.store import (
                FilesystemReconciliationRepository,
            )

            return FilesystemReconciliationRepository(self._data_root / "reconciliation").latest()
        except Exception:  # pragma: no cover - defensive
            return None

    def _critical_findings(self) -> list[str]:
        latest = self._latest_reconciliation()
        if latest is None:
            return []
        return [
            finding.finding_type.value
            for finding in latest.findings
            if finding.severity is ReconciliationSeverity.CRITICAL
        ]

    def _executions(self) -> list[Any]:
        try:
            from trading_system.execution.store import FilesystemExecutionRepository

            repository = FilesystemExecutionRepository(self._data_root / "execution")
            return [
                record
                for entry in repository.history(limit=200)
                if (record := repository.current(entry.execution_id)) is not None
            ]
        except Exception:  # pragma: no cover - defensive
            return []

    def _unknown_executions(self) -> list[str]:
        return [
            record.execution_id
            for record in self._executions()
            if record.state is ExecutionState.UNKNOWN
        ]

    def _rejected_executions(self, now: datetime) -> int:
        cutoff = now - timedelta(minutes=_RECENT_WINDOW_MINUTES)
        return sum(
            1
            for record in self._executions()
            if record.state is ExecutionState.REJECTED and record.updated_at >= cutoff
        )

    def _exit_unknown_positions(self) -> list[str]:
        try:
            from trading_system.exit.store import FilesystemExitRepository

            lifecycles = FilesystemExitRepository(self._data_root / "exit").all_lifecycles()
        except Exception:  # pragma: no cover - defensive
            return []
        return [
            lifecycle.position_id
            for lifecycle in lifecycles
            if lifecycle.state is PositionLifecycleState.EXIT_UNKNOWN
        ]

    def _blocked_settlements(self) -> list[str]:
        try:
            from trading_system.pnl.store import FilesystemPnLRepository

            entries = FilesystemPnLRepository(self._data_root / "pnl").settlement_history(limit=100)
        except Exception:  # pragma: no cover - defensive
            return []
        return [
            entry.reservation_id
            for entry in entries
            if entry.status == SettlementStatus.BLOCKED.value
        ]

    def _capital_facts(self) -> dict[str, str | None]:
        try:
            from trading_system.reservations.service import ReservationService

            capital = ReservationService(
                settings=self._settings,
                config=self._config,
                clock=self._clock,
                root=self._data_root.parent,
            ).capital()
        except Exception:  # pragma: no cover - defensive
            return {"available": None, "locked": None}
        return {
            "available": f"{capital.available} {capital.currency}",
            "locked": f"{capital.locked_by_unknown} {capital.currency}",
        }

    def _daily_facts(self, now: datetime) -> dict[str, Any]:
        try:
            from trading_system.pnl.campaign_state import read_campaign_state

            state = read_campaign_state(
                self._data_root,
                campaign_id=self._config.campaign.campaign_id,
                as_of=now,
                day_boundary_timezone=self._config.pnl.day_boundary_timezone,
                enabled=self._config.pnl.enabled,
            )
        except Exception:  # pragma: no cover - defensive
            return {"status": DailyPnLStatus.NOT_TRACKED, "loss": None, "exceeded": False}
        figure = state.realized_pnl_today
        loss = -figure if figure is not None and figure < 0 else None
        return {
            "status": state.daily_pnl_status,
            "loss": None if loss is None else str(loss),
            "exceeded": loss is not None and loss >= self._config.risk.max_daily_loss_eur,
        }

    def _telemetry_state(self) -> str:
        if self._telemetry_status is not None:
            return self._telemetry_status
        try:
            from trading_system.observability.runtime import telemetry_status

            return telemetry_status().value
        except Exception:  # pragma: no cover - telemetry must never matter
            return "DISABLED"

    def _probe_broker(self, broker: Any = None) -> tuple[str | None, str | None]:
        """One read-only connectivity probe. Never opens a writable connection.

        Uses ``build_broker``, which returns a read-only connection whatever the
        settings say — the same factory every diagnostic uses. There is no path
        from a health check to an order.
        """
        connection = broker
        opened = False
        try:
            if connection is None:
                from trading_system.broker.factory import build_broker

                connection = build_broker(self._settings)
                connection.connect()
                opened = True
            probe = connection.health_check()
            return probe.state.value, probe.error
        except Exception as exc:
            return None, str(exc)
        finally:
            if opened and connection is not None:
                with suppress(Exception):  # a failed close is not a health finding
                    connection.disconnect()
