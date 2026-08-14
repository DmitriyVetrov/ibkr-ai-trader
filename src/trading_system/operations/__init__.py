"""Operations: the scheduler, operational health and alerting (Milestone 11).

Milestone 10 built ``ExitService.monitor`` and deliberately not the loop that
calls it. This package is the loop — and only the loop.

.. code-block:: text

    config/schedules.yaml
          |
    Scheduler        cron, market calendar, isolation, bounded runs
          |
    JobDefinition    ONE call to an already-tested service method
          |
    JobRun           persisted before the work and after it
          |
    OperationsService  health (trading and observability, separately) + alerts
          |
    NotificationProvider   best effort; a failed send is recorded, never raised

The rule that shapes everything here: **the scheduler orchestrates; it contains
no trading logic.** Whether a position should close is Milestone 10's answer,
how an exit order is sent is Milestone 8's, and what actually happened at the
broker is Milestone 9's. A scheduler that re-derived any of those would be a
second, untested copy of a safety decision.

Two more properties hold throughout:

* **No broker lives here.** Services open their own short-lived read-only
  connections. A scheduler holding a persistent connection and polling through
  it is exactly the shape Milestone 2's one-reliable-round-trip constraint
  forbids.
* **An alert cannot trade.** Nothing in :mod:`~trading_system.operations.alerts`
  can reach an order path, and a boundary test walks the transitive graph to
  prove it. Safety is enforced by the domain; alerting is how a person finds
  out.

Everything that touches a store or a service is deferred through
``__getattr__``, for the same reason :mod:`trading_system.exit` and
:mod:`trading_system.pnl` defer theirs: importing an operational *type* must
not drag a service — and through it, eventually, a broker — into the import
graph of whatever merely named one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "OPERATIONS_SCHEMA_VERSION",
    "Alert",
    "AlertRules",
    "ComponentHealth",
    "FilesystemOperationsRepository",
    "JobDefinition",
    "JobRun",
    "NotificationProvider",
    "OperationalHealth",
    "OperationsRepository",
    "OperationsService",
    "Scheduler",
    "SchedulerRun",
    "build_registry",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.operations.alerts import AlertRules
    from trading_system.operations.jobs import JobDefinition, build_registry
    from trading_system.operations.models import (
        OPERATIONS_SCHEMA_VERSION,
        Alert,
        ComponentHealth,
        JobRun,
        OperationalHealth,
        SchedulerRun,
    )
    from trading_system.operations.notifications import NotificationProvider
    from trading_system.operations.scheduler import Scheduler
    from trading_system.operations.service import OperationsService
    from trading_system.operations.store import (
        FilesystemOperationsRepository,
        OperationsRepository,
    )

_LAZY: dict[str, str] = {
    "OPERATIONS_SCHEMA_VERSION": "trading_system.operations.models",
    "Alert": "trading_system.operations.models",
    "ComponentHealth": "trading_system.operations.models",
    "JobRun": "trading_system.operations.models",
    "OperationalHealth": "trading_system.operations.models",
    "SchedulerRun": "trading_system.operations.models",
    "AlertRules": "trading_system.operations.alerts",
    "JobDefinition": "trading_system.operations.jobs",
    "build_registry": "trading_system.operations.jobs",
    "NotificationProvider": "trading_system.operations.notifications",
    "Scheduler": "trading_system.operations.scheduler",
    "OperationsService": "trading_system.operations.service",
    "FilesystemOperationsRepository": "trading_system.operations.store",
    "OperationsRepository": "trading_system.operations.store",
}


def __getattr__(name: str) -> Any:
    """Resolve a public name on first use, never at import."""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)
