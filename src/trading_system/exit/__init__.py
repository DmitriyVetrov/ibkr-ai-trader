"""Exit management and position lifecycle (Milestone 10).

The milestone that asks one question about a position that already exists:

    **SHOULD THIS POSITION BE CLOSED?**

and answers it in a closed vocabulary — ``WAIT``, ``EXIT`` or ``BLOCK`` — from
arithmetic over stored artifacts.

.. code-block:: text

    MILESTONE 9 POSITION REALITY     what the broker actually holds
          |
    POSITION MONITOR                 every open structure, read not inferred
          |
    DETERMINISTIC EXIT POLICIES      consistency, expiration, data quality,
          |                          maximum loss, thesis, take profit, trail
    WAIT / EXIT / BLOCK              immutable evaluation + decision
          |
    MILESTONE 8 EXECUTION            the ONLY path to an exit order
          |
    MILESTONE 9 RECONCILIATION       what actually happened
          |
    CLOSED / STILL OPEN / UNKNOWN

The three responsibilities stay separate, and collapsing any two is the failure
this package is shaped to prevent:

    M10 answers  *should this position be closed?*
    M8  answers  *how do we send the exit order?*
    M9  answers  *what actually happened at the broker?*

Eight rules govern it, each with tests that fail loudly:

* **There is no AI here.** No agent, no prompt, no LLM client, and no import
  that reaches one. Whether to sell an option is a safety decision, and a
  deterministic engine that can be replayed is worth more than a persuasive
  one that cannot. A boundary test walks the whole transitive closure.
* **There is no broker here either.** This package holds no connection, no
  writable factory and no path to one. An exit order exists only because
  :meth:`~trading_system.execution.service.ExecutionService.submit_exit` made
  it, under Milestone 8's own two switches and its own idempotency.
* **`UNKNOWN` is never `FAILED`, and never re-sent.** An exit whose outcome was
  never learned may be a live order right now. It blocks, it is resolved by
  observing the broker, and no amount of elapsed time turns it into a failure.
* **Only broker reality closes a position.** Not a submitted order, not a
  reported fill, not a decision to exit. ``CLOSED`` is terminal and nothing
  reopens it.
* **A block always wins; the first trigger exits.** Precedence is one
  reviewable list, safety before profit-taking, and a position that is both at
  its take-profit and structurally unreadable blocks rather than sells.
* **Nothing is fabricated.** A missing bid is not the ask, the last print or
  the price we paid. A missing multiplier is not 100. An unquantified maximum
  loss is not a small one. Each is a named block.
* **A structure exits whole.** There is no independent-leg exit path, in code
  or in configuration; ``allow_independent_leg_exit: true`` fails to load.
* **It decides no money.** No budget, no allocation, no position size. The
  quantity an exit closes is what the broker says is held.

Development guidance is in :doc:`skills/exit-management/README.md`. Milestone
10 introduces **no agent**, so there is deliberately no ``.claude/agents/``
entry for it, exactly as in Milestones 7, 8 and 9.
"""

from typing import TYPE_CHECKING, Any

from trading_system.exit.models import (
    EXIT_SCHEMA_VERSION,
    ExitDecisionRecord,
    ExitEvaluation,
    ExitLegValuation,
    ExitPolicyOutcome,
    ExitPolicySnapshot,
    ExitRequest,
    ExitRunCounts,
    ExitRunResult,
    PositionLifecycleEvent,
    PositionLifecycleSnapshot,
    PositionValuation,
    ThesisConditionCheck,
    TrailingStopRecord,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.exit.engine import ExitInputs, ExitPolicyEngine
    from trading_system.exit.service import (
        ExitEvaluationOutcome,
        ExitRun,
        ExitService,
        OpenPosition,
    )
    from trading_system.exit.store import ExitRepository, FilesystemExitRepository
    from trading_system.exit.valuation import ExitQuoteReader

#: Members loaded on first access rather than at import time.
#:
#: Importing ``trading_system.exit.models`` executes this file, so anything
#: eager here lands in the import graph of every module that merely names an
#: exit type — including the execution service, which type-checks against
#: :class:`ExitRequest`. ``service`` reaches a repository and, through
#: Milestone 8, a *writable broker constructor*; an eager re-export would put
#: that in the import graph of the exit models themselves and make the
#: boundary test's job impossible. The universe, research, strategies,
#: allocation and risk packages defer for the same reason. Do not "tidy" the
#: ``__getattr__`` away.
_LAZY = {
    "ExitInputs": "engine",
    "ExitEvaluationOutcome": "service",
    "ExitPolicyEngine": "engine",
    "ExitQuoteReader": "valuation",
    "ExitRepository": "store",
    "ExitRun": "service",
    "ExitService": "service",
    "FilesystemExitRepository": "store",
    "OpenPosition": "service",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.exit.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "EXIT_SCHEMA_VERSION",
    "ExitDecisionRecord",
    "ExitEvaluation",
    "ExitEvaluationOutcome",
    "ExitInputs",
    "ExitLegValuation",
    "ExitPolicyEngine",
    "ExitPolicyOutcome",
    "ExitPolicySnapshot",
    "ExitQuoteReader",
    "ExitRepository",
    "ExitRequest",
    "ExitRun",
    "ExitRunCounts",
    "ExitRunResult",
    "ExitService",
    "FilesystemExitRepository",
    "OpenPosition",
    "PositionLifecycleEvent",
    "PositionLifecycleSnapshot",
    "PositionValuation",
    "ThesisConditionCheck",
    "TrailingStopRecord",
]
