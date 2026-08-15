"""Stable telemetry attribute names, in one place (Milestone 11).

Constants rather than string literals scattered through the services, for the
same reason reason codes are an enum: an attribute name is part of a contract
with the dashboards and the alert rules that query it, and a typo produces a
*silently empty* panel rather than an error.

Three namespaces, and the separation is the point:

``trading.*``
    Domain artifact identifiers. These are what let a trace lead back to the
    immutable record that actually decided something. They are span and log
    attributes, **never metric labels** — one time series per execution id is
    how a metrics backend falls over.
``llm.*``
    What a model call cost and how it went. Never what was asked or answered:
    the prompt and the response live in the immutable research artifact, and a
    telemetry backend is not an audit archive.
``service.*`` / ``deployment.*``
    Resource attributes, set once per process.

Note what is deliberately absent: there is no attribute here for an account
number, a balance, a portfolio, a prompt, a completion, or any monetary
payload. :mod:`trading_system.observability.privacy` enforces that as well, but
the first line of defence is that there is no name to put them under.

This module imports nothing but the standard library, on purpose. It is
reachable from the agents and from every trading package, and those have
boundary tests that forbid brokers, repositories and sockets in their import
graphs — so the telemetry vocabulary has to be importable without dragging an
SDK behind it.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "AGENT_NAME",
    "AGENT_VERSION",
    "DEPLOYMENT_ENVIRONMENT",
    "ERROR_TYPE",
    "LLM_DURATION_MS",
    "LLM_INPUT_TOKENS",
    "LLM_MODEL",
    "LLM_OPERATION",
    "LLM_OUTPUT_TOKENS",
    "LLM_PROVIDER",
    "LLM_STATUS",
    "OPERATION_NAMES",
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "TRADING_ALLOCATION_ID",
    "TRADING_BROKER_ORDER_ID",
    "TRADING_CAMPAIGN_ID",
    "TRADING_CONTRACT_ID",
    "TRADING_EXECUTION_ID",
    "TRADING_EXIT_ID",
    "TRADING_MODE",
    "TRADING_POSITION_ID",
    "TRADING_REASON_CODE",
    "TRADING_RECONCILIATION_ID",
    "TRADING_RESEARCH_ID",
    "TRADING_RISK_ID",
    "TRADING_STATUS",
    "TRADING_STRATEGY",
    "TRADING_STRATEGY_ID",
    "TRADING_SYMBOL",
    "TRADING_UNIVERSE_ID",
]

# --- resource ---------------------------------------------------------------
SERVICE_NAME: Final = "service.name"
SERVICE_VERSION: Final = "service.version"
DEPLOYMENT_ENVIRONMENT: Final = "deployment.environment"

# --- domain identifiers -----------------------------------------------------
#
# These are DOMAIN artifact ids, not trace ids. Both levels exist and neither
# substitutes for the other: a trace id says which execution of the software,
# an execution id says which trade. A trace that carried only the first could
# not lead anybody to the record that matters.
TRADING_MODE: Final = "trading.mode"
TRADING_CAMPAIGN_ID: Final = "trading.campaign.id"
TRADING_UNIVERSE_ID: Final = "trading.universe.id"
TRADING_RESEARCH_ID: Final = "trading.research.id"
TRADING_STRATEGY_ID: Final = "trading.strategy.id"
TRADING_CONTRACT_ID: Final = "trading.contract.id"
TRADING_RISK_ID: Final = "trading.risk.id"
TRADING_ALLOCATION_ID: Final = "trading.allocation.id"
TRADING_EXECUTION_ID: Final = "trading.execution.id"
TRADING_POSITION_ID: Final = "trading.position.id"
TRADING_EXIT_ID: Final = "trading.exit.id"
TRADING_BROKER_ORDER_ID: Final = "trading.broker_order.id"
TRADING_RECONCILIATION_ID: Final = "trading.reconciliation.id"
TRADING_PNL_ID: Final = "trading.pnl.id"
TRADING_JOB: Final = "trading.job.name"

# --- low-cardinality descriptors -------------------------------------------
#
# Safe as metric labels as well as span attributes: a closed vocabulary, or a
# value bounded by the configuration.
TRADING_STATUS: Final = "trading.status"
TRADING_REASON_CODE: Final = "trading.reason_code"
TRADING_REASON_CATEGORY: Final = "trading.reason_category"
TRADING_STRATEGY: Final = "trading.strategy"
#: A span attribute only. Deliberately *not* a metric label: a universe of a
#: few hundred underlyings is a few hundred time series per metric, and the
#: cardinality guard refuses it.
TRADING_SYMBOL: Final = "trading.symbol"
ERROR_TYPE: Final = "error.type"

# --- AI ---------------------------------------------------------------------
#
# Cost and outcome. Never content: no prompt, no completion, no research
# context. Those are in the immutable artifact, where they can be audited and
# where nobody has to trust a telemetry retention policy with them.
AGENT_NAME: Final = "agent.name"
AGENT_VERSION: Final = "agent.version"
LLM_PROVIDER: Final = "llm.provider"
LLM_MODEL: Final = "llm.model"
LLM_OPERATION: Final = "llm.operation"
LLM_STATUS: Final = "llm.status"
LLM_INPUT_TOKENS: Final = "llm.input_tokens"
LLM_OUTPUT_TOKENS: Final = "llm.output_tokens"
LLM_DURATION_MS: Final = "llm.duration_ms"

#: The business operations this system emits spans for. A closed list, checked
#: by a test, so a new span name is a deliberate addition rather than a typo
#: that quietly creates a second operation nobody is querying.
OPERATION_NAMES: Final = (
    "trading.workflow",
    "universe.selection",
    "research.run",
    "evidence.assemble",
    "research.validate",
    "strategy.selection",
    "contract.selection",
    "risk.evaluate",
    "allocation.calculate",
    "execution.open",
    "execution.close",
    "execution.validate",
    "order.build",
    "broker.submit",
    "broker.observe",
    "position.monitor",
    "exit.evaluate",
    "exit.decision",
    "exit.execute",
    "exit.confirm",
    "reconciliation.run",
    "pnl.compute",
    "pnl.settle",
    "ops.health",
    "llm.generate",
    #: The Milestone 12 observability acceptance gate. Not a business
    #: operation — it exists to prove the telemetry pipeline works end to end,
    #: which is why it carries no domain identifier of any kind.
    "readiness.acceptance",
)
