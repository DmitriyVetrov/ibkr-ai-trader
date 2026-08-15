"""Translate an :class:`OrderIntent` into the fields IBKR needs.

The mirror image of :mod:`trading_system.broker.ibkr.orders`, which translates
IBKR's answers back into domain models. Like that module — and like
``positions``, ``executions`` and ``market_data`` — this one is **pure** and
imports nothing from ``ib_async``. It produces a plain description of the
contract and the order; :mod:`trading_system.broker.ibkr.client` is the only
place that turns that description into library objects and sends it.

Keeping the two apart is what lets the whole translation be tested, exhaustively
and offline, without a gateway. The part that cannot be tested without one is
then reduced to attribute assignment.

Two IBKR-specific decisions live here:

* **A multi-leg structure becomes one BAG order.** IBKR's combo order fills the
  structure or does not fill it. Submitting a straddle as two orders risks a
  half-filled structure — a naked long call where a straddle was authorised,
  against limits nobody checked for one. The combo is why that cannot happen.
* **Every contract is addressed by ``conId``.** Milestone 6 resolved these
  against a real chain and Milestone 7 carried them here. Rebuilding a contract
  from symbol, strike, right and expiry would be selecting a contract at
  execution time, and would additionally cost a qualification round trip that
  the one-reliable-round-trip constraint makes unsafe to spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from trading_system.domain.enums import LegAction, OrderType, StrategyType, TimeInForce
from trading_system.domain.models import OrderIntent

__all__ = [
    "IBKR_BAG_SECURITY_TYPE",
    "IBKR_OPTION_SECURITY_TYPE",
    "IBKRComboLeg",
    "IBKROrderRequest",
    "OrderTranslationError",
    "to_ibkr_order_request",
]

IBKR_OPTION_SECURITY_TYPE = "OPT"
IBKR_BAG_SECURITY_TYPE = "BAG"

#: IBKR order type codes. Deliberately just the one: this milestone permits
#: LIMIT and nothing else, and a wider table would be a wider capability.
_ORDER_TYPE_CODES = {OrderType.LIMIT: "LMT"}

_ACTION_CODES = {LegAction.BUY: "BUY", LegAction.SELL: "SELL"}


class OrderTranslationError(RuntimeError):
    """An intent could not be expressed as an IBKR order."""


@dataclass(frozen=True, slots=True)
class IBKRComboLeg:
    """One leg of a BAG order, addressed by contract id."""

    contract_id: int
    ratio: int
    action: str
    exchange: str


@dataclass(frozen=True, slots=True)
class IBKROrderRequest:
    """Everything IBKR needs, and nothing it does not.

    A flat, comparable description: two calls that would send the same order
    produce two equal instances, which is what makes "what exactly would be
    submitted" a printable answer rather than a claim.
    """

    # --- the contract ------------------------------------------------------
    security_type: str
    symbol: str
    exchange: str
    currency: str
    #: Set for a single-leg order; ``None`` for a BAG, which is defined by its
    #: legs rather than by a contract id of its own.
    contract_id: int | None = None
    trading_class: str | None = None
    local_symbol: str | None = None
    multiplier: int | None = None
    combo_legs: tuple[IBKRComboLeg, ...] = field(default_factory=tuple)

    # --- the order ---------------------------------------------------------
    action: str = "BUY"
    total_quantity: int = 0
    order_type: str = "LMT"
    limit_price: Decimal | None = None
    time_in_force: str = "DAY"
    account: str | None = None
    order_ref: str | None = None
    #: Never true in this milestone. IBKR's ``transmit=False`` parks an order in
    #: TWS for a human to send; an execution engine that used it would report a
    #: submission that had not happened.
    transmit: bool = True

    @property
    def is_combo(self) -> bool:
        return self.security_type == IBKR_BAG_SECURITY_TYPE

    def describe(self) -> str:
        """One line, for the CLI's "this is what would be sent"."""
        if self.is_combo:
            parts = "+".join(
                f"{leg.action} {leg.ratio}x{leg.contract_id}" for leg in self.combo_legs
            )
            what = f"BAG[{parts}]"
        else:
            what = f"conId {self.contract_id}"
        price = f" @ {self.limit_price}" if self.limit_price is not None else ""
        return (
            f"{self.action} {self.total_quantity} {self.symbol} {what} "
            f"{self.order_type}{price} {self.time_in_force}"
        )


def to_ibkr_order_request(
    intent: OrderIntent,
    *,
    exchange: str = "SMART",
    currency: str = "USD",
    trading_class: str | None = None,
    account: str | None = None,
) -> IBKROrderRequest:
    """Build the IBKR request for one order intent.

    Deterministic: same intent, same arguments, same request — including leg
    order, which is sorted by contract id so two runs cannot produce combos that
    differ only in the order IBKR sees them.
    """
    if intent.order_type not in _ORDER_TYPE_CODES:
        raise OrderTranslationError(
            f"{intent.order_type.value} is not a permitted order type. This milestone submits "
            f"LIMIT orders only: a market order on an option is an unbounded price"
        )
    if intent.limit_price is None or intent.limit_price <= 0:
        raise OrderTranslationError(
            f"a {intent.order_type.value} order needs a positive limit price, got "
            f"{intent.limit_price}"
        )
    if intent.quantity < 1:
        raise OrderTranslationError(f"quantity {intent.quantity} is not submittable")
    if not intent.legs:
        raise OrderTranslationError("an order intent with no legs cannot be submitted")

    for index, leg in enumerate(intent.legs):
        if leg.broker_contract_id is None or leg.broker_contract_id <= 0:
            raise OrderTranslationError(
                f"leg {index} has no broker contract id. IBKR contracts are addressed by "
                f"conId here on purpose: rebuilding one from symbol, strike and expiry would "
                f"be selecting a contract at execution time"
            )
        if leg.multiplier < 1:
            raise OrderTranslationError(f"leg {index} has no contract multiplier")

    order_type = _ORDER_TYPE_CODES[intent.order_type]
    tif = _time_in_force(intent.time_in_force)

    if len(intent.legs) == 1:
        leg = intent.legs[0]
        assert leg.broker_contract_id is not None  # narrowing; checked above
        return IBKROrderRequest(
            security_type=IBKR_OPTION_SECURITY_TYPE,
            symbol=intent.underlying,
            exchange=exchange,
            currency=currency,
            contract_id=leg.broker_contract_id,
            trading_class=trading_class,
            local_symbol=leg.occ_symbol,
            multiplier=leg.multiplier,
            action=_ACTION_CODES[leg.action],
            total_quantity=intent.quantity,
            order_type=order_type,
            limit_price=intent.limit_price,
            time_in_force=tif,
            account=account,
            order_ref=intent.intent_id,
        )

    return _combo_request(
        intent,
        exchange=exchange,
        currency=currency,
        order_type=order_type,
        tif=tif,
        account=account,
    )


def _combo_request(
    intent: OrderIntent,
    *,
    exchange: str,
    currency: str,
    order_type: str,
    tif: str,
    account: str | None,
) -> IBKROrderRequest:
    """Express a multi-leg structure as a single BAG order.

    The BAG's own action is ``BUY`` and the direction of each leg lives on the
    leg, which is how IBKR models a combo. For the long-premium structures this
    system can select every leg is a BUY, and the limit price is the net debit
    for one unit of the structure.

    A structure with mixed directions is refused rather than guessed at: the net
    price of a combo whose legs point opposite ways is a sign convention this
    milestone has no shipped strategy to test against, and getting the sign
    wrong would send an order to *receive* a credit where a debit was intended.
    """
    if intent.strategy_type is None:
        # Only a CLEANUP intent has no strategy, and a cleanup closes exactly
        # one broker-observed contract. Reaching the combo path with no
        # strategy means several legs were bundled into a structure nobody
        # recorded — precisely the guess this refuses to make.
        raise OrderTranslationError(
            f"an order intent with {len(intent.legs)} legs carries no strategy type, so there "
            f"is no recorded structure to express as a combo. A multi-leg order assembled from "
            f"holdings whose relationship nobody recorded is an invented structure"
        )
    actions = sorted({leg.action.value for leg in intent.legs})
    if actions != [LegAction.BUY.value]:
        raise OrderTranslationError(
            f"{intent.strategy_type.value} mixes leg directions {actions}. Every strategy this "
            f"system can select is long premium; a combo with mixed directions needs a "
            f"net-price sign convention no shipped strategy exercises, and an untested sign "
            f"is a credit order where a debit was meant"
        )
    if intent.strategy_type not in _COMBO_STRATEGIES:
        raise OrderTranslationError(
            f"{intent.strategy_type.value} is not a structure this translation supports as a combo"
        )

    legs = tuple(
        IBKRComboLeg(
            contract_id=leg.broker_contract_id,
            ratio=leg.ratio,
            action=_ACTION_CODES[leg.action],
            exchange=exchange,
        )
        # Sorted so the request is byte-identical across runs. IBKR does not
        # care about leg order; reproducibility does.
        for leg in sorted(intent.legs, key=lambda item: (item.broker_contract_id or 0, item.strike))
        if leg.broker_contract_id is not None
    )
    if len(legs) != len(intent.legs):
        raise OrderTranslationError("a combo leg lost its contract id during translation")

    return IBKROrderRequest(
        security_type=IBKR_BAG_SECURITY_TYPE,
        symbol=intent.underlying,
        exchange=exchange,
        currency=currency,
        multiplier=intent.legs[0].multiplier,
        combo_legs=legs,
        action="BUY",
        total_quantity=intent.quantity,
        order_type=order_type,
        limit_price=intent.limit_price,
        time_in_force=tif,
        account=account,
        order_ref=intent.intent_id,
    )


#: Multi-leg structures this translation is written and tested for. A strategy
#: absent here is ``MULTI_LEG_UNSUPPORTED``, which is a refusal — never an
#: approximation built from unrelated single-leg orders.
_COMBO_STRATEGIES = frozenset({StrategyType.LONG_STRADDLE, StrategyType.LONG_STRANGLE})


def _time_in_force(tif: TimeInForce) -> str:
    return {TimeInForce.DAY: "DAY", TimeInForce.GTC: "GTC"}[tif]
