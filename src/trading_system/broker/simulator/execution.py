"""Simulated order execution.

Milestone 2 deliberately ships no matching engine. The specification calls for
the simulator to *expose* the order interface so later milestones have
something to build against, while order execution itself lands in Milestone 8.

Rather than a stub that returns a plausible fill, this module reports
``NOT_IMPLEMENTED``. A simulated fill that looks real is exactly the kind of
output that makes an unfinished system appear to work.
"""

from __future__ import annotations

from trading_system.broker.base import OrderSubmissionNotImplementedError
from trading_system.domain.models import ExecutionResult, OrderIntent

__all__ = ["simulate_order_submission"]


def simulate_order_submission(intent: OrderIntent, broker_name: str) -> ExecutionResult:
    """Placeholder for the Milestone 8 matching engine.

    Always raises: no fill is fabricated, and no order is recorded.
    """
    raise OrderSubmissionNotImplementedError(broker_name)
