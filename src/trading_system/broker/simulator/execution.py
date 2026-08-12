"""Deterministic simulated order execution (Milestone 8).

A matching engine with no randomness and no market model. Everything it does is
decided by state a test set beforehand, so the same scenario produces the same
fills today and next year — which is what makes it usable for asserting on
partial fills, rejections and cancellations rather than merely exercising them.

What it deliberately does **not** do:

* **It does not fill by default.** A submitted order is ``SUBMITTED``, not
  ``FILLED``. Acknowledgement is not execution here for the same reason it is
  not at IBKR, and a simulator that filled everything instantly would hide
  every partial-fill bug in the layer above it.
* **It does not invent a price.** A fill's price is the limit price, or a price
  the scenario supplied. There is no spread model, no slippage model and no
  "realistic" jitter, because a number nobody chose is a number nobody can
  assert on.
* **It does not label itself as real.** Every result names ``SIMULATOR`` as the
  broker, and the trading mode travels with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import OrderStatus, TradingMode
from trading_system.domain.models import ExecutionResult, Fill, OrderIntent

__all__ = ["SimulatedOrder", "SimulatedOrderBook", "simulate_order_submission"]


@dataclass
class SimulatedOrder:
    """One order the simulator is holding, and what it has done so far."""

    broker_order_id: str
    intent: OrderIntent
    status: OrderStatus
    submitted_at: datetime
    filled_quantity: int = 0
    fills: list[Fill] = field(default_factory=list)
    message: str | None = None
    updated_at: datetime | None = None

    @property
    def remaining_quantity(self) -> int:
        return max(self.intent.quantity - self.filled_quantity, 0)

    @property
    def average_fill_price(self) -> Decimal | None:
        """Quantity-weighted, or ``None`` when nothing has traded."""
        if not self.fills:
            return None
        total = sum((f.price * Decimal(f.quantity) for f in self.fills), Decimal("0"))
        units = sum(f.quantity for f in self.fills)
        return total / Decimal(units) if units else None


@dataclass
class SimulatedOrderBook:
    """Everything the simulator knows about orders it has been sent.

    ``reject_next``, ``fill_on_submit`` and friends are how a test arranges a
    scenario: they are read, never written, by the engine, so the outcome of a
    submission is entirely determined before it happens.
    """

    orders: dict[str, SimulatedOrder] = field(default_factory=dict)
    next_order_id: int = 1

    #: Reject the next submission with this message, rather than accepting it.
    reject_next: str | None = None
    #: Raise this on the next submission, to exercise the ambiguous-submission
    #: path: the caller cannot tell whether the order arrived.
    raise_next: Exception | None = None
    #: Fill this many units immediately on acknowledgement. Zero — the default —
    #: means an acknowledged order is exactly that and nothing more.
    fill_on_submit: int = 0
    #: Price to fill at. ``None`` uses the order's own limit price, which is the
    #: only price the simulator has any right to assume.
    fill_price: Decimal | None = None
    #: Refuse the next cancellation, for exercising CANCEL_FAILED.
    fail_next_cancel: str | None = None

    def next_id(self) -> str:
        order_id = f"sim-{self.next_order_id:06d}"
        self.next_order_id += 1
        return order_id

    @property
    def open_orders(self) -> list[SimulatedOrder]:
        return [
            order
            for order in self.orders.values()
            if order.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
        ]


def simulate_order_submission(
    intent: OrderIntent,
    book: SimulatedOrderBook,
    *,
    broker_name: str,
    now: datetime,
    trading_mode: TradingMode,
) -> ExecutionResult:
    """Accept an order into the book and report what happened.

    Mutates ``book`` — that is the point of it — and returns the result the
    broker interface promises. The order is acknowledged, not filled, unless
    the scenario asked for a fill.
    """
    if book.raise_next is not None:
        error = book.raise_next
        book.raise_next = None
        # Record the order first: the whole value of this path is that it
        # models a broker which *received* the order and then failed to answer.
        # A simulator that discarded it would model the easy case instead.
        order_id = book.next_id()
        book.orders[order_id] = SimulatedOrder(
            broker_order_id=order_id,
            intent=intent,
            status=OrderStatus.SUBMITTED,
            submitted_at=now,
            updated_at=now,
            message="acknowledged by the broker; the client never saw the answer",
        )
        raise error

    if book.reject_next is not None:
        message = book.reject_next
        book.reject_next = None
        order_id = book.next_id()
        book.orders[order_id] = SimulatedOrder(
            broker_order_id=order_id,
            intent=intent,
            status=OrderStatus.REJECTED,
            submitted_at=now,
            updated_at=now,
            message=message,
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            broker=broker_name,
            broker_order_id=order_id,
            status=OrderStatus.REJECTED,
            orders_submitted=1,
            filled_quantity=0,
            submitted_at=now,
            last_update_at=now,
            message=message,
            trading_mode=trading_mode,
        )

    order_id = book.next_id()
    order = SimulatedOrder(
        broker_order_id=order_id,
        intent=intent,
        status=OrderStatus.SUBMITTED,
        submitted_at=now,
        updated_at=now,
    )
    book.orders[order_id] = order

    if book.fill_on_submit:
        fill_order(book, order_id, units=book.fill_on_submit, at=now, price=book.fill_price)

    return _result_of(order, broker_name=broker_name, now=now, trading_mode=trading_mode)


def fill_order(
    book: SimulatedOrderBook,
    broker_order_id: str,
    *,
    units: int,
    at: datetime,
    price: Decimal | None = None,
) -> SimulatedOrder:
    """Fill part or all of a resting order.

    Refuses to overfill: a broker cannot execute more than was sent, so a
    scenario that asks for it is a broken test rather than a market event worth
    modelling.
    """
    order = book.orders[broker_order_id]
    if units < 1:
        raise ValueError("a fill is at least one unit")
    if units > order.remaining_quantity:
        raise ValueError(
            f"cannot fill {units} units of an order with {order.remaining_quantity} remaining; "
            f"a broker never fills more than was submitted"
        )

    fill_price = price if price is not None else order.intent.limit_price
    if fill_price is None or fill_price <= 0:
        raise ValueError("a fill needs a price; the simulator invents none")

    order.fills.append(
        Fill(
            fill_id=f"{broker_order_id}-f{len(order.fills) + 1}",
            leg_index=0,
            quantity=units,
            price=fill_price,
            filled_at=at,
        )
    )
    order.filled_quantity += units
    order.status = (
        OrderStatus.FILLED if order.remaining_quantity == 0 else OrderStatus.PARTIALLY_FILLED
    )
    order.updated_at = at
    return order


def cancel_order(book: SimulatedOrderBook, broker_order_id: str, *, at: datetime) -> SimulatedOrder:
    """Cancel a resting order, preserving anything it already filled."""
    if book.fail_next_cancel is not None:
        message = book.fail_next_cancel
        book.fail_next_cancel = None
        raise ValueError(message)

    order = book.orders[broker_order_id]
    if order.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
        # Nothing to cancel. Reported as-is rather than as a failure: the order
        # already reached the state the caller wanted, or one it cannot leave.
        return order
    order.status = OrderStatus.CANCELLED
    order.updated_at = at
    return order


def _result_of(
    order: SimulatedOrder, *, broker_name: str, now: datetime, trading_mode: TradingMode
) -> ExecutionResult:
    return ExecutionResult(
        intent_id=order.intent.intent_id,
        broker=broker_name,
        broker_order_id=order.broker_order_id,
        status=order.status,
        orders_submitted=1,
        filled_quantity=order.filled_quantity,
        average_fill_price=order.average_fill_price,
        fills=list(order.fills),
        submitted_at=order.submitted_at,
        last_update_at=order.updated_at or now,
        message=order.message,
        trading_mode=trading_mode,
    )
