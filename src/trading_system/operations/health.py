"""Operational health, with trading and observability kept apart (Milestone 11).

The central claim of this module is a *separation*, and it is worth stating
before the code: **an unreachable Grafana is not a trading fault.** A system
that reported it as one would, after the third time, teach its operators to
ignore exactly the banner that means a broker is unreachable or a
reconciliation is in dispute. So there are two verdicts, computed from two
disjoint sets of components, and the model refuses a report where a telemetry
component moved the trading one.

.. code-block:: text

    TRADING_HEALTH            OBSERVABILITY_HEALTH
      application               telemetry exporter
      configuration             notification channels
      storage
      broker (optional probe)
      data
      scheduler
      reconciliation
      capital
      risk state

Every check here is a **read**. Nothing in this module opens a writable broker,
places an order, moves capital or changes a decision — and the one check that
touches a broker at all does so through the same read-only factory every
diagnostic uses, and only when explicitly asked.

``UNKNOWN`` is a status, and it outranks ``HEALTHY``. A component nobody
checked is not a component that is fine, and "all green" must never be
achievable by not looking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    DailyPnLStatus,
    ExecutionState,
    HealthComponent,
    HealthDomain,
    HealthStatus,
    JobStatus,
    ReconciliationSeverity,
    TradingMode,
)
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import (
    ConfigError,
    Settings,
    SystemConfig,
    load_config,
)
from trading_system.operations.models import ComponentHealth, OperationalHealth

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = ["HealthInputs", "build_health", "health_identifier"]

_logger = get_logger(__name__)


def health_identifier(*, as_of: datetime, trading: str, observability: str) -> str:
    """Derive one report's identity from the instant and the two verdicts."""
    digest = stable_hash(["OPERATIONAL_HEALTH", as_of.isoformat(), trading, observability])
    return f"health-{digest[:20]}"


@dataclass(frozen=True, slots=True)
class HealthInputs:
    """Captured state one health report is computed from.

    A frozen bundle rather than a set of live lookups, for the same reason the
    risk engine takes an ``AccountSnapshot``: a health verdict that fetched its
    own inputs could not be reproduced, and the interesting reports are the
    ones somebody wants to reproduce.
    """

    as_of: datetime
    settings: Settings
    config: SystemConfig
    data_root: Path

    #: Whether the configuration actually loaded, and what went wrong if not.
    config_error: str | None = None
    #: Broker probe result, when one was requested. ``None`` means not probed,
    #: which is reported as ``UNKNOWN`` rather than as healthy.
    broker_state: str | None = None
    broker_error: str | None = None
    broker_probed: bool = False

    #: Scheduler facts, read from the operations store.
    last_tick_at: datetime | None = None
    recent_job_failures: int = 0
    recent_job_unknowns: int = 0
    scheduler_enabled: bool = True

    #: Data layer facts.
    latest_snapshot_at: datetime | None = None

    #: Reconciliation facts.
    last_reconciliation_at: datetime | None = None
    critical_findings: int = 0

    #: Capital facts. Amounts are *counts and statuses* where possible;
    #: the committed figure is included because "why can I not open a
    #: position" is unanswerable without it.
    unknown_executions: int = 0
    capital_available: str | None = None
    capital_locked_by_unknown: str | None = None

    #: Daily result facts.
    daily_pnl_status: DailyPnLStatus = DailyPnLStatus.NOT_TRACKED
    daily_loss: str | None = None

    #: Telemetry facts. Never influences trading health.
    telemetry_enabled: bool = False
    telemetry_status: str = "DISABLED"
    telemetry_detail: str | None = None
    notification_channels: int = 0
    notification_failures: int = 0


def build_health(inputs: HealthInputs) -> OperationalHealth:
    """Compute both verdicts from captured state. Pure and reproducible."""
    components = [
        _application(inputs),
        _configuration(inputs),
        _storage(inputs),
        _broker(inputs),
        _data(inputs),
        _scheduler(inputs),
        _reconciliation(inputs),
        _capital(inputs),
        _risk_state(inputs),
        _telemetry(inputs),
        _notifications(inputs),
    ]
    trading = _worst(
        component.status for component in components if component.domain is HealthDomain.TRADING
    )
    observability = _worst(
        component.status
        for component in components
        if component.domain is HealthDomain.OBSERVABILITY
    )
    return OperationalHealth(
        health_id=health_identifier(
            as_of=inputs.as_of, trading=trading.value, observability=observability.value
        ),
        as_of=inputs.as_of,
        trading_status=trading,
        observability_status=observability,
        trading_mode=inputs.settings.trading_mode,
        application_version=_application_version(),
        config_version=(
            inputs.config.application.config_version if inputs.config_error is None else "unknown"
        ),
        components=components,
        detail=(
            "trading health and observability health are computed from disjoint components. "
            "A telemetry backend that is down degrades the second and cannot move the first"
        ),
    )


# ---------------------------------------------------------------------------
# The trading domain
# ---------------------------------------------------------------------------
def _application(inputs: HealthInputs) -> ComponentHealth:
    """The process itself, and the mode it believes it is in."""
    mode = inputs.settings.trading_mode
    status = HealthStatus.HEALTHY
    summary = f"running in {mode.value}"
    if mode is TradingMode.LIVE:
        # Not a failure — LIVE is a configured, guarded state — but it is the
        # single most important thing on this report and must not read green.
        status = HealthStatus.DEGRADED
        summary = "running in LIVE. Every irreversible action needs its own explicit guard"
    return ComponentHealth(
        component=HealthComponent.APPLICATION,
        domain=HealthDomain.TRADING,
        status=status,
        summary=summary,
        facts={
            "trading_mode": mode.value,
            "version": _application_version(),
            "broker_read_only": str(inputs.settings.ibkr_read_only),
        },
    )


def _configuration(inputs: HealthInputs) -> ComponentHealth:
    """Whether the policy in force actually loaded."""
    if inputs.config_error is not None:
        return ComponentHealth(
            component=HealthComponent.CONFIGURATION,
            domain=HealthDomain.TRADING,
            status=HealthStatus.BLOCKED,
            summary="configuration failed to load",
            detail=inputs.config_error,
        )
    return ComponentHealth(
        component=HealthComponent.CONFIGURATION,
        domain=HealthDomain.TRADING,
        status=HealthStatus.HEALTHY,
        summary=f"loaded, version {inputs.config.application.config_version}",
        facts={
            "config_version": inputs.config.application.config_version,
            "execution_enabled": str(inputs.config.execution.enabled),
            "exit_enabled": str(inputs.config.exit.enabled),
            "scheduler_enabled": str(inputs.config.schedules.enabled),
        },
    )


def _storage(inputs: HealthInputs) -> ComponentHealth:
    """Whether the artifact store is present and writable.

    Blocking rather than degrading when it is not: every safety property in
    this system rests on being able to *record* what happened, and a system
    that could trade but not write its own execution ledger is the worst
    combination available.
    """
    root = inputs.data_root
    if not root.is_dir():
        return ComponentHealth(
            component=HealthComponent.STORAGE,
            domain=HealthDomain.TRADING,
            status=HealthStatus.UNAVAILABLE,
            summary=f"the data root {root} does not exist",
            detail=(
                "no artifact could be written. Every safety property here rests on recording "
                "what happened, so this blocks rather than degrades"
            ),
        )
    probe = root / ".health-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return ComponentHealth(
            component=HealthComponent.STORAGE,
            domain=HealthDomain.TRADING,
            status=HealthStatus.BLOCKED,
            summary="the data root is not writable",
            detail=str(exc),
        )
    return ComponentHealth(
        component=HealthComponent.STORAGE,
        domain=HealthDomain.TRADING,
        status=HealthStatus.HEALTHY,
        summary=f"writable at {root}",
        facts={"root": str(root)},
    )


def _broker(inputs: HealthInputs) -> ComponentHealth:
    """Broker connectivity, only when it was actually probed.

    ``UNKNOWN`` when it was not. Reporting an unprobed broker as healthy would
    make "all green" achievable by not looking, which is the failure this
    status exists to prevent. Health does not probe by default because a probe
    costs a connection, and Milestone 2 established that connections are a
    resource to spend deliberately.
    """
    if not inputs.broker_probed:
        return ComponentHealth(
            component=HealthComponent.BROKER,
            domain=HealthDomain.TRADING,
            status=HealthStatus.UNKNOWN,
            summary="not probed",
            detail=(
                "health does not open a broker connection by default. A probe costs one of "
                "the connection's reliable round trips, so it is requested explicitly"
            ),
            facts={"backend": inputs.settings.resolved_broker_backend.value},
        )
    if inputs.broker_error is not None or inputs.broker_state not in ("CONNECTED", "READY"):
        return ComponentHealth(
            component=HealthComponent.BROKER,
            domain=HealthDomain.TRADING,
            status=HealthStatus.UNAVAILABLE,
            summary=f"broker is {inputs.broker_state or 'unreachable'}",
            detail=inputs.broker_error,
            facts={"backend": inputs.settings.resolved_broker_backend.value},
        )
    return ComponentHealth(
        component=HealthComponent.BROKER,
        domain=HealthDomain.TRADING,
        status=HealthStatus.HEALTHY,
        summary=f"broker is {inputs.broker_state}",
        facts={
            "backend": inputs.settings.resolved_broker_backend.value,
            "read_only": str(inputs.settings.ibkr_read_only),
        },
    )


def _data(inputs: HealthInputs) -> ComponentHealth:
    """Whether the position ledger has seen the broker recently enough.

    Degraded rather than unavailable when it is stale: stale data does not stop
    the system deciding correctly, because every stage that consumes it already
    refuses stale input on its own. This is an operator's early warning, not a
    second enforcement of a limit that is enforced elsewhere.
    """
    if inputs.latest_snapshot_at is None:
        return ComponentHealth(
            component=HealthComponent.DATA,
            domain=HealthDomain.TRADING,
            status=HealthStatus.UNKNOWN,
            summary="no broker position snapshot has been captured",
            detail=(
                "this is not an empty account: it is an absence of observation. Run "
                "'positions snapshot' or let the scheduled monitor do it"
            ),
        )
    age = (inputs.as_of - inputs.latest_snapshot_at).total_seconds()
    stale = age > inputs.config.positions.snapshot.max_age_seconds
    return ComponentHealth(
        component=HealthComponent.DATA,
        domain=HealthDomain.TRADING,
        status=HealthStatus.DEGRADED if stale else HealthStatus.HEALTHY,
        summary=(
            f"the newest broker snapshot is {int(age)}s old"
            + (" — beyond the configured window" if stale else "")
        ),
        facts={
            "age_seconds": str(int(age)),
            "max_age_seconds": str(inputs.config.positions.snapshot.max_age_seconds),
        },
    )


def _scheduler(inputs: HealthInputs) -> ComponentHealth:
    """Whether the loop is running, and whether its jobs are succeeding."""
    if not inputs.scheduler_enabled:
        return ComponentHealth(
            component=HealthComponent.SCHEDULER,
            domain=HealthDomain.TRADING,
            status=HealthStatus.DEGRADED,
            summary="the scheduler is disabled in config/schedules.yaml",
            detail=(
                "jobs are still individually runnable from the CLI; nothing fires on a cadence"
            ),
        )
    if inputs.last_tick_at is None:
        return ComponentHealth(
            component=HealthComponent.SCHEDULER,
            domain=HealthDomain.TRADING,
            status=HealthStatus.UNKNOWN,
            summary="no scheduler tick has been recorded",
        )
    age = (inputs.as_of - inputs.last_tick_at).total_seconds()
    # Three missed ticks is the threshold: one is a slow job, two is bad luck,
    # three is a loop that has stopped.
    stalled = age > inputs.config.schedules.tick_seconds * 3
    problems = inputs.recent_job_failures + inputs.recent_job_unknowns
    status = HealthStatus.HEALTHY
    if stalled:
        status = HealthStatus.UNAVAILABLE
    elif problems:
        status = HealthStatus.DEGRADED
    return ComponentHealth(
        component=HealthComponent.SCHEDULER,
        domain=HealthDomain.TRADING,
        status=status,
        summary=(
            f"last tick {int(age)}s ago; {inputs.recent_job_failures} failed and "
            f"{inputs.recent_job_unknowns} unresolved job(s) recently"
        ),
        facts={
            "last_tick_age_seconds": str(int(age)),
            "tick_seconds": str(inputs.config.schedules.tick_seconds),
            "recent_failures": str(inputs.recent_job_failures),
            "recent_unknowns": str(inputs.recent_job_unknowns),
        },
    )


def _reconciliation(inputs: HealthInputs) -> ComponentHealth:
    """Whether our records and the account currently agree.

    A critical finding **blocks**. Milestone 9's rule carried forward: a
    discrepancy between internal state and IBKR stops new executions until
    somebody resolves it, and the health report says so in the status rather
    than only in a count.
    """
    if inputs.last_reconciliation_at is None:
        return ComponentHealth(
            component=HealthComponent.RECONCILIATION,
            domain=HealthDomain.TRADING,
            status=HealthStatus.UNKNOWN,
            summary="no reconciliation has been recorded",
        )
    age = (inputs.as_of - inputs.last_reconciliation_at).total_seconds()
    if inputs.critical_findings:
        return ComponentHealth(
            component=HealthComponent.RECONCILIATION,
            domain=HealthDomain.TRADING,
            status=HealthStatus.BLOCKED,
            summary=f"{inputs.critical_findings} critical finding(s) outstanding",
            detail=(
                "our records and the broker disagree about something that matters. New "
                "executions must not proceed until this is resolved; reconciliation reports "
                "and never repairs, so resolution is a person's decision"
            ),
            facts={"critical_findings": str(inputs.critical_findings)},
        )
    return ComponentHealth(
        component=HealthComponent.RECONCILIATION,
        domain=HealthDomain.TRADING,
        status=HealthStatus.HEALTHY,
        summary=f"agreed {int(age)}s ago, no critical findings",
        facts={"age_seconds": str(int(age))},
    )


def _capital(inputs: HealthInputs) -> ComponentHealth:
    """What the campaign's money is doing, and what is holding it.

    ``locked_by_unknown`` is the figure an operator most needs and the one most
    easily mistaken for available money, so it gets its own line here exactly
    as it does in the Milestone 9 capital report.
    """
    if inputs.unknown_executions:
        return ComponentHealth(
            component=HealthComponent.CAPITAL,
            domain=HealthDomain.TRADING,
            status=HealthStatus.DEGRADED,
            summary=(
                f"{inputs.unknown_executions} unresolved execution(s) are holding "
                f"{inputs.capital_locked_by_unknown or 'capital'}"
            ),
            detail=(
                "an execution whose outcome was never learned may be a live order. Its "
                "capital stays locked and is resolved by observing the broker — "
                "'execution explain --resolve' or a reconciliation run"
            ),
            facts={
                "unknown_executions": str(inputs.unknown_executions),
                "available": inputs.capital_available or "unknown",
            },
        )
    return ComponentHealth(
        component=HealthComponent.CAPITAL,
        domain=HealthDomain.TRADING,
        status=HealthStatus.HEALTHY,
        summary=f"available {inputs.capital_available or 'unknown'}",
        facts={"available": inputs.capital_available or "unknown"},
    )


def _risk_state(inputs: HealthInputs) -> ComponentHealth:
    """Whether the day's realised result can be measured at all.

    ``UNKNOWN`` degrades, deliberately. A day whose figure cannot be computed
    is not a day with no losses, and an operator should be able to see that
    distinction on the health report rather than discovering it when the risk
    engine refuses a trade.
    """
    status_map = {
        DailyPnLStatus.TRACKED: HealthStatus.HEALTHY,
        DailyPnLStatus.UNKNOWN: HealthStatus.DEGRADED,
        DailyPnLStatus.NOT_TRACKED: HealthStatus.UNKNOWN,
    }
    summaries = {
        DailyPnLStatus.TRACKED: f"today's realised loss is {inputs.daily_loss or '0'}",
        DailyPnLStatus.UNKNOWN: (
            "positions closed today and at least one produced no usable result, so the day's "
            "figure is unknown. That is not zero loss"
        ),
        DailyPnLStatus.NOT_TRACKED: "no realised result has been recorded for today",
    }
    return ComponentHealth(
        component=HealthComponent.RISK_STATE,
        domain=HealthDomain.TRADING,
        status=status_map[inputs.daily_pnl_status],
        summary=summaries[inputs.daily_pnl_status],
        facts={
            "daily_pnl_status": inputs.daily_pnl_status.value,
            "max_daily_loss": str(inputs.config.risk.max_daily_loss_eur),
        },
    )


# ---------------------------------------------------------------------------
# The observability domain
# ---------------------------------------------------------------------------
def _telemetry(inputs: HealthInputs) -> ComponentHealth:
    """The telemetry side channel. **Never influences trading health.**"""
    if not inputs.telemetry_enabled:
        return ComponentHealth(
            component=HealthComponent.TELEMETRY,
            domain=HealthDomain.OBSERVABILITY,
            status=HealthStatus.HEALTHY,
            summary="disabled by configuration",
            detail=(
                "telemetry is optional and ships off. The trading system runs identically "
                "with or without it"
            ),
            facts={"status": inputs.telemetry_status},
        )
    degraded = inputs.telemetry_status in ("EXPORT_FAILING", "SDK_UNAVAILABLE", "MISCONFIGURED")
    return ComponentHealth(
        component=HealthComponent.TELEMETRY,
        domain=HealthDomain.OBSERVABILITY,
        status=HealthStatus.DEGRADED if degraded else HealthStatus.HEALTHY,
        summary=f"telemetry is {inputs.telemetry_status}",
        detail=(
            inputs.telemetry_detail
            or (
                "an export failure is an observability problem and nothing else. No trading "
                "decision depends on it"
                if degraded
                else None
            )
        ),
        facts={
            "status": inputs.telemetry_status,
            "endpoint_configured": str(bool(inputs.config.observability.exporter.endpoint)),
        },
    )


def _notifications(inputs: HealthInputs) -> ComponentHealth:
    """Whether alerts can actually reach anybody."""
    if inputs.notification_channels == 0:
        return ComponentHealth(
            component=HealthComponent.NOTIFICATIONS,
            domain=HealthDomain.OBSERVABILITY,
            status=HealthStatus.DEGRADED,
            summary="no notification channel is enabled",
            detail=(
                "alerts are still recorded — an alert nobody was told about is still an alert "
                "that happened — but nothing will surface them"
            ),
        )
    return ComponentHealth(
        component=HealthComponent.NOTIFICATIONS,
        domain=HealthDomain.OBSERVABILITY,
        status=(HealthStatus.DEGRADED if inputs.notification_failures else HealthStatus.HEALTHY),
        summary=(
            f"{inputs.notification_channels} channel(s), "
            f"{inputs.notification_failures} recent failure(s)"
        ),
        facts={
            "channels": str(inputs.notification_channels),
            "failures": str(inputs.notification_failures),
        },
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _worst(statuses: Any) -> HealthStatus:
    """The most severe status in a set. ``UNKNOWN`` outranks ``HEALTHY``."""
    order = {
        HealthStatus.HEALTHY: 0,
        HealthStatus.UNKNOWN: 1,
        HealthStatus.DEGRADED: 2,
        HealthStatus.UNAVAILABLE: 3,
        HealthStatus.BLOCKED: 4,
    }
    worst = HealthStatus.HEALTHY
    for status in statuses:
        if order[status] > order[worst]:
            worst = status
    return worst


def _application_version() -> str:
    from trading_system import __version__

    return __version__


def probe_configuration(config_dir: Path) -> tuple[SystemConfig | None, str | None]:
    """Load the configuration for a health check, reporting failure rather than raising."""
    try:
        return load_config(config_dir), None
    except ConfigError as exc:
        return None, str(exc)


def measure(callable_: Any) -> tuple[Any, float]:
    """Run something and report how long it took. Used for check latency."""
    started = time.perf_counter()
    result = callable_()
    return result, time.perf_counter() - started


def within(instant: datetime | None, *, of: datetime, minutes: int) -> bool:
    """Whether ``instant`` falls inside the last ``minutes``. ``None`` is False."""
    if instant is None:
        return False
    return instant >= of - timedelta(minutes=minutes)


#: Execution states that mean an order may exist but its outcome is unknown.
#: Named here so the health check and the alert rules agree about the word.
UNRESOLVED_EXECUTION_STATES = frozenset({ExecutionState.UNKNOWN})

#: Reconciliation severities that block. One member today; a frozenset so the
#: health check and the alert rules cannot drift about which.
BLOCKING_FINDING_SEVERITIES = frozenset({ReconciliationSeverity.CRITICAL})

#: Job statuses a health check counts as a problem. ``SKIPPED`` is absent: a
#: job that deliberately did not run is not a fault.
PROBLEM_JOB_STATUSES = frozenset({JobStatus.FAILED, JobStatus.UNKNOWN})
