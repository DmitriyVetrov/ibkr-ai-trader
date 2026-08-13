"""Reservations: the lifecycle of capital Milestone 7 committed.

Milestone 7 ends at an authorisation and cannot know whether the order ever
filled, so it treats every authorisation as spent. That is the conservative
reading and it is correct — double-authorising the same capital is the failure
worth preventing — but it means committed capital never comes back. This
package is the milestone that can tell:

.. code-block:: text

    APPROVED authorisation  ->  Reservation (RESERVED)
    execution FAILED / REJECTED / CANCELLED unfilled / EXPIRED unfilled
                            ->  RELEASED
    execution FILLED        ->  CONSUMED, at what actually traded
    execution PARTIALLY_FILLED
                            ->  PARTIALLY_CONSUMED; the remainder stays committed
    execution UNKNOWN       ->  UNKNOWN; the capital stays locked

Five rules govern it, each with tests that fail loudly:

* **``RESERVED`` is not ``INVESTED``.** Capital is committed by an
  authorisation and spent by a fill. Only the second creates a position.
* **``UNKNOWN`` never releases.** An execution whose outcome was never learned
  may be a live order right now. ``release_on_unknown: true`` fails to load,
  the ``reservations release`` command refuses it, and where *any* attempt
  against an authorisation is unresolved, nothing at all is released.
* **The accounting balances exactly.** ``consumed + released + remaining ==
  authorised``, in decimal, on every record. Consuming past the authorisation
  requires a recorded broker correction.
* **Both figures are kept.** What Milestone 7 authorised and what the market
  actually charged are different numbers, and overwriting the first with the
  second destroys the only evidence of the difference.
* **No broker, no model.** This package imports neither. Capital moves on the
  evidence the execution ledger already recorded, and how much of a campaign is
  committed is arithmetic.
"""

from typing import TYPE_CHECKING, Any

from trading_system.reservations.models import (
    RESERVATIONS_SCHEMA_VERSION,
    CampaignCapital,
    Reservation,
    ReservationEvent,
    reservation_event_identifier,
    reservation_identifier,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.reservations.lifecycle import (
        ReservationOutcome,
        authorised_capital_for,
        dominant_execution,
        executed_capital,
        resolve_reservation,
    )
    from trading_system.reservations.report import (
        render_capital,
        render_reservation,
        render_reservations,
    )
    from trading_system.reservations.service import (
        ReservationService,
        ReservationSync,
        ReservationUpdate,
    )
    from trading_system.reservations.store import (
        FilesystemReservationRepository,
        ReservationHistoryEntry,
        ReservationRepository,
        ReservationStoreError,
    )

#: Members loaded on first access rather than at import time, for the same
#: reason every other package in this system defers its service: an eager
#: re-export would put a filesystem repository into the import graph of
#: anything that merely names a reservation type.
_LAZY = {
    "FilesystemReservationRepository": "store",
    "ReservationHistoryEntry": "store",
    "ReservationOutcome": "lifecycle",
    "ReservationRepository": "store",
    "ReservationService": "service",
    "ReservationStoreError": "store",
    "ReservationSync": "service",
    "ReservationUpdate": "service",
    "authorised_capital_for": "lifecycle",
    "dominant_execution": "lifecycle",
    "executed_capital": "lifecycle",
    "render_capital": "report",
    "render_reservation": "report",
    "render_reservations": "report",
    "resolve_reservation": "lifecycle",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.reservations.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "RESERVATIONS_SCHEMA_VERSION",
    "CampaignCapital",
    "FilesystemReservationRepository",
    "Reservation",
    "ReservationEvent",
    "ReservationHistoryEntry",
    "ReservationOutcome",
    "ReservationRepository",
    "ReservationService",
    "ReservationStoreError",
    "ReservationSync",
    "ReservationUpdate",
    "authorised_capital_for",
    "dominant_execution",
    "executed_capital",
    "render_capital",
    "render_reservation",
    "render_reservations",
    "reservation_event_identifier",
    "reservation_identifier",
    "resolve_reservation",
]
