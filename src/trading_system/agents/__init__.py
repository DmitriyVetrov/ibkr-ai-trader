"""AI agents. They propose and explain; they never move money.

The hierarchy this package sits inside, and never inverts:

.. code-block:: text

    AI AGENTS  ->  DETERMINISTIC LAYER  ->  EXECUTION  ->  BROKER
      propose        validate / limit        submit        reality

An agent may research, classify, rank and explain. No agent may determine a
budget, a risk limit, a position size, or whether an order is permitted — those
are deterministic modules, and an agent cannot overturn one of their verdicts.

Three properties hold for every agent here:

* **Isolated.** An agent imports no broker, no data provider and no repository.
  It is handed a validated input contract and returns a structured object; a
  test enforces this by parsing imports.
* **Structured.** Output is a schema-validated object, never prose to be parsed.
* **Checked, not trusted.** The prompt states an agent's boundaries; a
  deterministic validator enforces them, and rejects a violating response in
  full rather than repairing it.

Delivered so far: the Universe Selector (Milestone 4), the Market Researcher
(Milestone 5) and the Strategy Selector (Milestone 6). The remaining three —
thesis monitor, position manager, evaluation analyst — arrive in Milestones 9
onwards and are absent rather than stubbed.

Note what the Strategy Selector does *not* do, because it is the boundary
Milestone 6 exists to draw: it chooses a strategy, never a contract. The
strike, the expiration and the legs are resolved deterministically by
:mod:`trading_system.strategies.contract_selector`, which consults no model at
all.
"""

from trading_system.agents.base import (
    AgentError,
    AgentInvalidOutputError,
    AgentTimeoutError,
    AgentUnavailableError,
    LLMClient,
    LLMResponse,
    ModelIdentity,
    StructuredRequest,
)

__all__ = [
    "AgentError",
    "AgentInvalidOutputError",
    "AgentTimeoutError",
    "AgentUnavailableError",
    "LLMClient",
    "LLMResponse",
    "ModelIdentity",
    "StructuredRequest",
]
