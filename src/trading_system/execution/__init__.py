"""Order execution: the first stage permitted to send an order to a broker.

Milestone 8. Everything before it proposes, validates, authorises or sizes;
this is where an authorisation becomes an instruction:

.. code-block:: text

    APPROVED CampaignAllocation (M7)
          |
    ExecutionRequest              <- a deliberate authorisation, not an id
          |
    deterministic validation      <- windows, session, currency, structure
          |
    PurchaseCard + RiskDecision   <- the M1 artifacts, minted not forked
          |
    OrderIntent                   <- the only place a price becomes an order
          |
    idempotency check             <- has this exact trade already been sent?
          |
    Broker                        <- one submission, one short-lived connection
          |
    immutable record + events     <- written BEFORE the send, appended after

Six rules govern it, each with tests that fail loudly:

* **An allocation id is not permission to trade.** Two independent switches
  must both be on: ``execution.enabled`` in configuration and an explicit
  authorisation on the request. ``ExecutionRequest`` cannot even be constructed
  with ``execution_authorized=False``, so there is no shape in which "load this
  allocation" and "send this order" are the same call.
* **Execution changes nothing it was given.** Quantity, capital, maximum loss,
  contract and strategy are copied from the authorisation. When a broker
  refuses because the market moved, the answer is a recorded failure and a new
  Milestone 7 authorisation — never a smaller order that fits.
* **An acknowledgement is not a fill.** ``SUBMITTED`` means IBKR took the
  order. Only an execution report produces ``FILLED`` or ``PARTIALLY_FILLED``,
  and where a broker's status and its own counts disagree, the counts win.
* **Ambiguity fails closed.** A timeout after a submission is ``UNKNOWN``, not
  "safe to retry": the order may be live right now. There is no code path from
  an uncertain submission to a second one, and ``auto_retry_on_timeout`` fails
  to load. Resolution is by observing the broker.
* **One trade, one order.** Submission identity is derived from the
  authorisation, the mode, the order type and the policy version, and an
  attempt already in a state where an order *may* exist blocks another —
  including ``SUBMISSION_PENDING`` and ``UNKNOWN``, because absence of an
  acknowledgement is not absence of an order.
* **A multi-leg structure is one order.** A straddle goes to IBKR as a combo,
  so it fills as a structure or not at all. Independent leg orders fail to load
  in configuration: a half-filled straddle is a naked long call against limits
  nobody checked.

``NO_TRADE`` remains upstream's answer, and this stage adds its own first-class
outcomes: ``ALREADY_SUBMITTED`` is not a failure, and a run that submitted
nothing because everything was refused is a recorded result rather than an
error.

Nothing here consults a model. There is no LLM client, no prompt and no agent
in the import graph, and a test asserts it transitively — an execution engine
translates an already-approved deterministic artifact, and there is nothing in
that for a model to decide.
"""

from typing import TYPE_CHECKING, Any

from trading_system.execution.models import (
    EXECUTION_SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionLeg,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionRunCounts,
    ExecutionRunResult,
    execution_identifier,
    execution_request_identifier,
    execution_run_identifier,
)
from trading_system.execution.state_machine import (
    ALLOWED_EXECUTION_TRANSITIONS,
    ExecutionStateMachine,
    ExecutionTransition,
    InvalidExecutionTransitionError,
    can_transition,
    is_terminal,
    validate_transition,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.execution.execution_engine import ExecutionEngine, SubmissionOutcome
    from trading_system.execution.fill_tracker import (
        event_from_broker_order,
        event_from_execution_result,
        state_for,
    )
    from trading_system.execution.order_builder import (
        OrderBuildError,
        build_order_intent,
        limit_price_from_reference,
    )
    from trading_system.execution.purchase_card import (
        PurchaseCardError,
        build_purchase_card,
        build_risk_decision,
    )
    from trading_system.execution.report import render_execution, render_execution_run
    from trading_system.execution.service import ExecutionPlan, ExecutionRun, ExecutionService
    from trading_system.execution.store import (
        ExecutionHistoryEntry,
        ExecutionRepository,
        ExecutionStoreError,
        FilesystemExecutionRepository,
    )
    from trading_system.execution.validation import ExecutionValidation, ExecutionValidator

#: Members loaded on first access rather than at import time.
#:
#: The same discipline the data, research, universe, strategy, risk and
#: allocation packages follow, and here for the strongest reason of any of
#: them: an eager re-export of ``service`` or ``execution_engine`` would put a
#: **writable broker** into the import graph of anything that merely names an
#: execution *type*. The whole milestone rests on order submission being hard
#: to reach; do not "tidy" the ``__getattr__`` away.
_LAZY = {
    "ExecutionEngine": "execution_engine",
    "ExecutionHistoryEntry": "store",
    "ExecutionPlan": "service",
    "ExecutionRepository": "store",
    "ExecutionRun": "service",
    "ExecutionService": "service",
    "ExecutionStoreError": "store",
    "ExecutionValidation": "validation",
    "ExecutionValidator": "validation",
    "FilesystemExecutionRepository": "store",
    "OrderBuildError": "order_builder",
    "PurchaseCardError": "purchase_card",
    "SubmissionOutcome": "execution_engine",
    "build_order_intent": "order_builder",
    "build_purchase_card": "purchase_card",
    "build_risk_decision": "purchase_card",
    "event_from_broker_order": "fill_tracker",
    "event_from_execution_result": "fill_tracker",
    "limit_price_from_reference": "order_builder",
    "render_execution": "report",
    "render_execution_run": "report",
    "state_for": "fill_tracker",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.execution.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ALLOWED_EXECUTION_TRANSITIONS",
    "EXECUTION_SCHEMA_VERSION",
    "ExecutionEngine",
    "ExecutionEvent",
    "ExecutionHistoryEntry",
    "ExecutionLeg",
    "ExecutionPlan",
    "ExecutionRecord",
    "ExecutionRepository",
    "ExecutionRequest",
    "ExecutionRun",
    "ExecutionRunCounts",
    "ExecutionRunResult",
    "ExecutionService",
    "ExecutionStateMachine",
    "ExecutionStoreError",
    "ExecutionTransition",
    "ExecutionValidation",
    "ExecutionValidator",
    "FilesystemExecutionRepository",
    "InvalidExecutionTransitionError",
    "OrderBuildError",
    "PurchaseCardError",
    "SubmissionOutcome",
    "build_order_intent",
    "build_purchase_card",
    "build_risk_decision",
    "can_transition",
    "event_from_broker_order",
    "event_from_execution_result",
    "execution_identifier",
    "execution_request_identifier",
    "execution_run_identifier",
    "is_terminal",
    "limit_price_from_reference",
    "render_execution",
    "render_execution_run",
    "state_for",
    "validate_transition",
]
