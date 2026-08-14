"""Realised profit and loss, computed from broker-confirmed fills (Milestone 11).

The milestone that finally answers *what did this trade actually make?* — and
answers it from the only evidence that can support the claim:

.. code-block:: text

    confirmed entry fills (M9)   what the account actually paid
    confirmed exit fills (M9)    what the account actually received
          |
    PnLCalculator                pure, deterministic, per structure
          |
    RealizedPnL                  immutable, content-addressed
          |
    DailyPnL                     exchange-local day roll-up
          |
    reservation settlement       capital returns to the campaign
          |
    daily loss state             what the risk engine reads next time

Four rules hold throughout, each with tests that fail loudly:

* **Only a broker-confirmed fill contributes.** Not a limit price, not the
  reference price Milestone 7 authorised, not a midpoint, not an estimate of
  what an exit ought to have made.
* **``NOT_AVAILABLE`` is a first-class result.** A missing commission, an
  absent multiplier or a cross-currency pair yields no figure rather than a
  plausible one — because a plausible one would be *used*, by the daily loss
  limit, to permit or refuse the next trade.
* **A structure is one trade.** A straddle's result is one number over two
  legs, never two independent results that happen to share an underlying.
* **Nothing here decides anything.** No order, no sizing, no permission. This
  package computes a figure and stores it; the risk engine reads it later.

Everything that touches a repository is deferred through ``__getattr__``, for
the same reason :mod:`trading_system.exit` and :mod:`trading_system.execution`
defer theirs: importing a P&L *type* must not drag a store — and through it,
eventually, a broker — into the import graph of whatever merely named one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "PNL_SCHEMA_VERSION",
    "DailyPnL",
    "FilesystemPnLRepository",
    "PnLCalculator",
    "PnLRepository",
    "PnLService",
    "RealizedPnL",
    "RealizedPnLLeg",
    "ReservationSettlement",
    "daily_pnl_identifier",
    "realized_pnl_identifier",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.pnl.calculator import PnLCalculator
    from trading_system.pnl.models import (
        PNL_SCHEMA_VERSION,
        DailyPnL,
        RealizedPnL,
        RealizedPnLLeg,
        ReservationSettlement,
        daily_pnl_identifier,
        realized_pnl_identifier,
    )
    from trading_system.pnl.service import PnLService
    from trading_system.pnl.store import FilesystemPnLRepository, PnLRepository

_LAZY: dict[str, str] = {
    "PNL_SCHEMA_VERSION": "trading_system.pnl.models",
    "DailyPnL": "trading_system.pnl.models",
    "RealizedPnL": "trading_system.pnl.models",
    "RealizedPnLLeg": "trading_system.pnl.models",
    "ReservationSettlement": "trading_system.pnl.models",
    "daily_pnl_identifier": "trading_system.pnl.models",
    "realized_pnl_identifier": "trading_system.pnl.models",
    "PnLCalculator": "trading_system.pnl.calculator",
    "FilesystemPnLRepository": "trading_system.pnl.store",
    "PnLRepository": "trading_system.pnl.store",
    "PnLService": "trading_system.pnl.service",
}


def __getattr__(name: str) -> Any:
    """Resolve a public name on first use, never at import.

    ``PnLService`` reaches the position ledger, which is the one package that
    holds a broker. An eager re-export here would put that broker in the import
    graph of anything that merely names :class:`RealizedPnL` — including the
    allocation service, whose boundary tests forbid exactly that.
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)
