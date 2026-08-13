"""The position ledger: what the broker holds, and what we believe it holds.

Milestone 9's first half. Every stage before it proposes, authorises or sends;
this one *observes*, and keeps two records that must never be merged:

.. code-block:: text

    Broker (read-only, one short-lived connection)
          |
    BrokerPositionSnapshot     <- what the broker says it holds
    recorded fills             <- deduplicated on the broker's own execution ids
          |
    ExpectedPosition           <- what CONFIRMED FILLS say should exist
    StrategyPosition           <- the logical structure, leg by leg

Five rules govern it, each with tests that fail loudly:

* **Only a confirmed fill makes a position.** An allocation, a submitted order,
  an acknowledgement and an ``UNKNOWN`` submission all establish nothing. The
  configuration key that would relax this fails to load.
* **A failed broker read is not an empty account.** The two have different
  statuses, different constructors and different consequences, and the snapshot
  model refuses to let either wear the other's shape. Reconciling against a
  failed read would report every real holding as gone.
* **Identity comes from the broker.** The contract id wherever there is one;
  the human-readable fallback is used only when there is not, and is recorded
  as the weaker key it is. An option fill that can be identified neither way is
  refused rather than merged into the wrong strike.
* **Nothing is filled in.** A market value, an unrealised profit and loss or a
  commission the broker did not report stays ``None``. Nothing multiplies a
  quantity by a reference price and calls the result a broker figure.
* **Units are named apart.** ``price`` is quoted terms, ``average_cost`` is
  money for one contract with the multiplier in it, ``market_value`` is money
  for the holding. A conversion that needs a multiplier nobody reported yields
  ``None`` — never an assumed 100.

This package can submit nothing. It builds its broker through the read-only
factory, asserts the broker's own submitted-order counter is still zero after
every read, and a test walks the import graph to prove the writable constructor
is unreachable from here.
"""

from typing import TYPE_CHECKING, Any

from trading_system.positions.models import (
    POSITIONS_SCHEMA_VERSION,
    BrokerPositionSnapshot,
    ExpectedPosition,
    ObservedFill,
    ObservedPosition,
    StrategyLegPosition,
    StrategyPosition,
    contract_key,
    fill_identifier,
    mask_account,
    position_identifier,
    position_snapshot_identifier,
    strategy_position_identifier,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.positions.expected import (
        ExpectedProjection,
        expected_from_execution,
        expected_from_fills,
        leg_key,
        project_expected_positions,
        strategy_position_for,
        structure_status,
    )
    from trading_system.positions.fills import (
        ContractTerms,
        FillTranslationError,
        deduplicate_fills,
        new_fills,
        terms_from_legs,
        to_observed_fill,
        to_observed_fills,
    )
    from trading_system.positions.report import (
        render_capture,
        render_projection,
        render_snapshot,
    )
    from trading_system.positions.service import (
        BrokerState,
        PositionCapture,
        PositionService,
    )
    from trading_system.positions.snapshot import (
        build_position_snapshot,
        to_observed_position,
        unavailable_snapshot,
    )
    from trading_system.positions.store import (
        FilesystemFillRepository,
        FilesystemPositionRepository,
        FillRepository,
        PositionRepository,
        PositionStoreError,
    )

#: Members loaded on first access rather than at import time.
#:
#: The same discipline the data, research, universe, strategy, risk, allocation
#: and execution packages follow, and here for the same reason as execution's:
#: an eager re-export of ``service`` would put a **broker** into the import
#: graph of anything that merely names a position type. Do not "tidy" the
#: ``__getattr__`` away.
_LAZY = {
    "BrokerState": "service",
    "ContractTerms": "fills",
    "ExpectedProjection": "expected",
    "FillRepository": "store",
    "FillTranslationError": "fills",
    "FilesystemFillRepository": "store",
    "FilesystemPositionRepository": "store",
    "PositionCapture": "service",
    "PositionRepository": "store",
    "PositionService": "service",
    "PositionStoreError": "store",
    "build_position_snapshot": "snapshot",
    "deduplicate_fills": "fills",
    "expected_from_execution": "expected",
    "expected_from_fills": "expected",
    "leg_key": "expected",
    "new_fills": "fills",
    "project_expected_positions": "expected",
    "render_capture": "report",
    "render_projection": "report",
    "render_snapshot": "report",
    "strategy_position_for": "expected",
    "structure_status": "expected",
    "terms_from_legs": "fills",
    "to_observed_fill": "fills",
    "to_observed_fills": "fills",
    "to_observed_position": "snapshot",
    "unavailable_snapshot": "snapshot",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.positions.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "POSITIONS_SCHEMA_VERSION",
    "BrokerPositionSnapshot",
    "BrokerState",
    "ContractTerms",
    "ExpectedPosition",
    "ExpectedProjection",
    "FilesystemFillRepository",
    "FilesystemPositionRepository",
    "FillRepository",
    "FillTranslationError",
    "ObservedFill",
    "ObservedPosition",
    "PositionCapture",
    "PositionRepository",
    "PositionService",
    "PositionStoreError",
    "StrategyLegPosition",
    "StrategyPosition",
    "build_position_snapshot",
    "contract_key",
    "deduplicate_fills",
    "expected_from_execution",
    "expected_from_fills",
    "fill_identifier",
    "leg_key",
    "mask_account",
    "new_fills",
    "position_identifier",
    "position_snapshot_identifier",
    "project_expected_positions",
    "render_capture",
    "render_projection",
    "render_snapshot",
    "strategy_position_for",
    "strategy_position_identifier",
    "structure_status",
    "terms_from_legs",
    "to_observed_fill",
    "to_observed_fills",
    "to_observed_position",
    "unavailable_snapshot",
]
