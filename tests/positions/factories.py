"""Deterministic builders for the Milestone 9 suites.

Shared by ``tests/positions``, ``tests/reservations`` and
``tests/reconciliation``, because all three need the same three things: broker
state to observe, execution records to believe, and reservations to move.

Two rules hold throughout:

* **Nothing here reaches a broker.** Every object is constructed in process.
* **Everything is exact.** Fixed instants, fixed ids, decimal money. A test
  that passes today passes tomorrow, and a hash printed in an assertion means
  something.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from trading_system.domain.enums import (
    ExecutionReasonCode,
    ExecutionState,
    LegAction,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    ReservationReasonCode,
    ReservationState,
    SecurityType,
    StrategyType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import (
    BrokerAccount,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    SystemVersions,
)
from trading_system.execution.models import ExecutionLeg, ExecutionRecord
from trading_system.reservations.models import Reservation, reservation_identifier

#: The suites' one instant. Inside the regular NYSE session on a trading day,
#: which several validity checks rely on.
NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
EXPIRATION = date(2026, 9, 18)
ACCOUNT = "DU1234567"
#: What every artifact stores instead of the account number above.
MASKED = "*****4567"

CALL_CONTRACT_ID = 100001
PUT_CONTRACT_ID = 100002
CALL_KEY = f"cid:{CALL_CONTRACT_ID}"
PUT_KEY = f"cid:{PUT_CONTRACT_ID}"


def versions() -> SystemVersions:
    return SystemVersions(application_version="0.1.0", config_version="test")


# ---------------------------------------------------------------------------
# Broker reality
# ---------------------------------------------------------------------------
def broker_account(
    *,
    account_id: str = ACCOUNT,
    currency: str = "EUR",
    as_of: datetime = NOW,
    cash: Decimal | None = Decimal("100000.00"),
) -> BrokerAccount:
    return BrokerAccount(
        account_id=account_id,
        currency=currency,
        as_of=as_of,
        source="SIMULATOR",
        cash=cash,
        net_liquidation=Decimal("100000.00"),
        buying_power=Decimal("400000.00"),
        available_funds=Decimal("98000.00"),
    )


def option_position(
    *,
    contract_id: int | None = CALL_CONTRACT_ID,
    quantity: Decimal = Decimal("2"),
    strike: Decimal = Decimal("180.00"),
    right: OptionRight = OptionRight.CALL,
    expiration: date = EXPIRATION,
    symbol: str = "NVDA",
    average_cost: Decimal | None = Decimal("595.00"),
    market_value: Decimal | None = Decimal("1250.00"),
    unrealized_pnl: Decimal | None = Decimal("60.00"),
    as_of: datetime = NOW,
    account_id: str = ACCOUNT,
    currency: str | None = "EUR",
    multiplier: int | None = 100,
) -> BrokerPosition:
    return BrokerPosition(
        account_id=account_id,
        symbol=symbol,
        security_type=SecurityType.OPTION,
        as_of=as_of,
        source="SIMULATOR",
        contract_id=contract_id,
        local_symbol=f"{symbol}  260918C00180000",
        currency=currency,
        multiplier=multiplier,
        quantity=quantity,
        average_cost=average_cost,
        expiration=expiration,
        strike=strike,
        right=right,
        market_price=Decimal("6.25") if market_value is not None else None,
        market_value=market_value,
        unrealized_pnl=unrealized_pnl,
        realized_pnl=None,
    )


def stock_position(
    *,
    symbol: str = "SPY",
    quantity: Decimal = Decimal("10"),
    contract_id: int = 900001,
    as_of: datetime = NOW,
) -> BrokerPosition:
    return BrokerPosition(
        account_id=ACCOUNT,
        symbol=symbol,
        security_type=SecurityType.STOCK,
        as_of=as_of,
        source="SIMULATOR",
        contract_id=contract_id,
        currency="USD",
        multiplier=1,
        quantity=quantity,
        average_cost=Decimal("500.00"),
        market_price=Decimal("505.00"),
        market_value=Decimal("5050.00"),
        unrealized_pnl=Decimal("50.00"),
    )


def broker_order(
    *,
    broker_order_id: str = "ord-1",
    status: OrderStatus = OrderStatus.SUBMITTED,
    filled: Decimal = Decimal("0"),
    quantity: Decimal = Decimal("2"),
    symbol: str = "NVDA",
    contract_id: int = CALL_CONTRACT_ID,
    as_of: datetime = NOW,
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker_order_id,
        account_id=ACCOUNT,
        as_of=as_of,
        source="SIMULATOR",
        contract_id=contract_id,
        symbol=symbol,
        security_type=SecurityType.OPTION,
        side=OrderSide.BUY,
        quantity=quantity,
        order_type="LMT",
        limit_price=Decimal("5.95"),
        time_in_force="DAY",
        status=status,
        filled_quantity=filled,
        remaining_quantity=quantity - filled,
        average_fill_price=Decimal("5.95") if filled else None,
        submitted_at=as_of,
        updated_at=as_of,
    )


def broker_execution(
    *,
    execution_id: str = "exec-1",
    broker_order_id: str | None = "ord-1",
    contract_id: int | None = CALL_CONTRACT_ID,
    symbol: str = "NVDA",
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = Decimal("2"),
    price: Decimal = Decimal("5.95"),
    commission: Decimal | None = Decimal("1.30"),
    executed_at: datetime = NOW,
    security_type: SecurityType = SecurityType.OPTION,
    currency: str | None = "EUR",
) -> BrokerExecution:
    return BrokerExecution(
        execution_id=execution_id,
        broker_order_id=broker_order_id,
        account_id=ACCOUNT,
        as_of=executed_at,
        source="SIMULATOR",
        contract_id=contract_id,
        symbol=symbol,
        security_type=security_type,
        side=side,
        quantity=quantity,
        price=price,
        executed_at=executed_at,
        commission=commission,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# What this system believes
# ---------------------------------------------------------------------------
def execution_leg(
    *,
    leg_index: int = 0,
    contract_id: int = CALL_CONTRACT_ID,
    action: LegAction = LegAction.BUY,
    right: OptionRight = OptionRight.CALL,
    strike: Decimal = Decimal("180.00"),
    underlying: str = "NVDA",
    expiration: date = EXPIRATION,
    multiplier: int = 100,
    ratio: int = 1,
    currency: str | None = "EUR",
) -> ExecutionLeg:
    return ExecutionLeg(
        leg_index=leg_index,
        contract_id=contract_id,
        action=action,
        right=right,
        underlying=underlying,
        expiration=expiration,
        strike=strike,
        multiplier=multiplier,
        ratio=ratio,
        trading_class=underlying,
        exchange="SMART",
        currency=currency,
    )


def straddle_legs() -> list[ExecutionLeg]:
    """Two legs on one strike: the structure that makes PARTIAL meaningful."""
    return [
        execution_leg(leg_index=0, contract_id=CALL_CONTRACT_ID, right=OptionRight.CALL),
        execution_leg(leg_index=1, contract_id=PUT_CONTRACT_ID, right=OptionRight.PUT),
    ]


def execution_record(
    *,
    execution_id: str = "execution-1",
    state: ExecutionState = ExecutionState.FILLED,
    quantity: int = 2,
    filled_quantity: int | None = None,
    legs: list[ExecutionLeg] | None = None,
    allocation_id: str = "allocation-1",
    opportunity_id: str = "opportunity-1",
    campaign_id: str = "campaign-001",
    broker_order_id: str | None = "ord-1",
    average_fill_price: Decimal | None = Decimal("5.95"),
    multiplier: int = 100,
    underlying: str = "NVDA",
    strategy: StrategyType = StrategyType.LONG_CALL,
    currency: str | None = "EUR",
    capital_commitment: Decimal = Decimal("1190.00"),
    created_at: datetime = NOW,
    reason_codes: list[ExecutionReasonCode] | None = None,
) -> ExecutionRecord:
    """One execution attempt, in whatever state a test needs.

    ``filled_quantity`` defaults to the whole quantity for ``FILLED`` and to
    zero otherwise, which is the invariant the Milestone 8 model enforces.
    """
    resolved_legs = legs if legs is not None else [execution_leg()]
    if filled_quantity is None:
        filled_quantity = quantity if state is ExecutionState.FILLED else 0
    price = average_fill_price if filled_quantity else None
    codes = reason_codes
    if codes is None and state in (ExecutionState.REJECTED, ExecutionState.FAILED):
        codes = [ExecutionReasonCode.BROKER_REJECTED]
    return ExecutionRecord(
        execution_id=execution_id,
        execution_request_id=f"exec-req-{execution_id}",
        allocation_id=allocation_id,
        purchase_card_id="card-1",
        risk_decision_id="risk-1",
        order_intent_id="intent-1",
        campaign_id=campaign_id,
        opportunity_id=opportunity_id,
        created_at=created_at,
        updated_at=created_at,
        underlying=underlying,
        strategy=strategy,
        legs=resolved_legs,
        quantity=quantity,
        multiplier=multiplier,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        reference_price=Decimal("595.00"),
        reference_quote=Decimal("5.95"),
        submitted_price=Decimal("5.95"),
        capital_commitment=capital_commitment,
        maximum_loss=capital_commitment,
        currency=currency,
        trading_mode=TradingMode.PAPER,
        dry_run=False,
        broker="SIMULATOR",
        state=state,
        broker_order_id=broker_order_id if state is not ExecutionState.FAILED else None,
        broker_status=None,
        filled_quantity=filled_quantity,
        average_fill_price=price,
        orders_submitted=1 if state is not ExecutionState.FAILED else 0,
        reason_codes=codes or [],
        policy_version="test",
        versions=versions(),
    )


def reservation(
    *,
    allocation_id: str = "allocation-1",
    opportunity_id: str = "opportunity-1",
    campaign_id: str = "campaign-001",
    authorized: Decimal = Decimal("1190.00"),
    quantity: int = 2,
    currency: str = "EUR",
    state: ReservationState = ReservationState.RESERVED,
    created_at: datetime = NOW,
    symbol: str = "NVDA",
) -> Reservation:
    """A freshly created reservation: committed, nothing consumed."""
    return Reservation(
        reservation_id=reservation_identifier(
            campaign_id=campaign_id,
            allocation_id=allocation_id,
            opportunity_id=opportunity_id,
        ),
        campaign_id=campaign_id,
        allocation_id=allocation_id,
        opportunity_id=opportunity_id,
        symbol=symbol,
        strategy=StrategyType.LONG_CALL,
        currency=currency,
        authorized_amount=authorized,
        authorized_max_loss=authorized,
        authorized_quantity=quantity,
        authorized_at=created_at,
        remaining_amount=authorized,
        state=state,
        reason_codes=[ReservationReasonCode.AUTHORIZED],
        created_at=created_at,
        updated_at=created_at,
    )
