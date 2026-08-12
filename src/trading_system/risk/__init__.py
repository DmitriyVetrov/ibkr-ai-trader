"""The deterministic risk engine. No AI agent may override a rejection.

Milestone 7's first half. The question this package answers is *is this
proposed position permitted?* — and deliberately not *how many units can we
afford?*, which belongs to :mod:`trading_system.allocation`:

.. code-block:: text

    purchase candidate (Milestone 6)   legs, and the cost of ONE unit
          |
    limit hierarchy resolved           global -> campaign -> strategy -> position
          |
    guards                             price, quality, currency, point-in-time
          |
    limit checks                       budget, risk, concentration, positions
          |
    RiskEvaluation                     APPROVED / REJECTED + reason codes

Four properties hold, each with tests that fail loudly:

* **It is pure.** :class:`~trading_system.risk.engine.RiskEngine` takes limits,
  a candidate, a campaign snapshot and an instant, and returns a verdict. No
  clock, no network, no broker, no repository, no model. Two calls over the
  same inputs return the same verdict, checks in the same order.
* **It cannot reach a broker.** Account state arrives as a stored
  :class:`~trading_system.risk.models.AccountSnapshot`, captured once by an
  explicit command. This is a safety property as well as an architectural one:
  Milestone 2 established that a second uncached round trip on one IBKR
  connection can go unanswered indefinitely, so a risk check that fetched its
  own account state could hang the process.
* **An unevaluated limit is not a satisfied one.** ``NOT_EVALUATED`` is a
  distinct check outcome, used where an input genuinely is not tracked yet.
  Configuration decides whether the unknown blocks a trade; nothing here reads
  it as a pass.
* **Nothing is invented or repaired.** A missing price is a rejection, not a
  zero. A stale quote is stale. An unknown currency is not converted at a rate
  nobody configured. An upstream data-quality verdict is read, never re-graded.

The campaign budget is **independent of the broker account balance**. A paper
account holding a million euro is not permission to spend a million euro; the
campaign may spend its own envelope less its reserve, and when the account has
less than that, the account wins. The most restrictive relevant limit always
does.
"""

from typing import TYPE_CHECKING, Any

from trading_system.risk.models import (
    ALLOCATION_SCHEMA_VERSION,
    AccountPosition,
    AccountSnapshot,
    AllocationCandidate,
    CampaignPosition,
    CampaignSnapshot,
    CandidateLeg,
    CandidatePrice,
    OpportunityScore,
    PortfolioExposure,
    RiskCheck,
    RiskEvaluation,
    RiskLimits,
    StrategyRiskProfile,
    account_snapshot_identifier,
    opportunity_identifier,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.risk.account import build_account_snapshot
    from trading_system.risk.engine import RiskEngine, evaluation_identifier
    from trading_system.risk.exposure import exposure_for, would_add
    from trading_system.risk.limits import (
        LimitResolutionError,
        resolve_campaign_budget,
        resolve_limits,
    )
    from trading_system.risk.store import (
        AccountSnapshotRepository,
        FilesystemAccountSnapshotRepository,
        RiskStoreError,
    )

#: Members loaded on first access rather than at import time.
#:
#: The same discipline the data, research, universe and strategy packages
#: follow, and for the same reason: importing ``trading_system.risk.models``
#: executes this file, so anything eager here lands in the import graph of
#: every consumer of a risk *type* — including the allocation candidate builder,
#: which must not acquire a filesystem repository merely by naming a model.
_LAZY = {
    "AccountSnapshotRepository": "store",
    "FilesystemAccountSnapshotRepository": "store",
    "LimitResolutionError": "limits",
    "RiskEngine": "engine",
    "RiskStoreError": "store",
    "build_account_snapshot": "account",
    "evaluation_identifier": "engine",
    "exposure_for": "exposure",
    "resolve_campaign_budget": "limits",
    "resolve_limits": "limits",
    "would_add": "exposure",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.risk.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ALLOCATION_SCHEMA_VERSION",
    "AccountPosition",
    "AccountSnapshot",
    "AccountSnapshotRepository",
    "AllocationCandidate",
    "CampaignPosition",
    "CampaignSnapshot",
    "CandidateLeg",
    "CandidatePrice",
    "FilesystemAccountSnapshotRepository",
    "LimitResolutionError",
    "OpportunityScore",
    "PortfolioExposure",
    "RiskCheck",
    "RiskEngine",
    "RiskEvaluation",
    "RiskLimits",
    "RiskStoreError",
    "StrategyRiskProfile",
    "account_snapshot_identifier",
    "build_account_snapshot",
    "evaluation_identifier",
    "exposure_for",
    "opportunity_identifier",
    "resolve_campaign_budget",
    "resolve_limits",
    "would_add",
]
