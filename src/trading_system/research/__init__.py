"""Market research: what to expect from an underlying, and why.

The first genuinely analytical layer in the system, and the bridge from a
universe to a hypothesis:

.. code-block:: text

    UNIVERSE RUN                consumed, never re-selected
          |
    POINT-IN-TIME EVIDENCE      DataRepository only - no provider, no broker
          |
    RESEARCH INPUT              bounded, deduplicated, every fact carries an id
          |
    MARKET RESEARCHER AGENT     one isolated context per underlying
          |
    DETERMINISTIC VALIDATION    rejects a violating outlook in full, never repairs
          |
    IMMUTABLE RESEARCH REPORT   appended, never overwritten

The output is a **structured outlook** — a hypothesis, the evidence behind it,
the events that matter, the risks, and what would prove it wrong. It is never
an option contract, never a strategy, never a position size and never an order.
Milestone 5 ends at the report.

Six rules do most of the work, and each has tests that fail loudly:

* **Retrieval binds.** ``research(as_of=T)`` sees only what had actually been
  retrieved by T. A future-dated *event* is legitimate once it was announced;
  a filing downloaded today is invisible to last week.
* **Evidence is cited by id.** The agent references facts the input carried and
  has no field for a source name, a URL or a date — those are copied from the
  input into the report. An id the input did not contain is a fabricated source
  and rejects the whole response.
* **The hypothesis must be earned.** ``B`` needs upward evidence, ``C``
  downward, ``A`` an elevated-move claim that is not a dated event, ``D`` a
  specific identified event inside the horizon with its announcement recorded,
  ``E`` an explanation. ``A`` and ``D`` are different claims and the difference
  is enforced.
* **Confidence is constrained, not declared.** ``HIGH`` is refused — never
  quietly downgraded — when the evidence, the tiers, the data quality or an
  unresolved contradiction do not license it.
* **Insufficient evidence is a valid outcome.** ``E`` and
  ``INSUFFICIENT_EVIDENCE`` are answers. Nothing is forced into ``B`` or ``C``
  because a hypothesis was expected, and no failure is ever a market view.
* **No contract is expressible.** No model here has a field for a strike, an
  expiry, a right, a delta or an amount of money, and the option context the
  agent sees is aggregate. Selecting an instrument is a later, deterministic
  stage.
"""

from typing import TYPE_CHECKING, Any

from trading_system.research.evidence import ArticleGroup, group_articles
from trading_system.research.models import (
    RESEARCH_SCHEMA_VERSION,
    EventItem,
    EvidenceItem,
    MarketResearchReport,
    ResearchAgentOutput,
    ResearchHorizon,
    ResearchInput,
    ResearchRunResult,
    SourceProvenance,
)
from trading_system.research.report import render_report, render_run, render_summary
from trading_system.research.sources import SourceTrustPolicy
from trading_system.research.validation import (
    ResearchOutputInvalidError,
    validate_agent_output,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.research.context import ResearchInputBuilder
    from trading_system.research.service import ResearchRun, ResearchService
    from trading_system.research.store import (
        FilesystemResearchRepository,
        ResearchHistoryEntry,
        ResearchRepository,
        SymbolHistoryEntry,
    )

#: Members loaded on first access rather than at import time.
#:
#: ``service`` depends on the agent, and the agent depends on this package's
#: contract models — importing it eagerly here would close that loop and make
#: ``import trading_system.agents.market_researcher`` fail.
#:
#: ``context`` and ``store`` are deferred for a second reason. Importing
#: ``trading_system.research.models`` executes this file, so anything eager
#: here lands in the import graph of the agent — and the agent must reach
#: neither the data repository nor the research layer's own filesystem
#: internals. ``tests/research/test_boundaries.py`` follows package
#: ``__init__`` files precisely because Python does. Do not "tidy" the
#: ``__getattr__`` away.
_LAZY = {
    "FilesystemResearchRepository": "store",
    "ResearchHistoryEntry": "store",
    "ResearchInputBuilder": "context",
    "ResearchRepository": "store",
    "ResearchRun": "service",
    "ResearchService": "service",
    "SymbolHistoryEntry": "store",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.research.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "RESEARCH_SCHEMA_VERSION",
    "ArticleGroup",
    "EventItem",
    "EvidenceItem",
    "FilesystemResearchRepository",
    "MarketResearchReport",
    "ResearchAgentOutput",
    "ResearchHistoryEntry",
    "ResearchHorizon",
    "ResearchInput",
    "ResearchInputBuilder",
    "ResearchOutputInvalidError",
    "ResearchRepository",
    "ResearchRun",
    "ResearchRunResult",
    "ResearchService",
    "SourceProvenance",
    "SourceTrustPolicy",
    "SymbolHistoryEntry",
    "group_articles",
    "render_report",
    "render_run",
    "render_summary",
    "validate_agent_output",
]
