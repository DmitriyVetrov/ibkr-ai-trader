"""Strategy selection and deterministic contract selection.

The milestone that draws the line between *what should we do* and *which actual
option contract satisfies that decision*:

.. code-block:: text

    RESEARCH REPORT           consumed, never re-researched
          |
    STRATEGY REGISTRY         the allow-list, resolved against global risk
          |
    STRATEGY SELECTOR AGENT   chooses WHAT: one configured strategy, or none
          |
    DETERMINISTIC VALIDATION  rejects a violating decision in full, never repairs
          |
    STRATEGY DECISION         a proposal, never an order
          |
    CONTRACT SELECTOR         chooses WHICH: arithmetic over a stored chain
          |
    CONTRACT SELECTION        concrete legs, or an explicit "no valid contract"

The rule the whole package exists to enforce:

    **The AI selects the strategy. Deterministic code selects the contract.**

Five properties hold, each with tests that fail loudly:

* **The agent is never shown a contract.** Its input carries a research
  conclusion and strategy metadata — no chain, no strike list, no contract id,
  no account, no cash — and its output has no field for a strike, an expiry, a
  quantity or a price. It cannot select a contract because it has neither the
  information nor the vocabulary.
* **The contract selector never consults a model.** It imports no agent, no LLM
  client and no news provider; it reads a stored chain through the repository
  and applies configured policy. Identical inputs produce identical contracts.
* **A strategy may narrow a global risk limit, never widen one.** The registry
  refuses to build otherwise — it does not clamp silently.
* **Nothing is invented.** A missing delta is a rejection, not an estimate; an
  unquoted contract has an unknown cost, not a guessed midpoint; a trading
  class is copied from the chain, never derived from the ticker.
* **NO_TRADE and "no valid contract" are first-class outcomes.** Neither stage
  has to produce something because the stage above it did.

Milestone 6 ends at a purchase *candidate*. No order is constructed, no
quantity is chosen and no money is allocated: those are the risk, allocation
and execution engines, and nothing here can pre-empt them.
"""

from typing import TYPE_CHECKING, Any

from trading_system.strategies.base import LegTemplate, StrategyStructure, StrikeRelationship
from trading_system.strategies.models import (
    STRATEGY_SCHEMA_VERSION,
    ContractCostEstimate,
    ContractSelectionResult,
    ContractSelectionRunResult,
    DataReadiness,
    RejectedContract,
    ResearchSummary,
    SelectedLeg,
    StrategyAgentOutput,
    StrategyDecisionRecord,
    StrategyOption,
    StrategyRunResult,
    StrategySelectionInput,
)
from trading_system.strategies.validation import (
    StrategyOutputInvalidError,
    validate_agent_output,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.strategies.chain import ChainReader, ChainView, ContractCandidate
    from trading_system.strategies.context import DataReadinessProbe, build_selection_input
    from trading_system.strategies.contract_selector import ContractSelector, SelectionContext
    from trading_system.strategies.registry import (
        StrategyRegistry,
        StrategyRegistryError,
        StrategySpecification,
    )
    from trading_system.strategies.service import (
        ContractRun,
        ContractSelectionService,
        StrategyRun,
        StrategyService,
    )
    from trading_system.strategies.store import (
        ContractSelectionRepository,
        FilesystemContractSelectionRepository,
        FilesystemStrategyRepository,
        StrategyRepository,
    )

#: Members loaded on first access rather than at import time.
#:
#: Importing ``trading_system.strategies.models`` executes this file, so
#: anything eager here lands in the import graph of the strategy agent — and the
#: agent must reach neither a data repository nor the contract selector. The
#: research and universe packages defer for the same reason, and
#: ``tests/strategy/test_boundaries.py`` walks the closure *through* package
#: ``__init__`` files precisely because Python does. Do not "tidy" the
#: ``__getattr__`` away.
_LAZY = {
    "ChainReader": "chain",
    "ChainView": "chain",
    "ContractCandidate": "chain",
    "ContractRun": "service",
    "ContractSelectionRepository": "store",
    "ContractSelectionService": "service",
    "ContractSelector": "contract_selector",
    "DataReadinessProbe": "context",
    "FilesystemContractSelectionRepository": "store",
    "FilesystemStrategyRepository": "store",
    "SelectionContext": "contract_selector",
    "StrategyRegistry": "registry",
    "StrategyRegistryError": "registry",
    "StrategyRepository": "store",
    "StrategyRun": "service",
    "StrategyService": "service",
    "StrategySpecification": "registry",
    "build_selection_input": "context",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.strategies.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "STRATEGY_SCHEMA_VERSION",
    "ChainReader",
    "ChainView",
    "ContractCandidate",
    "ContractCostEstimate",
    "ContractRun",
    "ContractSelectionRepository",
    "ContractSelectionResult",
    "ContractSelectionRunResult",
    "ContractSelectionService",
    "ContractSelector",
    "DataReadiness",
    "DataReadinessProbe",
    "FilesystemContractSelectionRepository",
    "FilesystemStrategyRepository",
    "LegTemplate",
    "RejectedContract",
    "ResearchSummary",
    "SelectedLeg",
    "SelectionContext",
    "StrategyAgentOutput",
    "StrategyDecisionRecord",
    "StrategyOption",
    "StrategyOutputInvalidError",
    "StrategyRegistry",
    "StrategyRegistryError",
    "StrategyRepository",
    "StrategyRun",
    "StrategyRunResult",
    "StrategySelectionInput",
    "StrategyService",
    "StrategySpecification",
    "StrategyStructure",
    "StrikeRelationship",
    "build_selection_input",
    "validate_agent_output",
]
