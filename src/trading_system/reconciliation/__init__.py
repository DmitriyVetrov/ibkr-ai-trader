"""Reconciliation: comparing what we believe against what the broker holds.

Milestone 9's third part, and the one that closes the loop:

.. code-block:: text

    ALLOCATION -> EXECUTION -> BROKER ORDER -> FILL -> POSITION
                                                         |
                                             RESERVATION / CAPITAL STATE
                                                         |
                                                  RECONCILIATION
                                                         |
                                            RISK / ALLOCATION INPUT

It answers two questions: *what does the broker actually hold now?* and *how
much of our capital is still reserved rather than invested?*

Six rules govern it, each with tests that fail loudly:

* **The broker is authoritative, and reporting is the whole job.** A
  disagreement is a finding. Nothing here edits the internal ledger to make one
  disappear, adopts a broker position, cancels an order or places a
  compensating trade. ``corrective_orders_permitted: true`` and
  ``auto_adopt_orphan_positions: true`` both fail to load, so the refusals are
  visible in configuration rather than merely absent from the code.
* **"We could not look" is not "there is nothing there".** A failed broker read
  is ``BROKER_DATA_UNAVAILABLE`` and produces no comparison at all. An empty
  portfolio the broker actually reported is ``BROKER_RETURNED_EMPTY``, a valid
  answer about the account. ``MATCH`` requires that the broker was genuinely
  read — agreeing with an absence of data is not agreement.
* **``UNKNOWN`` is resolved by observation and never optimistically.** Broker
  evidence can settle an ambiguous submission to ``SUBMITTED``, ``FILLED``,
  ``PARTIALLY_FILLED`` or ``CANCELLED``. Absence from the open-order list
  settles nothing — a filled, a cancelled and a never-sent order look identical
  from there — and an unsettled ``UNKNOWN`` keeps its capital locked.
* **``FAILED`` means nothing was sent.** An order at the broker for a ``FAILED``
  execution is ``FAILED_EXECUTION_HAS_BROKER_ORDER``, a critical consistency
  violation. The execution is never quietly relabelled ``SUBMITTED``.
* **Every finding shows both sides.** Contract identity, expected value,
  observed value, the difference, both provenances and both clocks. "Positions
  differ" is not something anyone can act on.
* **Running it twice changes nothing.** Ids are content-derived, fills
  deduplicate on the broker's own execution ids, reservation outcomes are
  deltas against current state, and a replayed event is recognised and dropped.
  The second run over unchanged state releases no capital, consumes none, and
  records a re-observation.

Severity is configuration, not code: how alarming a missing position is depends
on how the account is operated, and ``config/reconciliation.yaml`` must state a
severity for every finding type or fail to load.
"""

from typing import TYPE_CHECKING, Any

from trading_system.reconciliation.models import (
    RECONCILIATION_SCHEMA_VERSION,
    ReconciliationCounts,
    ReconciliationEvent,
    ReconciliationFinding,
    ReconciliationResult,
    finding_identifier,
    reconciliation_event_identifier,
    reconciliation_identifier,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.reconciliation.engine import (
        ReconciliationEngine,
        ReconciliationInputs,
    )
    from trading_system.reconciliation.fills import compare_fills
    from trading_system.reconciliation.findings import make_finding
    from trading_system.reconciliation.orders import compare_orders
    from trading_system.reconciliation.positions import (
        compare_positions,
        compare_structures,
    )
    from trading_system.reconciliation.report import (
        render_finding,
        render_reconciliation,
        render_run,
    )
    from trading_system.reconciliation.reservations import compare_reservations
    from trading_system.reconciliation.service import (
        ReconciliationRun,
        ReconciliationService,
        blocks_new_executions,
    )
    from trading_system.reconciliation.store import (
        FilesystemReconciliationRepository,
        ReconciliationHistoryEntry,
        ReconciliationRepository,
        ReconciliationStoreError,
    )
    from trading_system.reconciliation.unknown import UnknownResolution, resolve_unknown

#: Members loaded on first access rather than at import time.
#:
#: The same discipline every other package here follows. An eager re-export of
#: ``service`` would put a broker connection and three repositories into the
#: import graph of anything that merely names a finding type.
_LAZY = {
    "FilesystemReconciliationRepository": "store",
    "ReconciliationEngine": "engine",
    "ReconciliationHistoryEntry": "store",
    "ReconciliationInputs": "engine",
    "ReconciliationRepository": "store",
    "ReconciliationRun": "service",
    "ReconciliationService": "service",
    "ReconciliationStoreError": "store",
    "UnknownResolution": "unknown",
    "blocks_new_executions": "service",
    "compare_fills": "fills",
    "compare_orders": "orders",
    "compare_positions": "positions",
    "compare_reservations": "reservations",
    "compare_structures": "positions",
    "make_finding": "findings",
    "render_finding": "report",
    "render_reconciliation": "report",
    "render_run": "report",
    "resolve_unknown": "unknown",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.reconciliation.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "RECONCILIATION_SCHEMA_VERSION",
    "FilesystemReconciliationRepository",
    "ReconciliationCounts",
    "ReconciliationEngine",
    "ReconciliationEvent",
    "ReconciliationFinding",
    "ReconciliationHistoryEntry",
    "ReconciliationInputs",
    "ReconciliationRepository",
    "ReconciliationResult",
    "ReconciliationRun",
    "ReconciliationService",
    "ReconciliationStoreError",
    "UnknownResolution",
    "blocks_new_executions",
    "compare_fills",
    "compare_orders",
    "compare_positions",
    "compare_reservations",
    "compare_structures",
    "finding_identifier",
    "make_finding",
    "reconciliation_event_identifier",
    "reconciliation_identifier",
    "render_finding",
    "render_reconciliation",
    "render_run",
    "resolve_unknown",
]
