"""Fakes for IBKR objects and brokers.

The IBKR translation functions read attributes off whatever object they are
given, so ``SimpleNamespace`` fakes exercise them faithfully without needing
``ib_async`` or a gateway. The fakes deliberately reproduce IBKR's real
quirks — ``NaN`` for missing prices, ``0.0`` strikes on stock contracts,
``YYYYMMDD`` date strings — because those are what the code exists to handle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from trading_system.broker.base import OrderSubmissionNotImplementedError
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import TradingMode
from trading_system.domain.models import ExecutionResult, OrderIntent
from trading_system.infrastructure.clock import FixedClock

BROKER_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

NAN = float("nan")
#: IBKR's "unset double" sentinel.
UNSET_DOUBLE = 1.7976931348623157e308


@pytest.fixture
def broker_clock() -> FixedClock:
    return FixedClock(BROKER_NOW)


# ---------------------------------------------------------------------------
# IBKR object fakes
# ---------------------------------------------------------------------------
def make_option_contract(
    symbol: str = "NVDA",
    *,
    con_id: int = 100001,
    expiry: str = "20260918",
    strike: float = 180.0,
    right: str = "C",
) -> SimpleNamespace:
    return SimpleNamespace(
        secType="OPT",
        conId=con_id,
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right,
        multiplier="100",
        exchange="SMART",
        primaryExchange="",
        currency="USD",
        localSymbol=f"{symbol}  {expiry[2:]}{right}00180000",
        tradingClass=symbol,
    )


def make_stock_contract(symbol: str = "SPY", *, con_id: int = 100002) -> SimpleNamespace:
    return SimpleNamespace(
        secType="STK",
        conId=con_id,
        symbol=symbol,
        lastTradeDateOrContractMonth="",
        # IBKR reports 0.0, not None, for a stock's strike.
        strike=0.0,
        right="",
        multiplier="",
        exchange="SMART",
        primaryExchange="ARCA",
        currency="USD",
        localSymbol=symbol,
        tradingClass=symbol,
    )


def make_portfolio_item(
    contract: SimpleNamespace | None = None,
    *,
    position: float = 2.0,
    average_cost: float = 595.0,
    market_price: float = 6.25,
    market_value: float = 1250.0,
    unrealized: float = 60.0,
    realized: float = 0.0,
    account: str = "DU1234567",
) -> SimpleNamespace:
    """An ``ib_async.PortfolioItem``: carries valuation."""
    return SimpleNamespace(
        contract=contract or make_option_contract(),
        position=position,
        marketPrice=market_price,
        marketValue=market_value,
        averageCost=average_cost,
        unrealizedPNL=unrealized,
        realizedPNL=realized,
        account=account,
    )


def make_position(
    contract: SimpleNamespace | None = None,
    *,
    position: float = 2.0,
    avg_cost: float = 595.0,
    account: str = "DU1234567",
) -> SimpleNamespace:
    """An ``ib_async.Position``: quantity and cost only, no valuation."""
    return SimpleNamespace(
        account=account,
        contract=contract or make_option_contract(),
        position=position,
        avgCost=avg_cost,
    )


def make_trade(
    contract: SimpleNamespace | None = None,
    *,
    status: str = "Submitted",
    total_quantity: float = 3.0,
    filled: float = 0.0,
    remaining: float = 3.0,
    avg_fill_price: float = 0.0,
    order_id: int = 5,
    perm_id: int = 900001,
    action: str = "BUY",
    order_type: str = "LMT",
    limit_price: float = 5.80,
) -> SimpleNamespace:
    """An ``ib_async.Trade``: contract + order + orderStatus + log."""
    return SimpleNamespace(
        contract=contract or make_option_contract(),
        order=SimpleNamespace(
            orderId=order_id,
            clientId=1,
            permId=perm_id,
            action=action,
            totalQuantity=total_quantity,
            orderType=order_type,
            lmtPrice=limit_price,
            auxPrice=UNSET_DOUBLE,
            tif="DAY",
            account="DU1234567",
        ),
        orderStatus=SimpleNamespace(
            orderId=order_id,
            status=status,
            filled=filled,
            remaining=remaining,
            avgFillPrice=avg_fill_price,
            permId=perm_id,
        ),
        log=[SimpleNamespace(time=BROKER_NOW)],
    )


def make_fill(
    contract: SimpleNamespace | None = None,
    *,
    exec_id: str = "0000e1a7.68000001.01.01",
    side: str = "BOT",
    shares: float = 2.0,
    price: float = 5.95,
    commission: float | None = 1.30,
    perm_id: int = 900001,
    order_id: int = 5,
) -> SimpleNamespace:
    """An ``ib_async.Fill``: contract + execution + commissionReport."""
    return SimpleNamespace(
        contract=contract or make_option_contract(),
        execution=SimpleNamespace(
            execId=exec_id,
            time=BROKER_NOW,
            acctNumber="DU1234567",
            exchange="SMART",
            side=side,
            shares=shares,
            price=price,
            permId=perm_id,
            clientId=1,
            orderId=order_id,
            cumQty=shares,
            avgPrice=price,
        ),
        commissionReport=SimpleNamespace(
            execId=exec_id,
            commission=commission if commission is not None else NAN,
            currency="USD",
            realizedPNL=NAN,
        ),
        time=BROKER_NOW,
    )


def make_ticker(
    contract: SimpleNamespace | None = None,
    *,
    bid: float = 6.20,
    ask: float = 6.30,
    last: float = 6.25,
    close: float = 6.10,
    volume: float = 1500.0,
    average_daily_volume: float = 2500.0,
    market_data_type: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=contract or make_option_contract(),
        time=BROKER_NOW,
        marketDataType=market_data_type,
        bid=bid,
        ask=ask,
        last=last,
        close=close,
        volume=volume,
        # IBKR tick 21. Distinct from `volume` by default so a test cannot pass
        # by accident when the two are conflated.
        avVolume=average_daily_volume,
    )


def make_option_chain_row(
    *,
    exchange: str = "SMART",
    underlying_con_id: int = 100002,
    expirations: list[str] | None = None,
    strikes: list[float] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        exchange=exchange,
        underlyingConId=underlying_con_id,
        tradingClass="SPY",
        multiplier="100",
        expirations=expirations if expirations is not None else ["20260918", "20260821"],
        strikes=strikes if strikes is not None else [180.0, 175.0, 185.0],
    )


# ---------------------------------------------------------------------------
# Broker fakes
# ---------------------------------------------------------------------------
class RecordingBroker(SimulatedBroker):
    """A simulator that records every attempt to mutate broker state.

    Constructed writable on purpose. A read-only broker refuses an order before
    reaching the submission hook, which would make "no order was attempted"
    indistinguishable from "an order was attempted and blocked". Writable, any
    attempt is recorded — so an empty record proves the command never tried.
    """

    def __init__(self, state: SimulatedBrokerState | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("read_only", False)
        kwargs.setdefault("trading_mode", TradingMode.DRY_RUN)
        super().__init__(state, **kwargs)
        self.mutation_attempts: list[tuple[str, Any]] = []

    def _submit_order(self, intent: OrderIntent) -> ExecutionResult:
        self.mutation_attempts.append(("place_order", intent))
        raise OrderSubmissionNotImplementedError(self.name)

    def _cancel_order(self, broker_order_id: str) -> Any:
        self.mutation_attempts.append(("cancel_order", broker_order_id))
        raise OrderSubmissionNotImplementedError(self.name)

    @property
    def order_submission_count(self) -> int:
        return len([a for a in self.mutation_attempts if a[0] == "place_order"])


@pytest.fixture
def recording_broker(broker_clock: FixedClock) -> RecordingBroker:
    broker = RecordingBroker(clock=broker_clock)
    broker.connect()
    return broker


@pytest.fixture
def simulated_broker(broker_clock: FixedClock) -> SimulatedBroker:
    broker = SimulatedBroker(clock=broker_clock)
    broker.connect()
    return broker
