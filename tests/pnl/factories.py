"""Deterministic builders for the Milestone 11 profit-and-loss suites.

Four rules, the same four the Milestone 10 factories hold to:

* **Nothing here reaches a broker, a network or a model.** Every object is
  constructed in process.
* **Everything is exact.** Fixed instants, fixed ids, decimal money. A test
  that passes today passes tomorrow.
* **Fills are built the way the system records them.** An
  :class:`~trading_system.positions.models.ObservedFill` with a broker
  execution id, a multiplier and a currency — because a fill missing any of
  those is precisely the case the calculator is supposed to refuse, and a
  factory that quietly supplied them would hide the refusal.
* **Money is quoted terms times multiplier.** A ``6.05`` fill of two contracts
  at a multiplier of 100 is ``1,210.00`` of money, and the factories never
  conflate the two.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from trading_system.domain.enums import (
    ExecutionIntent,
    ExecutionState,
    LegAction,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    SecurityType,
    StrategyType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import SystemVersions
from trading_system.execution.models import ExecutionLeg, ExecutionRecord
from trading_system.positions.models import ObservedFill
from trading_system.reservations.models import Reservation

#: The suites' one instant: a Monday inside the NYSE session, in a year the
#: shipped market calendar has actually verified.
NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
#: The exit, four hours later — the same New York session, deliberately, so a
#: day-boundary test has to move the clock rather than rely on the default.
EXIT_AT = datetime(2026, 8, 10, 18, 30, tzinfo=UTC)
EXPIRATION = date(2026, 9, 18)

ACCOUNT = "DU1234567"
MASKED = "*****4567"
CAMPAIGN = "campaign-001"
POSITION = "position-nvda-1"
ENTRY_EXECUTION = "execution-entry-1"
EXIT_EXECUTION = "execution-exit-1"
ALLOCATION = "allocation-1"
OPPORTUNITY = "opportunity-1"

CALL_CONTRACT_ID = 100001
PUT_CONTRACT_ID = 100002
CALL_KEY = f"cid:{CALL_CONTRACT_ID}"
PUT_KEY = f"cid:{PUT_CONTRACT_ID}"

MULTIPLIER = 100
#: Bought at 6.05, sold at 8.05: two contracts make 400.00 gross.
ENTRY_QUOTE = Decimal("6.05")
EXIT_QUOTE = Decimal("8.05")
QUANTITY = 2


def versions() -> SystemVersions:
    return SystemVersions(application_version="0.1.0", config_version="test")


# ---------------------------------------------------------------------------
# Confirmed broker fills — the only thing a realised result rests on
# ---------------------------------------------------------------------------
def fill(
    *,
    fill_id: str = "fill-1",
    key: str = CALL_KEY,
    contract_id: int | None = CALL_CONTRACT_ID,
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal | int = QUANTITY,
    price: Decimal = ENTRY_QUOTE,
    commission: Decimal | None = Decimal("1.50"),
    multiplier: int | None = MULTIPLIER,
    currency: str | None = "EUR",
    executed_at: datetime = NOW,
    execution_id: str | None = ENTRY_EXECUTION,
    right: OptionRight | None = OptionRight.CALL,
    strike: Decimal | None = Decimal("180.00"),
    expiration: date | None = EXPIRATION,
    underlying: str = "NVDA",
) -> ObservedFill:
    """One confirmed execution report, exactly as Milestone 9 records it."""
    return ObservedFill(
        fill_id=fill_id,
        account_reference=MASKED,
        key=key,
        broker_execution_id=f"broker-{fill_id}",
        broker_order_id="order-1",
        underlying=underlying,
        symbol=f"{underlying}  260918C00180000",
        asset_class=SecurityType.OPTION,
        contract_id=contract_id,
        expiration=expiration,
        strike=strike,
        right=right,
        multiplier=multiplier,
        side=side,
        quantity=Decimal(quantity),
        price=price,
        commission=commission,
        currency=currency,
        executed_at=executed_at,
        observed_at=executed_at,
        broker_source="SIMULATOR",
        execution_id=execution_id,
    )


def entry_fills(
    *,
    price: Decimal = ENTRY_QUOTE,
    quantity: int = QUANTITY,
    commission: Decimal | None = Decimal("1.50"),
    multiplier: int | None = MULTIPLIER,
    currency: str | None = "EUR",
) -> list[ObservedFill]:
    """One BUY that opened the position."""
    return [
        fill(
            fill_id="fill-entry-1",
            side=OrderSide.BUY,
            price=price,
            quantity=Decimal(quantity),
            commission=commission,
            multiplier=multiplier,
            currency=currency,
            executed_at=NOW,
            execution_id=ENTRY_EXECUTION,
        )
    ]


def exit_fills(
    *,
    price: Decimal = EXIT_QUOTE,
    quantity: int = QUANTITY,
    commission: Decimal | None = Decimal("1.50"),
    multiplier: int | None = MULTIPLIER,
    currency: str | None = "EUR",
    executed_at: datetime = EXIT_AT,
) -> list[ObservedFill]:
    """One SELL that closed it."""
    return [
        fill(
            fill_id="fill-exit-1",
            side=OrderSide.SELL,
            price=price,
            quantity=Decimal(quantity),
            commission=commission,
            multiplier=multiplier,
            currency=currency,
            executed_at=executed_at,
            execution_id=EXIT_EXECUTION,
        )
    ]


def straddle_fills(
    *,
    call_entry: Decimal = Decimal("6.00"),
    put_entry: Decimal = Decimal("5.00"),
    call_exit: Decimal = Decimal("9.00"),
    put_exit: Decimal = Decimal("2.00"),
    quantity: int = 1,
) -> tuple[list[ObservedFill], list[ObservedFill]]:
    """A straddle, opened and closed. Two legs, **one** trade.

    The default numbers are deliberately a winner paid for by a loser: the call
    makes 300 and the put loses 300, so a test that reported per-leg results
    would show two trades netting zero while the structure's own result is
    exactly zero for a different and more interesting reason.
    """
    entry = [
        fill(
            fill_id="fill-entry-call",
            key=CALL_KEY,
            contract_id=CALL_CONTRACT_ID,
            right=OptionRight.CALL,
            side=OrderSide.BUY,
            price=call_entry,
            quantity=Decimal(quantity),
            executed_at=NOW,
            execution_id=ENTRY_EXECUTION,
        ),
        fill(
            fill_id="fill-entry-put",
            key=PUT_KEY,
            contract_id=PUT_CONTRACT_ID,
            right=OptionRight.PUT,
            side=OrderSide.BUY,
            price=put_entry,
            quantity=Decimal(quantity),
            executed_at=NOW,
            execution_id=ENTRY_EXECUTION,
        ),
    ]
    closing = [
        fill(
            fill_id="fill-exit-call",
            key=CALL_KEY,
            contract_id=CALL_CONTRACT_ID,
            right=OptionRight.CALL,
            side=OrderSide.SELL,
            price=call_exit,
            quantity=Decimal(quantity),
            executed_at=EXIT_AT,
            execution_id=EXIT_EXECUTION,
        ),
        fill(
            fill_id="fill-exit-put",
            key=PUT_KEY,
            contract_id=PUT_CONTRACT_ID,
            right=OptionRight.PUT,
            side=OrderSide.SELL,
            price=put_exit,
            quantity=Decimal(quantity),
            executed_at=EXIT_AT,
            execution_id=EXIT_EXECUTION,
        ),
    ]
    return entry, closing


# ---------------------------------------------------------------------------
# The executions behind them
# ---------------------------------------------------------------------------
def execution_leg(
    *,
    leg_index: int = 0,
    contract_id: int = CALL_CONTRACT_ID,
    right: OptionRight = OptionRight.CALL,
    action: LegAction = LegAction.BUY,
    strike: Decimal = Decimal("180.00"),
) -> ExecutionLeg:
    return ExecutionLeg(
        leg_index=leg_index,
        contract_id=contract_id,
        action=action,
        right=right,
        underlying="NVDA",
        expiration=EXPIRATION,
        strike=strike,
        multiplier=MULTIPLIER,
        trading_class="NVDA",
        exchange="SMART",
        currency="EUR",
    )


def entry_execution(
    *,
    execution_id: str = ENTRY_EXECUTION,
    quantity: int = QUANTITY,
    state: ExecutionState = ExecutionState.FILLED,
    legs: list[ExecutionLeg] | None = None,
    strategy: StrategyType = StrategyType.LONG_CALL,
) -> ExecutionRecord:
    """The Milestone 8 record the position descends from. ``intent=OPEN``."""
    return ExecutionRecord(
        execution_id=execution_id,
        execution_request_id="exec-req-entry-1",
        allocation_id=ALLOCATION,
        purchase_card_id="card-1",
        risk_decision_id="risk-1",
        order_intent_id="intent-1",
        campaign_id=CAMPAIGN,
        opportunity_id=OPPORTUNITY,
        created_at=NOW,
        updated_at=NOW,
        underlying="NVDA",
        strategy=strategy,
        intent=ExecutionIntent.OPEN,
        legs=legs or [execution_leg()],
        quantity=quantity,
        multiplier=MULTIPLIER,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        reference_price=Decimal("605.00"),
        reference_quote=ENTRY_QUOTE,
        submitted_price=ENTRY_QUOTE,
        capital_commitment=Decimal("1210.00"),
        maximum_loss=Decimal("1210.00"),
        currency="EUR",
        trading_mode=TradingMode.PAPER,
        broker="SIMULATOR",
        state=state,
        broker_order_id="order-1",
        broker_status=OrderStatus.FILLED,
        filled_quantity=quantity if state is ExecutionState.FILLED else 0,
        average_fill_price=ENTRY_QUOTE,
        orders_submitted=1,
        research_report_id="research-report-1",
        contract_selection_id="contract-1",
        policy_version="1.0.0",
        versions=versions(),
    )


def exit_execution(
    *,
    execution_id: str = EXIT_EXECUTION,
    quantity: int = QUANTITY,
    state: ExecutionState = ExecutionState.FILLED,
    position_id: str = POSITION,
    legs: list[ExecutionLeg] | None = None,
) -> ExecutionRecord:
    """The Milestone 8 record that closed it. ``intent=CLOSE``.

    Its legs are stored **as sent** — inverted — exactly as Milestone 10
    requires: the position ledger reads each leg's action to decide whether a
    fill adds or subtracts, and a CLOSE carrying the entry's BUY legs would net
    an exit onto the position as though it had bought more.
    """
    return ExecutionRecord(
        execution_id=execution_id,
        execution_request_id="exit-req-1",
        allocation_id=ALLOCATION,
        purchase_card_id="card-1",
        risk_decision_id="risk-1",
        order_intent_id="intent-exit-1",
        campaign_id=CAMPAIGN,
        opportunity_id=OPPORTUNITY,
        created_at=EXIT_AT,
        updated_at=EXIT_AT,
        underlying="NVDA",
        strategy=StrategyType.LONG_CALL,
        intent=ExecutionIntent.CLOSE,
        legs=legs or [execution_leg(action=LegAction.SELL)],
        quantity=quantity,
        multiplier=MULTIPLIER,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        submitted_price=EXIT_QUOTE,
        capital_commitment=Decimal("0"),
        maximum_loss=Decimal("0"),
        currency="EUR",
        trading_mode=TradingMode.PAPER,
        broker="SIMULATOR",
        state=state,
        broker_order_id="order-exit-1",
        broker_status=OrderStatus.FILLED,
        filled_quantity=quantity if state is ExecutionState.FILLED else 0,
        average_fill_price=EXIT_QUOTE,
        orders_submitted=1,
        position_id=position_id,
        exit_decision_id="exit-decision-1",
        entry_execution_id=ENTRY_EXECUTION,
        policy_version="1.0.0",
        versions=versions(),
    )


# ---------------------------------------------------------------------------
# The capital behind it
# ---------------------------------------------------------------------------
def reservation(
    *,
    reservation_id: str = "reservation-1",
    authorized: Decimal = Decimal("1210.00"),
    consumed: Decimal = Decimal("1210.00"),
    quantity: int = QUANTITY,
    state: str = "CONSUMED",
) -> Reservation:
    """A reservation whose capital is sitting in the position, ready to settle."""
    from trading_system.domain.enums import ReservationReasonCode, ReservationState

    resolved = ReservationState(state)
    reasons = [ReservationReasonCode.AUTHORIZED]
    if resolved is ReservationState.CONSUMED:
        reasons.append(ReservationReasonCode.FILLED)
    if resolved is ReservationState.UNKNOWN:
        reasons.append(ReservationReasonCode.EXECUTION_UNKNOWN)

    remaining = authorized - consumed
    return Reservation(
        reservation_id=reservation_id,
        campaign_id=CAMPAIGN,
        allocation_id=ALLOCATION,
        opportunity_id=OPPORTUNITY,
        symbol="NVDA",
        strategy=StrategyType.LONG_CALL,
        currency="EUR",
        authorized_amount=authorized,
        authorized_max_loss=authorized,
        authorized_quantity=quantity,
        authorized_at=NOW,
        consumed_amount=consumed,
        consumed_quantity=quantity,
        remaining_amount=remaining,
        consumed_from_actual_fills=True,
        state=resolved,
        reason_codes=reasons,
        created_at=NOW,
        updated_at=NOW,
        execution_id=ENTRY_EXECUTION,
    )
