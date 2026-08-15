"""Controlled closure of pre-existing broker holdings (orphan cleanup).

Reconciliation reports an ``ORPHAN_BROKER_POSITION`` — a holding the broker
really has that no execution of ours accounts for — and deliberately never
adopts it, never sells it and never assigns it to a campaign. That is correct.
It also leaves a real account in a state an operator has to resolve by hand,
and this package is that resolution made auditable:

.. code-block:: text

    external / pre-existing broker position
          |
    observed as ORPHAN_BROKER_POSITION      reconciliation, unmodified
          |
    explicitly authorised cleanup           an operator, a specific report,
          |                                 specific contract ids
    cleanup execution                       ExecutionService, the one order path
          |
    broker confirmation                     a POSITION read, not a fill report
          |
    reconciliation history records the resolution

Nine rules govern it, each with tests that fail loudly:

* **It adopts nothing.** No allocation, purchase card, risk decision,
  opportunity, strategy, research report or expected position is created for a
  holding this system never opened. ``ExecutionIntent.CLEANUP`` makes that
  structural: the execution record *refuses* to be constructed carrying any of
  them, and its fills are excluded from the internal position ledger — because
  netting them in would manufacture an expected position of minus one for a
  contract this system never expected to hold.
* **Only an explicitly reported orphan may be targeted.** Not "everything the
  ledger does not recognise": when the ledger cannot be read *nothing* is
  recognised, and that rule liquidates the account precisely when the system is
  least able to say what it owns. ``require_orphan_finding: false`` fails to
  load.
* **Identity comes from the broker.** A target is addressed by contract id or
  it is not a target. Adjusted contracts share symbol, strike, expiry and
  right, so a symbol is not an identity.
* **The quantity is what the broker holds, and nothing else.** Equal by
  construction, checked again against the snapshot, and both figures are
  recorded so the equality is auditable. There is no oversell path, and a
  short holding is refused outright rather than bought back.
* **No structure is invented.** Two orphan holdings that look like a straddle
  may or may not have been bought as one, and nothing recorded which. There is
  no combo path here at all: one holding, one order. Every orphan being *long*
  is what makes that safe — closing one leg of an invented pair cannot leave a
  short.
* **Four switches, and no two are the same decision.** ``cleanup.enabled``,
  ``execution.enabled``, an explicit ``--confirm``, and PAPER with both live
  guards off and the connected account's own identity proving it.
* **A review is structural, not a flag.** Without ``--confirm`` the code path
  never reaches the method that can construct a writable broker, so "this
  submits nothing" is a property of the graph rather than a check someone has
  to get right.
* **Nothing is retried, repriced or continued.** A rejection is reported. An
  ``UNKNOWN`` submission blocks everything about that holding permanently and
  is resolved by observing the broker. A partial fill is reported and the
  remainder is left; ``allow_partial_continuation: true`` fails to load.
* **Only a position read closes a target.** Not a submitted order, not a
  reported fill. ``CleanupOutcome`` refuses ``CLOSED`` without a broker
  observation afterwards, and refuses one that disagrees with it.

The service is deferred through ``__getattr__`` for the same reason
``exit/__init__.py`` and ``execution/__init__.py`` defer theirs: it imports
:class:`~trading_system.execution.service.ExecutionService`, which is the only
module in the system that can obtain a *writable* broker. An eager re-export
would put that in the import graph of anything that merely names a cleanup
type — including the execution service itself, which type-checks against
:class:`~trading_system.cleanup.models.CleanupTarget`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trading_system.cleanup.models import (
    CLEANUP_SCHEMA_VERSION,
    CleanupOutcome,
    CleanupOutcomeStatus,
    CleanupRunStatus,
    CleanupTarget,
    OrphanCleanupRequest,
    OrphanCleanupRun,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.cleanup.gates import GateVerdict, evaluate_run_gates
    from trading_system.cleanup.service import CleanupPlan, CleanupRunOutcome, CleanupService
    from trading_system.cleanup.store import CleanupRepository, FilesystemCleanupRepository
    from trading_system.cleanup.targets import TargetSelection, select_targets

__all__ = [
    "CLEANUP_SCHEMA_VERSION",
    "CleanupOutcome",
    "CleanupOutcomeStatus",
    "CleanupPlan",
    "CleanupRepository",
    "CleanupRunOutcome",
    "CleanupRunStatus",
    "CleanupService",
    "CleanupTarget",
    "FilesystemCleanupRepository",
    "GateVerdict",
    "OrphanCleanupRequest",
    "OrphanCleanupRun",
    "TargetSelection",
    "evaluate_run_gates",
    "select_targets",
]

_DEFERRED = {
    "CleanupPlan": "trading_system.cleanup.service",
    "CleanupRunOutcome": "trading_system.cleanup.service",
    "CleanupService": "trading_system.cleanup.service",
    "CleanupRepository": "trading_system.cleanup.store",
    "FilesystemCleanupRepository": "trading_system.cleanup.store",
    "GateVerdict": "trading_system.cleanup.gates",
    "TargetSelection": "trading_system.cleanup.targets",
    "evaluate_run_gates": "trading_system.cleanup.gates",
    "select_targets": "trading_system.cleanup.targets",
}


def __getattr__(name: str) -> Any:
    module = _DEFERRED.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module), name)
