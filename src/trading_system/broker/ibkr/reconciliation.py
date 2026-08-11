"""Reconciliation between internal state and broker state (specification section 20).

The rule is absolute: **the broker is authoritative**. When internal records
and the broker disagree, the broker is right and the internal record is wrong.

This module therefore *detects and reports*; it never resolves. It does not
adjust internal state, it does not place a correcting trade, and it does not
decide which side is stale. It returns a structured
:class:`~trading_system.domain.models.ReconciliationReport`, and any status
other than ``MATCHED`` blocks new executions until a human resolves it.

The comparison operates purely on domain models, so it works against any
:class:`~trading_system.broker.base.Broker` — the simulator included. It lives
under ``ibkr/`` because that is where the specification places it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_system.broker.base import Broker, BrokerError
from trading_system.domain.enums import (
    DiscrepancyType,
    ReconciliationStatus,
)
from trading_system.domain.models import (
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    ReconciliationDiscrepancy,
    ReconciliationReport,
)
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = ["PositionKey", "Reconciler", "position_key"]

#: Identity of a position for comparison purposes.
#:
#: Deliberately not the broker contract id: internal state may have been
#: written before a contract id was known. Symbol plus option terms identifies
#: the same instrument from either side.
PositionKey = tuple[str, str, str, str, str]


def position_key(position: BrokerPosition) -> PositionKey:
    """Build a comparable identity for a position."""
    return (
        position.symbol.upper(),
        position.security_type.value,
        position.expiration.isoformat() if position.expiration else "",
        str(position.strike) if position.strike is not None else "",
        position.right.value if position.right else "",
    )


class Reconciler:
    """Compares internal state against broker state.

    Milestone 2 supplies internal state directly to :meth:`reconcile`. Once a
    position repository exists (Milestone 9) it will supply the same models.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()

    def reconcile(
        self,
        broker: Broker,
        *,
        internal_positions: Sequence[BrokerPosition] = (),
        internal_orders: Sequence[BrokerOrder] = (),
        internal_executions: Sequence[BrokerExecution] = (),
    ) -> ReconciliationReport:
        """Compare internal state against what the broker reports.

        An unreachable broker yields ``BROKER_UNAVAILABLE`` rather than an
        exception: not knowing is a legitimate — and blocking — outcome.
        """
        now = self._clock.now()

        try:
            broker_positions = broker.get_positions()
            broker_orders = broker.get_open_orders()
            broker_executions = broker.get_executions()
        except BrokerError as exc:
            return ReconciliationReport(
                as_of=now,
                broker=broker.name,
                status=ReconciliationStatus.BROKER_UNAVAILABLE,
                discrepancies=[
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.UNKNOWN,
                        identifier=broker.name,
                        description=f"broker state could not be read: {exc}",
                    )
                ],
            )

        discrepancies: list[ReconciliationDiscrepancy] = []
        discrepancies += self._compare_positions(internal_positions, broker_positions)
        discrepancies += self._compare_orders(internal_orders, broker_orders)
        discrepancies += self._compare_executions(internal_executions, broker_executions)

        return ReconciliationReport(
            as_of=now,
            broker=broker.name,
            status=(
                ReconciliationStatus.MISMATCH if discrepancies else ReconciliationStatus.MATCHED
            ),
            discrepancies=discrepancies,
            positions_compared=max(len(internal_positions), len(broker_positions)),
            orders_compared=max(len(internal_orders), len(broker_orders)),
            executions_compared=max(len(internal_executions), len(broker_executions)),
        )

    # --- comparisons -------------------------------------------------------
    @staticmethod
    def _compare_positions(
        internal: Sequence[BrokerPosition], broker: Sequence[BrokerPosition]
    ) -> list[ReconciliationDiscrepancy]:
        internal_by_key = {position_key(p): p for p in internal}
        broker_by_key = {position_key(p): p for p in broker}
        discrepancies: list[ReconciliationDiscrepancy] = []

        for key in sorted(set(internal_by_key) | set(broker_by_key)):
            ours = internal_by_key.get(key)
            theirs = broker_by_key.get(key)
            identifier = "|".join(part for part in key if part)

            if theirs is None:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.POSITION_MISMATCH,
                        identifier=identifier,
                        description=(
                            "internal state holds a position the broker does not report; "
                            "do not assume it exists"
                        ),
                        internal_value=str(ours.quantity) if ours else None,
                        broker_value=None,
                    )
                )
                continue

            if ours is None:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.POSITION_MISMATCH,
                        identifier=identifier,
                        description=(
                            "the broker reports a position unknown to internal state; "
                            "it is real and must be accounted for"
                        ),
                        internal_value=None,
                        broker_value=str(theirs.quantity),
                    )
                )
                continue

            if ours.quantity != theirs.quantity:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.POSITION_MISMATCH,
                        identifier=identifier,
                        description="position quantity disagrees; the broker is authoritative",
                        internal_value=str(ours.quantity),
                        broker_value=str(theirs.quantity),
                    )
                )

        return discrepancies

    @staticmethod
    def _compare_orders(
        internal: Sequence[BrokerOrder], broker: Sequence[BrokerOrder]
    ) -> list[ReconciliationDiscrepancy]:
        internal_by_id = {o.broker_order_id: o for o in internal}
        broker_by_id = {o.broker_order_id: o for o in broker}
        discrepancies: list[ReconciliationDiscrepancy] = []

        for order_id in sorted(set(internal_by_id) | set(broker_by_id)):
            ours = internal_by_id.get(order_id)
            theirs = broker_by_id.get(order_id)

            if theirs is None:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.ORDER_MISMATCH,
                        identifier=order_id,
                        description=(
                            "internal state believes this order is open; the broker "
                            "does not report it"
                        ),
                        internal_value=ours.status.value if ours else None,
                        broker_value=None,
                    )
                )
                continue

            if ours is None:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.ORDER_MISMATCH,
                        identifier=order_id,
                        description="the broker reports an open order unknown to internal state",
                        internal_value=None,
                        broker_value=theirs.status.value,
                    )
                )
                continue

            if ours.status != theirs.status:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.ORDER_MISMATCH,
                        identifier=order_id,
                        description="order status disagrees; the broker is authoritative",
                        internal_value=ours.status.value,
                        broker_value=theirs.status.value,
                    )
                )

            if _quantity(ours.filled_quantity) != _quantity(theirs.filled_quantity):
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.ORDER_MISMATCH,
                        identifier=order_id,
                        description=(
                            "filled quantity disagrees; a submitted order is not a filled order"
                        ),
                        internal_value=str(ours.filled_quantity),
                        broker_value=str(theirs.filled_quantity),
                    )
                )

        return discrepancies

    @staticmethod
    def _compare_executions(
        internal: Sequence[BrokerExecution], broker: Sequence[BrokerExecution]
    ) -> list[ReconciliationDiscrepancy]:
        internal_by_id = {e.execution_id: e for e in internal}
        broker_by_id = {e.execution_id: e for e in broker}
        discrepancies: list[ReconciliationDiscrepancy] = []

        # Only executions the broker knows about are real. An execution missing
        # from internal state is a gap in our records; the reverse means we
        # recorded a fill that never happened, which is worse.
        for execution_id in sorted(set(internal_by_id) - set(broker_by_id)):
            ours = internal_by_id[execution_id]
            discrepancies.append(
                ReconciliationDiscrepancy(
                    discrepancy_type=DiscrepancyType.EXECUTION_MISMATCH,
                    identifier=execution_id,
                    description=(
                        "internal state records a fill the broker does not report; "
                        "it must not be treated as real"
                    ),
                    internal_value=f"{ours.quantity} @ {ours.price}",
                    broker_value=None,
                )
            )

        for execution_id in sorted(set(broker_by_id) - set(internal_by_id)):
            theirs = broker_by_id[execution_id]
            discrepancies.append(
                ReconciliationDiscrepancy(
                    discrepancy_type=DiscrepancyType.EXECUTION_MISMATCH,
                    identifier=execution_id,
                    description="the broker reports a fill missing from internal state",
                    internal_value=None,
                    broker_value=f"{theirs.quantity} @ {theirs.price}",
                )
            )

        for execution_id in sorted(set(internal_by_id) & set(broker_by_id)):
            ours = internal_by_id[execution_id]
            theirs = broker_by_id[execution_id]
            if ours.quantity != theirs.quantity or ours.price != theirs.price:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.EXECUTION_MISMATCH,
                        identifier=execution_id,
                        description="fill quantity or price disagrees; the broker is authoritative",
                        internal_value=f"{ours.quantity} @ {ours.price}",
                        broker_value=f"{theirs.quantity} @ {theirs.price}",
                    )
                )

        return discrepancies


def _quantity(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal("0")
