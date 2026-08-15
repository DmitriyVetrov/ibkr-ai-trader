"""Deterministic in-process broker used by tests and dry runs.

Holds a fixed account, positions, orders and executions supplied at
construction. Nothing here touches the network, and nothing changes unless a
test changes it, so a test that passes today passes tomorrow.

The default fixture is deliberately *not* empty: an all-zero account would let
code that mishandles positions or orders pass unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from trading_system.broker.base import (
    Broker,
    BrokerConnectionError,
    BrokerResponseError,
    MarketDataUnavailableError,
    OptionChainUnavailableError,
)
from trading_system.broker.simulator.execution import (
    SimulatedOrder,
    SimulatedOrderBook,
    cancel_order,
    simulate_order_submission,
)
from trading_system.broker.simulator.market import (
    SIMULATED_SOURCE,
    simulated_option_chain,
    simulated_quote,
    simulated_reference_price,
)
from trading_system.domain.enums import (
    BrokerConnectionState,
    LegAction,
    OptionRight,
    OrderSide,
    OrderStatus,
    SecurityType,
    TradingMode,
)
from trading_system.domain.models import (
    BrokerAccount,
    BrokerContract,
    BrokerExecution,
    BrokerHealth,
    BrokerOrder,
    BrokerPosition,
    ExecutionResult,
    MarketDataSnapshot,
    OptionChainSnapshot,
    OrderIntent,
)
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = ["SimulatedBroker", "SimulatedBrokerState", "default_simulated_state"]

DEFAULT_ACCOUNT_ID = "DU0000000"


@dataclass
class SimulatedBrokerState:
    """Everything the simulated broker will report.

    Mutable so a test can arrange a scenario, but never mutated by the broker
    itself during Milestone 2 — every operation is read-only.
    """

    account_id: str = DEFAULT_ACCOUNT_ID
    currency: str = "EUR"
    cash: Decimal = Decimal("100000.00")
    net_liquidation: Decimal = Decimal("100000.00")
    buying_power: Decimal = Decimal("400000.00")
    available_funds: Decimal = Decimal("98000.00")
    initial_margin: Decimal = Decimal("2000.00")
    maintenance_margin: Decimal = Decimal("1500.00")
    excess_liquidity: Decimal = Decimal("96500.00")
    unrealized_pnl: Decimal = Decimal("125.00")
    realized_pnl: Decimal = Decimal("0.00")

    positions: list[BrokerPosition] = field(default_factory=list)
    open_orders: list[BrokerOrder] = field(default_factory=list)
    executions: list[BrokerExecution] = field(default_factory=list)

    #: Orders this simulator has been sent (Milestone 8). Separate from
    #: ``open_orders``, which is the pre-existing fixture: a test asserting
    #: "this run submitted one order" must not have to subtract the scenery.
    book: SimulatedOrderBook = field(default_factory=SimulatedOrderBook)

    #: Whether ``get_positions`` reports the scenery net of this simulator's
    #: own fills. Off by default so every pre-existing scenario is unchanged;
    #: on, it models a broker that updates its position list when an order
    #: fills, which is the only way a test can tell a *fill report* apart from
    #: a *position read*.
    net_fills_into_positions: bool = False

    #: Symbols the simulator refuses to quote, for exercising the
    #: "market data unavailable" path without touching a real broker.
    unquotable_symbols: set[str] = field(default_factory=set)
    #: Underlyings with no option chain.
    chainless_symbols: set[str] = field(default_factory=set)


def default_simulated_state(
    clock: Clock, account_id: str = DEFAULT_ACCOUNT_ID
) -> SimulatedBrokerState:
    """A small but non-trivial portfolio: one option position and one stock."""
    now = clock.now()
    return SimulatedBrokerState(
        account_id=account_id,
        positions=[
            BrokerPosition(
                account_id=account_id,
                symbol="NVDA",
                security_type=SecurityType.OPTION,
                as_of=now,
                source=SIMULATED_SOURCE,
                contract_id=100001,
                local_symbol="NVDA  260918C00180000",
                currency="USD",
                multiplier=100,
                quantity=Decimal("2"),
                average_cost=Decimal("595.00"),
                expiration=date(2026, 9, 18),
                strike=Decimal("180.00"),
                right=OptionRight.CALL,
                market_price=Decimal("6.25"),
                market_value=Decimal("1250.00"),
                unrealized_pnl=Decimal("60.00"),
                realized_pnl=Decimal("0.00"),
            ),
            BrokerPosition(
                account_id=account_id,
                symbol="SPY",
                security_type=SecurityType.STOCK,
                as_of=now,
                source=SIMULATED_SOURCE,
                contract_id=100002,
                local_symbol="SPY",
                currency="USD",
                multiplier=1,
                quantity=Decimal("10"),
                average_cost=Decimal("500.00"),
                market_price=Decimal("505.00"),
                market_value=Decimal("5050.00"),
                unrealized_pnl=Decimal("50.00"),
                realized_pnl=Decimal("0.00"),
            ),
        ],
        open_orders=[
            BrokerOrder(
                broker_order_id="sim-order-1",
                account_id=account_id,
                as_of=now,
                source=SIMULATED_SOURCE,
                client_order_id="1",
                perm_id=900001,
                contract_id=100001,
                symbol="NVDA",
                security_type=SecurityType.OPTION,
                local_symbol="NVDA  260918C00180000",
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                order_type="LMT",
                limit_price=Decimal("5.80"),
                time_in_force="DAY",
                status=OrderStatus.SUBMITTED,
                filled_quantity=Decimal("0"),
                remaining_quantity=Decimal("1"),
                submitted_at=now,
                updated_at=now,
            )
        ],
        executions=[
            BrokerExecution(
                execution_id="sim-exec-1",
                broker_order_id="sim-order-0",
                account_id=account_id,
                as_of=now,
                source=SIMULATED_SOURCE,
                contract_id=100001,
                symbol="NVDA",
                security_type=SecurityType.OPTION,
                side=OrderSide.BUY,
                quantity=Decimal("2"),
                price=Decimal("5.95"),
                executed_at=now,
                commission=Decimal("1.30"),
                currency="USD",
            )
        ],
    )


class SimulatedBroker(Broker):
    """An in-process broker backed by fixed state.

    Read-only by default like every other broker in the system. Constructed
    with ``read_only=False`` it can execute against the deterministic matching
    engine in :mod:`trading_system.broker.simulator.execution` — no network, no
    randomness, and every outcome arranged by the scenario beforehand.
    """

    def __init__(
        self,
        state: SimulatedBrokerState | None = None,
        *,
        clock: Clock | None = None,
        trading_mode: TradingMode = TradingMode.DRY_RUN,
        read_only: bool = True,
        fail_to_connect: bool = False,
    ) -> None:
        super().__init__(trading_mode=trading_mode, read_only=read_only)
        self._clock = clock or SystemClock()
        self._state = state if state is not None else default_simulated_state(self._clock)
        self._connected = False
        self._fail_to_connect = fail_to_connect
        self._last_communication = None if not self._connected else self._clock.now()

    @property
    def name(self) -> str:
        return SIMULATED_SOURCE

    @property
    def state(self) -> SimulatedBrokerState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._connected

    # --- connection --------------------------------------------------------
    def connect(self) -> BrokerHealth:
        if self._fail_to_connect:
            raise BrokerConnectionError("simulated connection failure")
        self._connected = True
        self._last_communication = self._clock.now()
        return self.health_check()

    def disconnect(self) -> None:
        self._connected = False

    def health_check(self) -> BrokerHealth:
        return BrokerHealth(
            broker=self.name,
            state=(
                BrokerConnectionState.CONNECTED
                if self._connected
                else BrokerConnectionState.DISCONNECTED
            ),
            as_of=self._clock.now(),
            trading_mode=self.trading_mode,
            host="in-process",
            port=None,
            account_id=self._state.account_id if self._connected else None,
            read_only=self.read_only,
            latency_ms=0.0,
            last_successful_communication=self._last_communication,
        )

    def _require_connection(self) -> None:
        if not self._connected:
            raise BrokerConnectionError(f"{self.name} is not connected")

    # --- read-only state ---------------------------------------------------
    def get_account(self) -> BrokerAccount:
        self._require_connection()
        s = self._state
        return BrokerAccount(
            account_id=s.account_id,
            currency=s.currency,
            as_of=self._clock.now(),
            source=self.name,
            cash=s.cash,
            net_liquidation=s.net_liquidation,
            buying_power=s.buying_power,
            available_funds=s.available_funds,
            initial_margin=s.initial_margin,
            maintenance_margin=s.maintenance_margin,
            excess_liquidity=s.excess_liquidity,
            unrealized_pnl=s.unrealized_pnl,
            realized_pnl=s.realized_pnl,
        )

    def get_account_summary(self) -> dict[str, str]:
        account = self.get_account()
        return {
            "AccountId": account.account_id,
            "Currency": account.currency,
            "TotalCashValue": str(account.cash),
            "NetLiquidation": str(account.net_liquidation),
            "BuyingPower": str(account.buying_power),
            "AvailableFunds": str(account.available_funds),
            "FullInitMarginReq": str(account.initial_margin),
            "FullMaintMarginReq": str(account.maintenance_margin),
            "ExcessLiquidity": str(account.excess_liquidity),
            "UnrealizedPnL": str(account.unrealized_pnl),
            "RealizedPnL": str(account.realized_pnl),
        }

    def get_positions(self) -> list[BrokerPosition]:
        """The scenery, optionally net of anything this simulator has filled.

        Positions are static scenery by default, which is what every suite
        before Milestone 9 needs: a test arranges a portfolio and the broker
        reports it back unchanged.

        ``net_fills_into_positions`` models the other half of a real broker —
        one that *updates its position list when an order fills*. Without it
        no test can distinguish "the fill report said one contract traded" from
        "the account now holds none of it", and that distinction is the entire
        difference between an order this system sent and a position this system
        no longer has. It is off by default so no existing scenario changes.
        """
        self._require_connection()
        if not self._state.net_fills_into_positions:
            return list(self._state.positions)
        return [
            position
            for position in (self._netted(position) for position in self._state.positions)
            if position is not None
        ]

    def _netted(self, position: BrokerPosition) -> BrokerPosition | None:
        """One position, less whatever this simulator has filled against it.

        A holding netted to zero is *absent*, not reported as zero: that is how
        IBKR reports a closed position, and a simulator that returned a
        zero-quantity row would let code pass that a real account would break.
        """
        traded = sum(
            (
                Decimal(order.filled_quantity)
                * Decimal(leg.ratio)
                * (Decimal(-1) if leg.action is LegAction.SELL else Decimal(1))
                for order in self._state.book.orders.values()
                for leg in order.intent.legs
                if leg.broker_contract_id == position.contract_id and order.filled_quantity
            ),
            Decimal("0"),
        )
        if not traded:
            return position
        remaining = position.quantity + traded
        if remaining == 0:
            return None
        return position.model_copy(update={"quantity": remaining})

    def get_open_orders(self) -> list[BrokerOrder]:
        """Scenery orders plus anything this simulator has actually been sent.

        The second half matters from Milestone 9 onwards: resolving an
        ambiguous submission means asking the broker what it has, and a
        simulator that never reported its own working orders could not be used
        to test that at all. Orders the book has finished with (filled,
        cancelled, rejected) are not open and are not listed.
        """
        self._require_connection()
        now = self._clock.now()
        working = [self._to_broker_order(order, now) for order in self._state.book.open_orders]
        return [*self._state.open_orders, *working]

    def get_executions(self) -> list[BrokerExecution]:
        self._require_connection()
        return list(self._state.executions)

    def get_contract(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
        *,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> BrokerContract:
        self._require_connection()
        return BrokerContract(
            symbol=symbol.upper(),
            security_type=security_type,
            as_of=self._clock.now(),
            source=self.name,
            contract_id=abs(hash(symbol.upper())) % 1_000_000,
            exchange=exchange or "SMART",
            currency=currency or "USD",
            local_symbol=symbol.upper(),
            trading_class=symbol.upper(),
            multiplier=100 if security_type is SecurityType.OPTION else 1,
        )

    def get_market_data(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
    ) -> MarketDataSnapshot:
        self._require_connection()
        if symbol.upper() in self._state.unquotable_symbols:
            raise MarketDataUnavailableError(
                f"MARKET_DATA_UNAVAILABLE: simulator has no quote for {symbol.upper()}"
            )
        return simulated_quote(symbol, self._clock, security_type)

    def get_option_chain(self, underlying: str) -> OptionChainSnapshot:
        self._require_connection()
        if underlying.upper() in self._state.chainless_symbols:
            raise OptionChainUnavailableError(
                f"simulator has no option chain for {underlying.upper()}"
            )
        return simulated_option_chain(underlying, self._clock)

    def reference_price(self, symbol: str) -> Decimal:
        """Expose the deterministic reference price used by the simulated quote."""
        return simulated_reference_price(symbol)

    # --- execution (Milestone 8) -------------------------------------------
    @property
    def book(self) -> SimulatedOrderBook:
        """Orders this broker has been sent. Empty until something submits one."""
        return self._state.book

    @property
    def submitted_orders(self) -> list[SimulatedOrder]:
        return list(self._state.book.orders.values())

    def _submit_order(self, intent: OrderIntent) -> ExecutionResult:
        """Route an order into the deterministic matching engine.

        Reached only through :meth:`Broker.place_order`, which refuses while
        read-only and has already counted the attempt.
        """
        self._require_connection()
        return simulate_order_submission(
            intent,
            self._state.book,
            broker_name=self.name,
            now=self._clock.now(),
            trading_mode=self.trading_mode,
        )

    def _cancel_order(self, broker_order_id: str) -> BrokerOrder:
        self._require_connection()
        if broker_order_id not in self._state.book.orders:
            raise BrokerResponseError(
                f"{self.name} has no order {broker_order_id}; refusing to report a "
                f"cancellation of an order it never received"
            )
        now = self._clock.now()
        order = cancel_order(self._state.book, broker_order_id, at=now)
        return self._to_broker_order(order, now)

    def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        """Look one submitted order up. ``None`` when this broker never saw it."""
        self._require_connection()
        order = self._state.book.orders.get(broker_order_id)
        return None if order is None else self._to_broker_order(order, self._clock.now())

    def _to_broker_order(self, order: SimulatedOrder, now: datetime) -> BrokerOrder:
        leg = order.intent.legs[0]
        return BrokerOrder(
            broker_order_id=order.broker_order_id,
            account_id=self._state.account_id,
            as_of=now,
            source=self.name,
            contract_id=leg.broker_contract_id,
            symbol=order.intent.underlying,
            security_type=SecurityType.OPTION,
            local_symbol=leg.occ_symbol,
            side=OrderSide.BUY if leg.action is LegAction.BUY else OrderSide.SELL,
            quantity=Decimal(order.intent.quantity),
            order_type=order.intent.order_type.value,
            limit_price=order.intent.limit_price,
            time_in_force=order.intent.time_in_force.value,
            status=order.status,
            filled_quantity=Decimal(order.filled_quantity),
            remaining_quantity=Decimal(order.remaining_quantity),
            average_fill_price=order.average_fill_price,
            submitted_at=order.submitted_at,
            updated_at=order.updated_at or now,
        )
