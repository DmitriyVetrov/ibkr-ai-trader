"""Turn an approved purchase card into a broker-agnostic order instruction.

This module is where a *price* becomes an *instruction*, and it is the only
place in the system that does so. It is a pure function of its arguments: no
broker, no repository, no clock, no model, no market data. Given the same card,
the same reference price and the same policy it produces a byte-identical
intent, which is what lets an order be reviewed before it is sent and
reconstructed afterwards.

The single subtlety worth reading carefully is **units**. Milestone 7 records
the cost of one unit of a structure as money — the sum over legs of
``ask x multiplier x ratio``. A broker limit price is not money for a structure;
it is a *quote*, per multiplier unit, and for a combo it is the net of the legs
in those same terms. Sending 595 where 5.95 was meant is a hundredfold
overpayment that every downstream number would faithfully reproduce, so the
conversion happens exactly once, here, against a multiplier the validator has
already proved is shared by every leg.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    ExecutionIntent,
    ExecutionReasonCode,
    LegAction,
    OptionRight,
    OrderType,
    StrategyType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import OptionLeg, OrderIntent, PurchaseCard, SystemVersions
from trading_system.execution.models import EXECUTION_SCHEMA_VERSION
from trading_system.infrastructure.settings import (
    CleanupOrderConfig,
    ExecutionConfig,
    ExitOrderConfig,
)

__all__ = [
    "OrderBuildError",
    "build_cleanup_order_intent",
    "build_exit_order_intent",
    "build_order_intent",
    "cleanup_intent_identifier",
    "exit_intent_identifier",
    "exit_limit_price",
    "limit_price_from_reference",
    "order_intent_identifier",
    "reference_quote_of",
]


class OrderBuildError(RuntimeError):
    """An order could not be built. Carries a machine-readable reason."""

    def __init__(self, reason_code: ExecutionReasonCode, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def order_intent_identifier(
    *,
    purchase_card_id: str,
    execution_request_id: str,
    schema_version: str = EXECUTION_SCHEMA_VERSION,
) -> str:
    """Derive the intent's identity from the card and the request it serves."""
    digest = stable_hash(["ORDER_INTENT", schema_version, purchase_card_id, execution_request_id])
    return f"intent-{digest[:20]}"


def reference_quote_of(reference_price: Decimal, multiplier: int) -> Decimal:
    """Restate a structure's cost in the broker's quoted terms.

    ``reference_price`` is money for one whole unit of the structure and
    already includes the multiplier; a limit price and a fill price do not.
    """
    if multiplier < 1:
        raise OrderBuildError(
            ExecutionReasonCode.MULTIPLIER_MISSING,
            "cannot convert a structure cost into a quote without a contract multiplier",
        )
    return reference_price / Decimal(multiplier)


def limit_price_from_reference(
    reference_price: Decimal,
    *,
    multiplier: int,
    offset_pct: float,
    increment: Decimal,
) -> Decimal:
    """Derive the limit price to send, from the authorisation's reference price.

    Rounds **down** to the configured increment, always. For a debit structure
    that is the direction which spends less, so rounding can only ever bid
    below what was authorised and never a cent above it. The cost is a slightly
    lower chance of filling, which is the correct trade: an unfilled order is
    recoverable and an overspend is not.

    Raises rather than returning a price of zero or less — a non-positive limit
    is not a conservative order, it is a malformed one.
    """
    if reference_price <= 0:
        raise OrderBuildError(
            ExecutionReasonCode.INVALID_PRICE,
            f"reference price {reference_price} is not a price anything can be bought at",
        )
    if increment <= 0:
        raise OrderBuildError(
            ExecutionReasonCode.EXECUTION_CONFIGURATION_ERROR,
            f"execution.price_increment must be positive, got {increment}",
        )

    quote = reference_quote_of(reference_price, multiplier)
    adjusted = quote * (Decimal(1) + Decimal(str(offset_pct)) / Decimal(100))
    rounded = (adjusted / increment).to_integral_value(rounding=ROUND_FLOOR) * increment

    if rounded <= 0:
        raise OrderBuildError(
            ExecutionReasonCode.INVALID_PRICE,
            f"the derived limit price rounds to {rounded} at an increment of {increment}. "
            f"A non-positive limit is malformed, not conservative",
        )
    return rounded


def build_order_intent(
    card: PurchaseCard,
    *,
    execution_request_id: str,
    risk_decision_id: str,
    reference_price: Decimal,
    config: ExecutionConfig,
    order_type: OrderType,
    time_in_force: TimeInForce,
    trading_mode: TradingMode,
    created_at: datetime,
    versions: SystemVersions,
) -> OrderIntent:
    """Build the Milestone 1 ``OrderIntent`` this card authorises.

    The legs, their contract ids and the quantity are copied from the card
    without arithmetic. The only number this function computes is the limit
    price, and it computes it from a figure Milestone 7 stored rather than from
    anything it went and looked up.

    ``action`` comes from each leg's structural ``LegAction`` — from the shape
    of the strategy, never from a rationale, an explanation or any other prose
    (brief section 51). A leg whose action is not expressible is refused.
    """
    if order_type not in config.permitted_order_types:
        raise OrderBuildError(
            ExecutionReasonCode.ORDER_TYPE_NOT_PERMITTED,
            f"{order_type.value} is not permitted; execution.permitted_order_types is "
            f"{[t.value for t in config.permitted_order_types]}",
        )
    if order_type is OrderType.MARKET:
        raise OrderBuildError(
            ExecutionReasonCode.ORDER_TYPE_NOT_PERMITTED,
            "a market order on an option is an unbounded price. Milestone 7 authorised a "
            "specific amount of capital against a specific quoted cost, and a market order "
            "would ignore both",
        )
    if card.quantity < 1:
        raise OrderBuildError(
            ExecutionReasonCode.INVALID_QUANTITY,
            f"purchase card {card.card_id} authorises {card.quantity} contracts",
        )

    legs = list(card.contract.legs)
    if not legs:
        raise OrderBuildError(
            ExecutionReasonCode.CONTRACT_INVALID, f"purchase card {card.card_id} carries no legs"
        )

    multipliers = {leg.multiplier for leg in legs}
    if len(multipliers) != 1:
        raise OrderBuildError(
            ExecutionReasonCode.CONTRACT_INVALID,
            f"the structure's legs carry different multipliers {sorted(multipliers)}; the net "
            f"price of a combo is only defined when they share one",
        )
    multiplier = multipliers.pop()

    for index, leg in enumerate(legs):
        if leg.broker_contract_id is None or leg.broker_contract_id <= 0:
            raise OrderBuildError(
                ExecutionReasonCode.CONTRACT_ID_MISSING,
                f"leg {index} has no broker contract id. Rebuilding one from symbol, strike "
                f"and expiration would place an order for a contract nobody selected",
            )
        if leg.action is LegAction.SELL and not config.allow_short_legs:
            raise OrderBuildError(
                ExecutionReasonCode.SHORT_LEG_NOT_SUPPORTED,
                f"leg {index} is a SELL, and no strategy this system can select is short premium",
            )
    if len(legs) > 1 and not config.multi_leg_as_combo:
        raise OrderBuildError(
            ExecutionReasonCode.MULTI_LEG_UNSUPPORTED,
            f"{card.strategy_type.value} has {len(legs)} legs and combo submission is "
            f"disabled. Independent leg orders could half-fill into a structure nobody "
            f"authorised",
        )

    limit_price = limit_price_from_reference(
        reference_price,
        multiplier=multiplier,
        offset_pct=config.limit_price_offset_pct,
        increment=config.price_increment,
    )

    return OrderIntent(
        intent_id=order_intent_identifier(
            purchase_card_id=card.card_id, execution_request_id=execution_request_id
        ),
        purchase_card_id=card.card_id,
        risk_decision_id=risk_decision_id,
        created_at=created_at,
        underlying=card.underlying,
        strategy_type=card.strategy_type,
        legs=[_copy_leg(leg) for leg in legs],
        quantity=card.quantity,
        order_type=order_type,
        limit_price=limit_price,
        time_in_force=time_in_force,
        # Zero, and deliberately so: the limit price *is* the slippage control.
        # A tolerance here would authorise paying more than the limit says.
        max_slippage_bps=0,
        trading_mode=trading_mode,
        versions=versions,
    )


def _copy_leg(leg: OptionLeg) -> OptionLeg:
    """Copy a leg unchanged.

    Explicit rather than incidental: the legs on the intent must be the legs on
    the card, and a copy that "normalised" anything would be an order for a
    different contract.
    """
    return leg.model_copy()


# ---------------------------------------------------------------------------
# Closing an existing position (Milestone 10)
#
# The same module, deliberately. This is still the only place in the system
# where a price becomes an instruction, it still reaches no broker and reads no
# clock, and it still produces the Milestone 1 ``OrderIntent`` that the
# execution engine already knows how to submit. What is genuinely different
# about a closing order is small and is all here:
#
#   * the legs are INVERTED — a structure we bought is closed by selling it —
#     and the SELL that results is a closing sale rather than short premium;
#   * the offset is applied in the opposite economic direction, because an exit
#     that will not fill is not a safe exit;
#   * there is no purchase card, because nothing is being purchased. The
#     original card and risk decision are referenced by id, so the exit stays
#     traceable to the authorisation that opened the position.
# ---------------------------------------------------------------------------
def exit_intent_identifier(
    *,
    position_id: str,
    exit_request_id: str,
    schema_version: str = EXECUTION_SCHEMA_VERSION,
) -> str:
    """Derive a closing intent's identity from the position and the request."""
    digest = stable_hash(["EXIT_ORDER_INTENT", schema_version, position_id, exit_request_id])
    return f"intent-exit-{digest[:20]}"


def exit_limit_price(
    reference_quote: Decimal,
    *,
    offset_pct: float,
    increment: Decimal,
) -> Decimal:
    """Derive the limit price to offer, from the exit reference quote.

    Two things differ from the entry side and both are about direction.

    The **reference** is already a quote — the bid, or whichever field policy
    named — rather than money for a structure, so no multiplier conversion
    happens here. It happened once already, when the valuation was built.

    The **offset** is expected to be negative: offering slightly below the bid
    is what makes an exit fill, and an exit that does not fill is not a safe
    exit. Rounding is still always **down**, which on this side means the order
    can only ever ask for less than the reference and never more — so a
    rounding error can cost a cent of proceeds and can never leave the position
    open by bidding above the market.

    Raises rather than returning zero or less. A non-positive limit on a sell
    order is not conservative, it is an instruction to give the structure away.
    """
    if reference_quote <= 0:
        raise OrderBuildError(
            ExecutionReasonCode.INVALID_PRICE,
            f"exit reference quote {reference_quote} is not a price anything can be sold at",
        )
    if increment <= 0:
        raise OrderBuildError(
            ExecutionReasonCode.EXECUTION_CONFIGURATION_ERROR,
            f"exit.order.price_increment must be positive, got {increment}",
        )

    adjusted = reference_quote * (Decimal(1) + Decimal(str(offset_pct)) / Decimal(100))
    rounded = (adjusted / increment).to_integral_value(rounding=ROUND_FLOOR) * increment

    if rounded <= 0:
        raise OrderBuildError(
            ExecutionReasonCode.INVALID_PRICE,
            f"the derived exit limit rounds to {rounded} at an increment of {increment}. A "
            f"non-positive limit on a closing order is an instruction to give the position "
            f"away, not a conservative price",
        )
    return rounded


def build_exit_order_intent(
    *,
    position_id: str,
    exit_request_id: str,
    purchase_card_id: str,
    risk_decision_id: str,
    underlying: str,
    strategy_type: StrategyType,
    legs: Sequence[OptionLeg],
    quantity: int,
    reference_quote: Decimal,
    config: ExitOrderConfig,
    execution_config: ExecutionConfig,
    trading_mode: TradingMode,
    created_at: datetime,
    versions: SystemVersions,
) -> OrderIntent:
    """Build the Milestone 1 ``OrderIntent`` that closes an existing position.

    The legs, their contract ids and the quantity are **copied** from what the
    position ledger says is held; the only thing inverted is each leg's action,
    and the only number computed is the limit price.

    Every structural guard the entry side applies is applied here, on the legs
    as they were *bought*:

    * a leg without a broker contract id is refused, because re-deriving one
      would send an order for a contract nobody holds;
    * a structure whose legs carry different multipliers is refused, because a
      combo's net price is only defined when they share one;
    * a multi-leg structure goes as one combo or not at all — closing one leg
      of a straddle leaves a naked long option, which is the same failure
      Milestone 8 refuses on the way in;
    * a position that was *short* a leg is refused as ``SHORT_LEG_NOT_SUPPORTED``,
      because no strategy this system can select is short premium and a
      structure that somehow is one is not something to unwind automatically.
    """
    if config.order_type is not OrderType.LIMIT:
        raise OrderBuildError(
            ExecutionReasonCode.ORDER_TYPE_NOT_PERMITTED,
            f"exit.order.order_type is {config.order_type.value}. A market order to close an "
            f"option is an unbounded price in the one direction that cannot be recovered from",
        )
    if quantity < 1:
        raise OrderBuildError(
            ExecutionReasonCode.INVALID_QUANTITY,
            f"an exit for {quantity} contract(s) is not an exit; the broker holds nothing to sell",
        )
    held = list(legs)
    if not held:
        raise OrderBuildError(
            ExecutionReasonCode.CONTRACT_INVALID,
            f"position {position_id} reports no legs to close",
        )

    multipliers = {leg.multiplier for leg in held}
    if len(multipliers) != 1:
        raise OrderBuildError(
            ExecutionReasonCode.CONTRACT_INVALID,
            f"the structure's legs carry different multipliers {sorted(multipliers)}; the net "
            f"price of a combo is only defined when they share one",
        )

    for index, leg in enumerate(held):
        if leg.broker_contract_id is None or leg.broker_contract_id <= 0:
            raise OrderBuildError(
                ExecutionReasonCode.CONTRACT_ID_MISSING,
                f"leg {index} has no broker contract id. Rebuilding one from symbol, strike "
                f"and expiration would send a closing order for a contract nobody holds",
            )
        if leg.action is LegAction.SELL:
            raise OrderBuildError(
                ExecutionReasonCode.SHORT_LEG_NOT_SUPPORTED,
                f"leg {index} was opened SELL, so closing it would buy back a short option. "
                f"No strategy this system can select is short premium, and a structure that "
                f"somehow is one is not something to unwind automatically",
            )
    if len(held) > 1 and not execution_config.multi_leg_as_combo:
        raise OrderBuildError(
            ExecutionReasonCode.MULTI_LEG_UNSUPPORTED,
            f"{strategy_type.value} has {len(held)} legs and combo submission is disabled. "
            f"Closing the legs independently could leave a naked long option where a complete "
            f"structure was held",
        )

    limit_price = exit_limit_price(
        reference_quote,
        offset_pct=config.limit_price_offset_pct,
        increment=config.price_increment,
    )

    return OrderIntent(
        intent_id=exit_intent_identifier(position_id=position_id, exit_request_id=exit_request_id),
        purchase_card_id=purchase_card_id,
        risk_decision_id=risk_decision_id,
        created_at=created_at,
        underlying=underlying,
        strategy_type=strategy_type,
        legs=[_closing_leg(leg) for leg in held],
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=limit_price,
        time_in_force=config.time_in_force,
        # Zero, exactly as on the entry side: the limit price *is* the slippage
        # control, and a tolerance here would accept less than the limit says.
        max_slippage_bps=0,
        trading_mode=trading_mode,
        versions=versions,
    )


# ---------------------------------------------------------------------------
# Closing a pre-existing broker holding (orphan cleanup)
# ---------------------------------------------------------------------------
#
# Structurally simpler than the exit above, and the simplicity is the safety.
# There is exactly ONE leg, because a broker position is one contract, and this
# builder has no way to express more than one:
#
#   * it never assembles a combo. Two orphan holdings that *look* like a
#     straddle may or may not have been bought as one, and nothing in this
#     system recorded which. A combo built from that guess would be a structure
#     nobody chose, submitted as one order that fills or does not — turning an
#     invented relationship into a real trade;
#   * it never nets, never pairs and never groups. One holding, one order,
#     one quantity, read from the broker.
#
# Closing four long holdings as four independent orders cannot create a short
# leg, which is the failure the multi-leg rule exists to prevent — and a
# *short* holding is refused outright before this is reached.
# ---------------------------------------------------------------------------
def cleanup_intent_identifier(
    *,
    cleanup_request_id: str,
    contract_key: str,
    schema_version: str = EXECUTION_SCHEMA_VERSION,
) -> str:
    """Derive a cleanup intent's identity from the request and the contract."""
    digest = stable_hash(["CLEANUP_ORDER_INTENT", schema_version, cleanup_request_id, contract_key])
    return f"intent-cleanup-{digest[:20]}"


def build_cleanup_order_intent(
    *,
    cleanup_request_id: str,
    contract_key: str,
    underlying: str,
    contract_id: int | None,
    right: OptionRight | None,
    strike: Decimal | None,
    expiration: date | None,
    multiplier: int | None,
    quantity: Decimal,
    reference_quote: Decimal | None,
    local_symbol: str | None,
    config: CleanupOrderConfig,
    trading_mode: TradingMode,
    created_at: datetime,
    versions: SystemVersions,
) -> OrderIntent:
    """Build the ``OrderIntent`` that closes one pre-existing broker holding.

    Every field comes from what the broker actually reported about the holding.
    Nothing is derived from a symbol, nothing is defaulted to a convention, and
    the resulting intent carries no purchase card, no risk decision and no
    strategy — because none exists.

    Refusals, each a different fact:

    * a **short** holding is ``SHORT_POSITION_NOT_SUPPORTED``. Closing one is a
      purchase whose cost is unbounded above;
    * a **fractional** quantity is ``INVALID_QUANTITY``. An option position is
      whole contracts; a fraction means the snapshot is describing something
      this builder does not understand;
    * a missing **contract id** is ``CONTRACT_ID_MISSING``. Rebuilding one from
      symbol, strike and expiration would send an order for a contract nobody
      holds — adjusted contracts share all four;
    * a missing **multiplier** is ``MULTIPLIER_MISSING``. Never assumed to be
      100 (specification section 18);
    * a missing **price** is ``PRICE_UNAVAILABLE``. Not the average cost, not
      the strike, not a midpoint of one side.
    """
    if config.order_type is not OrderType.LIMIT:
        raise OrderBuildError(
            ExecutionReasonCode.ORDER_TYPE_NOT_PERMITTED,
            f"cleanup.order.order_type is {config.order_type.value}. A market order to sell an "
            f"option is an unbounded price in the one direction that cannot be recovered from",
        )
    if quantity < 0:
        raise OrderBuildError(
            ExecutionReasonCode.SHORT_POSITION_NOT_SUPPORTED,
            f"{contract_key} is short {abs(quantity)} contract(s). Closing a short holding is a "
            f"purchase whose cost is unbounded above, and nothing here is authorised to decide "
            f"it. It is reported and left exactly where it is",
        )
    if quantity != quantity.to_integral_value():
        raise OrderBuildError(
            ExecutionReasonCode.INVALID_QUANTITY,
            f"{contract_key} reports a fractional quantity of {quantity}. An option position is "
            f"whole contracts; this snapshot describes something this builder does not model",
        )
    units = int(quantity)
    if units < 1:
        raise OrderBuildError(
            ExecutionReasonCode.POSITION_NOT_AT_BROKER,
            f"{contract_key} reports a quantity of {quantity}; there is nothing held to close",
        )
    if contract_id is None or contract_id <= 0:
        raise OrderBuildError(
            ExecutionReasonCode.CONTRACT_ID_MISSING,
            f"{contract_key} carries no broker contract id. Rebuilding one from symbol, strike "
            f"and expiration would send a closing order for a contract nobody holds",
        )
    if right is None or strike is None or expiration is None:
        raise OrderBuildError(
            ExecutionReasonCode.CONTRACT_INVALID,
            f"{contract_key} is missing part of its option identity "
            f"(right={right}, strike={strike}, expiration={expiration}); an order cannot be "
            f"built for a contract this incompletely described",
        )
    if multiplier is None or multiplier < 1:
        raise OrderBuildError(
            ExecutionReasonCode.MULTIPLIER_MISSING,
            f"{contract_key} carries no contract multiplier. A standard US equity option is "
            f"100 and a system that assumed so would misprice the first one that is not",
        )
    if reference_quote is None:
        raise OrderBuildError(
            ExecutionReasonCode.PRICE_UNAVAILABLE,
            f"the broker reports no market price for {contract_key}, so no limit price can be "
            f"derived from one. It is not the average cost, not the strike and not a midpoint "
            f"conjured from one side: an unpriced holding is left alone",
        )

    # Reused rather than re-derived: identical arithmetic to the exit side —
    # negative offset, round down, refuse a non-positive result — because it is
    # the same question. A second copy would be a second place for the rounding
    # direction to be got wrong.
    limit_price = exit_limit_price(
        reference_quote,
        offset_pct=config.limit_price_offset_pct,
        increment=config.price_increment,
    )

    return OrderIntent(
        intent_id=cleanup_intent_identifier(
            cleanup_request_id=cleanup_request_id, contract_key=contract_key
        ),
        intent=ExecutionIntent.CLEANUP,
        purchase_card_id=None,
        risk_decision_id=None,
        created_at=created_at,
        underlying=underlying,
        strategy_type=None,
        legs=[
            OptionLeg(
                underlying=underlying,
                right=right,
                strike=strike,
                expiration=expiration,
                # SELL, because the holding is long — proved above, not assumed.
                action=LegAction.SELL,
                ratio=1,
                multiplier=multiplier,
                occ_symbol=local_symbol,
                broker_contract_id=contract_id,
            )
        ],
        quantity=units,
        order_type=OrderType.LIMIT,
        limit_price=limit_price,
        time_in_force=config.time_in_force,
        max_slippage_bps=0,
        trading_mode=trading_mode,
        versions=versions,
    )


def _closing_leg(leg: OptionLeg) -> OptionLeg:
    """The same contract, in the opposite direction.

    Only ``action`` changes. The contract id, the strike, the expiration, the
    ratio and the multiplier are all copied unchanged — a "normalised" copy
    would be a closing order for a different contract, which is the one
    mistake here that produces a *new* position instead of ending one.
    """
    closing = LegAction.SELL if leg.action is LegAction.BUY else LegAction.BUY
    return leg.model_copy(update={"action": closing})
