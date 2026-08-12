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

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    ExecutionReasonCode,
    LegAction,
    OrderType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import OptionLeg, OrderIntent, PurchaseCard, SystemVersions
from trading_system.execution.models import EXECUTION_SCHEMA_VERSION
from trading_system.infrastructure.settings import ExecutionConfig

__all__ = [
    "OrderBuildError",
    "build_order_intent",
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
