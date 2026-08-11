"""Universe selection: which underlying assets deserve deeper research.

The bridge between the data layer and the first AI agent, and the place where
the system's central rule is made structural rather than aspirational:

.. code-block:: text

    DATA REPOSITORY
          |
    DETERMINISTIC ELIGIBILITY     <- decides who is even considered
          |
    RESEARCH-READY CANDIDATES
          |
    UNIVERSE SELECTOR AGENT       <- ranks; cannot admit, cannot exceed the cap
          |
    STRUCTURED RANKING
          |
    DETERMINISTIC VALIDATION      <- rejects a violating response in full
          |
    IMMUTABLE UNIVERSE SNAPSHOT

The output is a list of **underlyings** — SPY, NVDA — never option contracts,
never a strategy, never a direction, never an amount of money. The universe
selector is not a trader, not a predictor, not an option strategist and not a
risk engine; it is a controlled ranking component, and the model shapes here
are what stop it becoming anything else.

Three rules do most of the work:

* a deterministic exclusion is final — no ranking can restore it;
* an unestablished fact stays unestablished — ``UNKNOWN`` optionability is
  never read as ``TRUE`` or as ``FALSE``;
* every asset is explainable — selected assets carry reasons and provenance,
  rejected assets carry a machine-readable reason, and there is no third case.
"""

from typing import TYPE_CHECKING, Any

from trading_system.universe.filters import FilterOutcome, PreFilter
from trading_system.universe.models import (
    UNIVERSE_SCHEMA_VERSION,
    CandidateAsset,
    RejectedAsset,
    SelectedAsset,
    UniverseAgentRanking,
    UniverseSelectionInput,
    UniverseSelectionResult,
)
from trading_system.universe.report import render_report, render_summary
from trading_system.universe.store import (
    FilesystemUniverseRepository,
    UniverseHistoryEntry,
    UniverseRepository,
)
from trading_system.universe.validation import AgentOutputInvalidError, validate_ranking

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.universe.service import UniverseRun, UniverseSelectionService

#: Members loaded on first access rather than at import time.
#:
#: ``service`` depends on the agent, and the agent depends on this package's
#: contract models — importing it eagerly here would close that loop and make
#: ``import trading_system.agents.universe_selector`` fail. Deferring the import
#: keeps the dependency pointing one way (universe -> agents) without giving up
#: the flat public API. The alternative, moving the contract models out of the
#: universe package, would put a universe artifact somewhere it does not belong.
_LAZY = {"UniverseRun": "service", "UniverseSelectionService": "service"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.universe.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "UNIVERSE_SCHEMA_VERSION",
    "AgentOutputInvalidError",
    "CandidateAsset",
    "FilesystemUniverseRepository",
    "FilterOutcome",
    "PreFilter",
    "RejectedAsset",
    "SelectedAsset",
    "UniverseAgentRanking",
    "UniverseHistoryEntry",
    "UniverseRepository",
    "UniverseRun",
    "UniverseSelectionInput",
    "UniverseSelectionResult",
    "UniverseSelectionService",
    "render_report",
    "render_summary",
    "validate_ranking",
]
