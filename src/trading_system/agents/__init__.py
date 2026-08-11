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

Delivered so far: the Universe Selector (Milestone 4). The remaining five —
market researcher, options strategist, thesis monitor, position manager,
evaluation analyst — arrive in Milestones 5 onwards and are absent rather than
stubbed.
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
