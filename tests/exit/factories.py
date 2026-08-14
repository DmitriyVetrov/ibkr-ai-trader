"""Deterministic builders for the Milestone 10 suites.

Four rules hold throughout:

* **Nothing here reaches a broker, a network or a model.** Every object is
  constructed in process, and the one broker any of these tests sees is the
  simulator, supplied explicitly.
* **Everything is exact.** Fixed instants, fixed ids, decimal money. A test
  that passes today passes tomorrow.
* **Positions are built the way the system builds them.** An entry execution
  with confirmed fills plus a broker snapshot, run through Milestone 9's own
  projection — not a hand-assembled ``OpenPosition``. A test against an object
  the system never constructs has not tested the system.
* **Quotes go through the real repository.** Point-in-time visibility, hashing
  and the ledger are all exercised, because an exit that only works against a
  hand-built quote has not been tested against the thing it will see.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trading_system.data.hashing import stable_hash
from trading_system.data.models import (
    DataQualityReport,
    DataRecord,
    DataSourceMetadata,
    OptionContract,
    OptionQuote,
)
from trading_system.data.repository import FilesystemDataRepository, build_snapshot
from trading_system.domain.enums import (
    DataType,
    ExecutionIntent,
    ExecutionState,
    ExitQuoteField,
    LegAction,
    MarketDataOrigin,
    OptionRight,
    OrderStatus,
    OrderType,
    SourceTier,
    StrategyType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import SystemVersions
from trading_system.execution.models import ExecutionLeg, ExecutionRecord
from trading_system.exit.models import (
    ExitPolicySnapshot,
    PositionValuation,
    TrailingStopRecord,
    trailing_state_identifier,
)
from trading_system.exit.valuation import HeldLeg

#: The suites' one instant: a Monday inside the NYSE session, in a year the
#: shipped market calendar has actually verified.
NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

#: Comfortably outside the force-exit window at NOW (39 calendar days).
EXPIRATION = date(2026, 9, 18)
#: Inside the force-exit window (3 calendar days).
NEAR_EXPIRATION = date(2026, 8, 13)
#: Inside the warning window but not the force-exit one (8 calendar days).
WARNING_EXPIRATION = date(2026, 8, 18)
#: A year the shipped calendar does not cover.
UNCOVERED_EXPIRATION = date(2030, 1, 18)

ACCOUNT = "DU1234567"
MASKED = "*****4567"

CALL_CONTRACT_ID = 100001
PUT_CONTRACT_ID = 100002
CALL_KEY = f"cid:{CALL_CONTRACT_ID}"
PUT_KEY = f"cid:{PUT_CONTRACT_ID}"

ENTRY_QUOTE = Decimal("6.00")
MULTIPLIER = 100


def versions() -> SystemVersions:
    return SystemVersions(application_version="0.1.0", config_version="test")


# ---------------------------------------------------------------------------
# The entry execution: what established the position
# ---------------------------------------------------------------------------
def execution_leg(
    *,
    leg_index: int = 0,
    contract_id: int = CALL_CONTRACT_ID,
    right: OptionRight = OptionRight.CALL,
    strike: Decimal = Decimal("180.00"),
    expiration: date = EXPIRATION,
    action: LegAction = LegAction.BUY,
    underlying: str = "NVDA",
    multiplier: int = MULTIPLIER,
    ratio: int = 1,
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
        currency="EUR",
    )


def straddle_legs(*, expiration: date = EXPIRATION) -> list[ExecutionLeg]:
    """One call and one put on the same strike and expiration."""
    return [
        execution_leg(leg_index=0, expiration=expiration),
        execution_leg(
            leg_index=1,
            contract_id=PUT_CONTRACT_ID,
            right=OptionRight.PUT,
            expiration=expiration,
        ),
    ]


def entry_execution(
    *,
    execution_id: str = "execution-entry-1",
    legs: Sequence[ExecutionLeg] | None = None,
    quantity: int = 2,
    filled: int | None = None,
    average_fill_price: Decimal | None = ENTRY_QUOTE,
    state: ExecutionState = ExecutionState.FILLED,
    strategy: StrategyType = StrategyType.LONG_CALL,
    created_at: datetime = NOW,
    research_report_id: str | None = "research-report-1",
    currency: str | None = "EUR",
) -> ExecutionRecord:
    """The Milestone 8 record a position descends from. ``intent=OPEN``."""
    chosen = list(legs) if legs is not None else [execution_leg()]
    filled_quantity = quantity if filled is None else filled
    return ExecutionRecord(
        execution_id=execution_id,
        execution_request_id="exec-req-entry-1",
        allocation_id="allocation-1",
        purchase_card_id="card-1",
        risk_decision_id="risk-1",
        order_intent_id="intent-1",
        campaign_id="campaign-001",
        opportunity_id="opportunity-1",
        created_at=created_at,
        updated_at=created_at,
        underlying="NVDA",
        strategy=strategy,
        intent=ExecutionIntent.OPEN,
        legs=chosen,
        quantity=quantity,
        multiplier=MULTIPLIER,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        reference_price=ENTRY_QUOTE * Decimal(MULTIPLIER),
        reference_quote=ENTRY_QUOTE,
        submitted_price=ENTRY_QUOTE,
        capital_commitment=ENTRY_QUOTE * Decimal(MULTIPLIER) * Decimal(quantity),
        maximum_loss=ENTRY_QUOTE * Decimal(MULTIPLIER) * Decimal(quantity),
        currency=currency,
        trading_mode=TradingMode.PAPER,
        broker="SIMULATOR",
        state=state,
        broker_order_id="sim-000001",
        broker_status=OrderStatus.FILLED,
        filled_quantity=filled_quantity,
        average_fill_price=average_fill_price,
        filled_at=created_at,
        orders_submitted=1,
        allocation_run_id="allocation-run-1",
        contract_selection_id="contract-1",
        strategy_decision_id="strategy-1",
        research_report_id=research_report_id,
        policy_version="test",
        versions=versions(),
    )


# ---------------------------------------------------------------------------
# Broker reality
# ---------------------------------------------------------------------------
def broker_position(
    *,
    contract_id: int = CALL_CONTRACT_ID,
    quantity: Decimal = Decimal("2"),
    right: OptionRight = OptionRight.CALL,
    strike: Decimal = Decimal("180.00"),
    expiration: date = EXPIRATION,
    symbol: str = "NVDA",
    as_of: datetime = NOW,
):
    from trading_system.domain.enums import SecurityType
    from trading_system.domain.models import BrokerPosition

    return BrokerPosition(
        account_id=ACCOUNT,
        symbol=symbol,
        security_type=SecurityType.OPTION,
        as_of=as_of,
        source="SIMULATOR",
        contract_id=contract_id,
        currency="EUR",
        multiplier=MULTIPLIER,
        quantity=quantity,
        average_cost=ENTRY_QUOTE * Decimal(MULTIPLIER),
        expiration=expiration,
        strike=strike,
        right=right,
        market_price=Decimal("6.50"),
        market_value=Decimal("1300.00"),
        unrealized_pnl=Decimal("100.00"),
    )


def position_snapshot(
    positions: Sequence[object] | None = None,
    *,
    as_of: datetime = NOW,
    usable: bool = True,
):
    """A stored broker snapshot, built the way the position service builds one."""
    from trading_system.domain.enums import BrokerReadStatus
    from trading_system.positions.snapshot import build_position_snapshot, unavailable_snapshot

    if not usable:
        return unavailable_snapshot(
            broker="SIMULATOR",
            account_id=ACCOUNT,
            trading_mode=TradingMode.PAPER,
            as_of=as_of,
            observed_at=as_of,
            status=BrokerReadStatus.UNAVAILABLE,
            detail="the simulator refused; this is not an empty account",
        )
    chosen = list(positions) if positions is not None else [broker_position()]
    return build_position_snapshot(
        chosen,  # type: ignore[arg-type]
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=as_of,
        observed_at=as_of,
    )


# ---------------------------------------------------------------------------
# Quotes, through the real repository
# ---------------------------------------------------------------------------
def _metadata(*, retrieved_at: datetime, as_of: datetime, identifier: str) -> DataSourceMetadata:
    return DataSourceMetadata(
        provider="IBKR",
        source_name="IBKR",
        source_tier=SourceTier.TIER_1,
        origin=MarketDataOrigin.BROKER_DELAYED,
        retrieved_at=retrieved_at,
        source_timestamp=as_of,
        observed_at=as_of,
        source_identifier=identifier,
    )


def option_quote(
    *,
    contract_id: int = CALL_CONTRACT_ID,
    right: OptionRight = OptionRight.CALL,
    strike: Decimal = Decimal("180.00"),
    expiration: date = EXPIRATION,
    bid: Decimal | None = Decimal("6.50"),
    ask: Decimal | None = Decimal("6.70"),
    last: Decimal | None = Decimal("6.60"),
    as_of: datetime = NOW,
    retrieved_at: datetime | None = None,
    research_usable: bool = True,
    symbol: str = "NVDA",
) -> OptionQuote:
    retrieved = retrieved_at or as_of
    return OptionQuote(
        as_of=as_of,
        source=_metadata(
            retrieved_at=retrieved, as_of=as_of, identifier=f"ibkr:{symbol}:{contract_id}"
        ),
        contract=OptionContract(
            underlying=symbol,
            symbol=symbol,
            expiration=expiration,
            strike=strike,
            right=right,
            contract_id=contract_id,
            exchange="SMART",
            currency="EUR",
            multiplier=MULTIPLIER,
            trading_class=symbol,
        ),
        bid=bid,
        ask=ask,
        last=last,
        quality=DataQualityReport(evaluated_at=retrieved, research_usable=research_usable),
    )


def store_quotes(
    repository: FilesystemDataRepository,
    quotes: Sequence[DataRecord],
    *,
    symbol: str = "NVDA",
    as_of: datetime = NOW,
    retrieved_at: datetime | None = None,
) -> str:
    retrieved = retrieved_at or as_of
    snapshot = build_snapshot(
        data_type=DataType.OPTION_QUOTE,
        key=symbol.upper(),
        records=list(quotes),
        provider="IBKR",
        source_tier=SourceTier.TIER_1,
        origin=MarketDataOrigin.BROKER_DELAYED,
        as_of=as_of,
        retrieved_at=retrieved,
        quality=DataQualityReport(evaluated_at=retrieved),
    )
    repository.save_snapshot(snapshot)
    return snapshot.snapshot_id


# ---------------------------------------------------------------------------
# Pieces of an evaluation, for the pure-function suites
# ---------------------------------------------------------------------------
def held_leg(
    *,
    leg_index: int = 0,
    contract_id: int = CALL_CONTRACT_ID,
    key: str = CALL_KEY,
    right: OptionRight = OptionRight.CALL,
    strike: Decimal = Decimal("180.00"),
    expiration: date = EXPIRATION,
    ratio: int = 1,
    observed_quantity: Decimal | None = Decimal("2"),
) -> HeldLeg:
    return HeldLeg(
        leg_index=leg_index,
        key=key,
        contract_id=contract_id,
        underlying="NVDA",
        right=right,
        strike=strike,
        expiration=expiration,
        ratio=ratio,
        multiplier=MULTIPLIER,
        observed_quantity=observed_quantity,
    )


def policy_snapshot(
    *,
    strategy: StrategyType = StrategyType.LONG_CALL,
    force_exit_dte: int = 7,
    warning_dte: int = 10,
    trailing_distance_pct: float = 30.0,
    activation_return_pct: float = 25.0,
    take_profit_return_pct: float | None = 100.0,
    max_loss_pct: float = 50.0,
    quote_field: ExitQuoteField = ExitQuoteField.BID,
    max_quote_age_seconds: int = 900,
) -> ExitPolicySnapshot:
    return ExitPolicySnapshot(
        policy_version="1.0.0",
        strategy=strategy,
        expiration_warning_dte=max(warning_dte, force_exit_dte),
        expiration_force_exit_dte=force_exit_dte,
        trailing_activation_return_pct=activation_return_pct,
        trailing_distance_pct=trailing_distance_pct,
        trailing_min_improvement_pct=1.0,
        take_profit_return_pct=take_profit_return_pct,
        max_loss_pct=max_loss_pct,
        quote_field=quote_field,
        max_quote_age_seconds=max_quote_age_seconds,
    )


def valuation(
    *,
    exit_quote: Decimal | None = Decimal("6.50"),
    entry_quote: Decimal | None = ENTRY_QUOTE,
    multiplier: int | None = MULTIPLIER,
    open_quantity: int = 2,
    quote_field: ExitQuoteField = ExitQuoteField.BID,
    legs: Sequence[object] | None = None,
    quote_age_seconds: float | None = 30.0,
    as_of: datetime = NOW,
) -> PositionValuation:
    """A priced structure. Unpriced when ``exit_quote`` is ``None``."""
    from trading_system.exit.models import ExitLegValuation

    built = (
        list(legs)
        if legs is not None
        else [
            ExitLegValuation(
                leg_index=0,
                contract_id=CALL_CONTRACT_ID,
                key=CALL_KEY,
                right=OptionRight.CALL,
                strike=Decimal("180.00"),
                expiration=EXPIRATION,
                multiplier=multiplier,
                observed_quantity=Decimal(open_quantity),
                quote_field=quote_field,
                price=exit_quote,
                bid=exit_quote,
                ask=(exit_quote + Decimal("0.20")) if exit_quote is not None else None,
                quote_age_seconds=quote_age_seconds,
            )
        ]
    )
    unpriced = [leg.leg_index for leg in built if leg.price is None]  # type: ignore[attr-defined]
    return PositionValuation(
        as_of=as_of,
        quote_field=quote_field,
        multiplier=multiplier,
        open_quantity=open_quantity,
        currency="EUR",
        legs=built,
        entry_quote=entry_quote,
        entry_cost=(
            entry_quote * Decimal(multiplier)
            if entry_quote is not None and multiplier is not None
            else None
        ),
        exit_quote=None if unpriced else exit_quote,
        exit_value=(
            exit_quote * Decimal(multiplier)
            if exit_quote is not None and multiplier is not None and not unpriced
            else None
        ),
        max_quote_age_seconds=quote_age_seconds,
        unpriced_legs=unpriced,
    )


def trailing_record(
    *,
    position_id: str = "strategypos-1",
    state=None,
    entry_quote: Decimal | None = ENTRY_QUOTE,
    peak_quote: Decimal | None = None,
    stop_quote: Decimal | None = None,
    trigger_quote: Decimal | None = None,
    distance_pct: float = 30.0,
    activation_return_pct: float = 25.0,
    min_improvement_pct: float = 1.0,
    created_at: datetime = NOW,
) -> TrailingStopRecord:
    from trading_system.domain.enums import TrailingStopState

    return TrailingStopRecord(
        trailing_state_id=trailing_state_identifier(position_id=position_id),
        position_id=position_id,
        state=state or TrailingStopState.INACTIVE,
        quote_field=ExitQuoteField.BID,
        activation_return_pct=activation_return_pct,
        distance_pct=distance_pct,
        min_improvement_pct=min_improvement_pct,
        entry_quote=entry_quote,
        peak_quote=peak_quote,
        stop_quote=stop_quote,
        trigger_quote=trigger_quote,
        created_at=created_at,
        updated_at=created_at,
    )


def data_repository(root: Path, clock: object | None = None) -> FilesystemDataRepository:
    return FilesystemDataRepository(root, clock=clock)  # type: ignore[arg-type]


def digest(*parts: str) -> str:
    return stable_hash(list(parts))
