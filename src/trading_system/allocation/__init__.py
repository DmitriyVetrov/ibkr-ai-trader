"""Opportunity scoring and deterministic campaign budget allocation.

Milestone 7's second half. The risk engine answers *is this permitted?*; this
package answers *how many units may we commit?*:

.. code-block:: text

    purchase candidates (Milestone 6)
          |
    deterministic score            <- weights, not a model; every term recorded
          |
    priority ordering              <- ties broken on explicit keys
          |
    RiskEngine per candidate       <- against the campaign INCLUDING this run
          |
    quantity = floor(min(ceilings))<- never rounds up, never fractional
          |
    immutable allocation run       <- appended, never overwritten

Six rules govern it, each with tests that fail loudly:

* **The campaign budget is independent of the account balance.** EUR 5,000 is
  the shipped envelope; the paper account's balance is irrelevant to it. Where
  the account holds less than the campaign permits, the account wins. The most
  restrictive relevant limit always does.
* **No AI decides money.** Nothing in this package imports an agent, an LLM
  client or a prompt. Confidence *bands* from validated upstream artifacts feed
  the ordering; they can never change a quantity, a limit or a permission.
* **Quantity is a floor, and a whole number.** :func:`~budget_allocator.max_units`
  computes an exact floor and verifies it by multiplication, so no rounding can
  produce a position one contract larger than a limit authorised.
* **No opportunity is funded twice.** An opportunity's identity is derived from
  the research report, strategy decision and contract selection it descends
  from, so a re-run recognises its own earlier authorisation and records
  ``ALREADY_ALLOCATED`` rather than reserving the capital again.
* **NO_TRADE is a first-class outcome.** A valid strategy and a valid contract
  are not an entitlement to capital. When nothing whole fits what is left, the
  answer is no, and the record says which constraint bound.
* **Nothing here is an order.** No order type, no side, no limit price, no
  broker order id, and no broker anywhere in the import graph. The artifact is
  an authorisation boundary, and Milestone 8 is what turns one into an order.

``allocated + reserve + available == budget`` holds exactly, in decimal, at
every step, and the run record refuses to be constructed if it does not.
"""

from typing import TYPE_CHECKING, Any

from trading_system.allocation.models import (
    AllocationRunCounts,
    AllocationRunResult,
    CampaignAllocation,
    QuantityCalculation,
    allocation_identifier,
    allocation_run_identifier,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.allocation.budget_allocator import (
        AllocationEngine,
        CandidateAllocation,
        max_units,
        order_candidates,
    )
    from trading_system.allocation.campaign import build_campaign_snapshot, reservations_from
    from trading_system.allocation.candidates import (
        CandidateBuildError,
        build_candidate,
        risk_profile_of,
    )
    from trading_system.allocation.report import (
        render_allocation,
        render_allocation_run,
        render_evaluation,
    )
    from trading_system.allocation.scorer import score_opportunity
    from trading_system.allocation.service import AllocationRun, AllocationService
    from trading_system.allocation.store import (
        AllocationHistoryEntry,
        AllocationRepository,
        AllocationStoreError,
        FilesystemAllocationRepository,
    )

#: Members loaded on first access rather than at import time.
#:
#: The same discipline the data, research, universe, strategy and risk packages
#: follow, and here for a specific reason: an eager re-export of ``service``
#: would put a filesystem repository — and every repository it composes — into
#: the import graph of anything that merely names an allocation *type*. Do not
#: "tidy" the ``__getattr__`` away.
_LAZY = {
    "AllocationEngine": "budget_allocator",
    "AllocationHistoryEntry": "store",
    "AllocationRepository": "store",
    "AllocationRun": "service",
    "AllocationService": "service",
    "AllocationStoreError": "store",
    "CandidateAllocation": "budget_allocator",
    "CandidateBuildError": "candidates",
    "FilesystemAllocationRepository": "store",
    "build_campaign_snapshot": "campaign",
    "build_candidate": "candidates",
    "max_units": "budget_allocator",
    "order_candidates": "budget_allocator",
    "render_allocation": "report",
    "render_allocation_run": "report",
    "render_evaluation": "report",
    "reservations_from": "campaign",
    "risk_profile_of": "candidates",
    "score_opportunity": "scorer",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.allocation.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AllocationEngine",
    "AllocationHistoryEntry",
    "AllocationRepository",
    "AllocationRun",
    "AllocationRunCounts",
    "AllocationRunResult",
    "AllocationService",
    "AllocationStoreError",
    "CampaignAllocation",
    "CandidateAllocation",
    "CandidateBuildError",
    "FilesystemAllocationRepository",
    "QuantityCalculation",
    "allocation_identifier",
    "allocation_run_identifier",
    "build_campaign_snapshot",
    "build_candidate",
    "max_units",
    "order_candidates",
    "render_allocation",
    "render_allocation_run",
    "render_evaluation",
    "reservations_from",
    "risk_profile_of",
    "score_opportunity",
]
