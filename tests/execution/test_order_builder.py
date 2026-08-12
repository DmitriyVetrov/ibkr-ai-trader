"""Building the order, and translating it for IBKR (brief section 42.3).

Two layers, tested together because they are one boundary: the deterministic
builder that turns a purchase card into an ``OrderIntent``, and the pure
translation that expresses that intent in IBKR's terms.

The unit conversion is the thing to read carefully. Milestone 7 records the
cost of a structure as *money* — ask x multiplier x ratio, summed over legs — and
a broker limit price is a *quote*, per multiplier unit. Sending 605 where 6.05
was meant is a hundredfold overpayment that every downstream number would
faithfully reproduce.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.broker.ibkr.order_translation import (
    IBKR_BAG_SECURITY_TYPE,
    IBKR_OPTION_SECURITY_TYPE,
    OrderTranslationError,
    to_ibkr_order_request,
)
from trading_system.domain.enums import (
    ExecutionReasonCode,
    LegAction,
    OptionRight,
    OrderType,
    StrategyType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import OptionLeg
from trading_system.execution.order_builder import (
    OrderBuildError,
    build_order_intent,
    limit_price_from_reference,
    reference_quote_of,
)

from .conftest import EXPIRATION, NOW

pytestmark = pytest.mark.unit


@pytest.fixture
def policy(system_config):
    return system_config.execution


@pytest.fixture
def build(policy, versions):
    def _build(card, *, reference_price=Decimal("605.00"), **overrides):
        kwargs = {
            "execution_request_id": "exec-req-0001",
            "risk_decision_id": "risk-0001",
            "reference_price": reference_price,
            "config": policy,
            "order_type": OrderType.LIMIT,
            "time_in_force": TimeInForce.DAY,
            "trading_mode": TradingMode.PAPER,
            "created_at": NOW,
            "versions": versions,
        }
        kwargs.update(overrides)
        return build_order_intent(card, **kwargs)

    return _build


# ---------------------------------------------------------------------------
# Units: the conversion that must happen exactly once
# ---------------------------------------------------------------------------
def test_a_structure_cost_becomes_a_quote_by_dividing_out_the_multiplier() -> None:
    assert reference_quote_of(Decimal("605.00"), 100) == Decimal("6.05")


def test_the_limit_price_is_a_quote_not_a_structure_cost() -> None:
    """The factor-of-100 error this whole conversion exists to prevent."""
    limit = limit_price_from_reference(
        Decimal("605.00"), multiplier=100, offset_pct=0.0, increment=Decimal("0.01")
    )
    assert limit == Decimal("6.05")


def test_a_non_standard_multiplier_is_honoured() -> None:
    """100 is common for US equity options and is never assumed."""
    limit = limit_price_from_reference(
        Decimal("605.00"), multiplier=10, offset_pct=0.0, increment=Decimal("0.01")
    )
    assert limit == Decimal("60.50")


def test_rounding_always_spends_less() -> None:
    """Rounding up would pay a price nobody authorised, one cent at a time.

    An unfilled order is recoverable; an overspend is not.
    """
    limit = limit_price_from_reference(
        Decimal("605.99"), multiplier=100, offset_pct=0.0, increment=Decimal("0.05")
    )
    assert limit == Decimal("6.05")
    assert limit <= Decimal("6.0599")


def test_a_positive_offset_pays_above_the_reference() -> None:
    limit = limit_price_from_reference(
        Decimal("600.00"), multiplier=100, offset_pct=1.0, increment=Decimal("0.01")
    )
    assert limit == Decimal("6.06")


def test_a_non_positive_derived_limit_is_refused() -> None:
    """A limit of zero is malformed, not conservative."""
    with pytest.raises(OrderBuildError) as error:
        limit_price_from_reference(
            Decimal("0.40"), multiplier=100, offset_pct=0.0, increment=Decimal("0.01")
        )
    assert error.value.reason_code is ExecutionReasonCode.INVALID_PRICE


def test_a_zero_reference_price_is_refused() -> None:
    with pytest.raises(OrderBuildError) as error:
        limit_price_from_reference(
            Decimal("0"), multiplier=100, offset_pct=0.0, increment=Decimal("0.01")
        )
    assert error.value.reason_code is ExecutionReasonCode.INVALID_PRICE


# ---------------------------------------------------------------------------
# The intent
# ---------------------------------------------------------------------------
def test_a_long_call_builds(build, executable_card) -> None:
    intent = build(executable_card)

    assert intent.strategy_type is executable_card.strategy_type
    assert intent.order_type is OrderType.LIMIT
    assert intent.limit_price is not None and intent.limit_price > 0


def test_the_quantity_is_copied_not_computed(build, executable_card) -> None:
    intent = build(executable_card)
    assert intent.quantity == executable_card.quantity


def test_the_contract_ids_are_copied_exactly(build, executable_card) -> None:
    intent = build(executable_card)
    assert [leg.broker_contract_id for leg in intent.legs] == [
        leg.broker_contract_id for leg in executable_card.contract.legs
    ]


def test_no_slippage_tolerance_is_granted(build, executable_card) -> None:
    """The limit price *is* the slippage control; a tolerance would widen it."""
    assert build(executable_card).max_slippage_bps == 0


def test_a_market_order_is_refused(build, executable_card) -> None:
    """A market order on an option is an unbounded price."""
    with pytest.raises(OrderBuildError) as error:
        build(executable_card, order_type=OrderType.MARKET)
    assert error.value.reason_code is ExecutionReasonCode.ORDER_TYPE_NOT_PERMITTED


def test_a_zero_quantity_card_cannot_be_constructed(executable_card) -> None:
    """The model refuses it first. Zero units is NO_TRADE, a different answer."""
    from pydantic import ValidationError

    payload = executable_card.model_dump(mode="json") | {"quantity": 0}
    with pytest.raises(ValidationError):
        type(executable_card).model_validate(payload)


def test_the_builder_refuses_a_zero_quantity_it_is_handed_anyway(build, executable_card) -> None:
    """Belt and braces: ``model_copy`` skips validation, and a bug upstream could.

    The builder is the last thing between an authorisation and a broker, so it
    checks rather than assuming the model already did.
    """
    with pytest.raises(OrderBuildError) as error:
        build(executable_card.model_copy(update={"quantity": 0}))
    assert error.value.reason_code is ExecutionReasonCode.INVALID_QUANTITY


def test_a_leg_without_a_contract_id_is_refused(build, executable_card) -> None:
    legs = [executable_card.contract.legs[0].model_copy(update={"broker_contract_id": None})]
    card = executable_card.model_copy(
        update={"contract": executable_card.contract.model_copy(update={"legs": legs})}
    )

    with pytest.raises(OrderBuildError) as error:
        build(card)
    assert error.value.reason_code is ExecutionReasonCode.CONTRACT_ID_MISSING


def test_a_short_leg_is_refused(build, executable_card) -> None:
    """No strategy this system can select is short premium."""
    legs = [executable_card.contract.legs[0].model_copy(update={"action": LegAction.SELL})]
    card = executable_card.model_copy(
        update={"contract": executable_card.contract.model_copy(update={"legs": legs})}
    )

    with pytest.raises(OrderBuildError) as error:
        build(card)
    assert error.value.reason_code is ExecutionReasonCode.SHORT_LEG_NOT_SUPPORTED


def test_legs_with_different_multipliers_are_refused(build, executable_card, straddle_legs) -> None:
    """The net price of a combo is only defined when the legs share a size."""
    legs = [straddle_legs[0], straddle_legs[1].model_copy(update={"multiplier": 10})]
    card = executable_card.model_copy(
        update={
            "strategy_type": StrategyType.LONG_STRADDLE,
            "contract": executable_card.contract.model_copy(
                update={"legs": legs, "strategy_type": StrategyType.LONG_STRADDLE}
            ),
        }
    )

    with pytest.raises(OrderBuildError) as error:
        build(card)
    assert error.value.reason_code is ExecutionReasonCode.CONTRACT_INVALID


def test_building_is_deterministic(build, executable_card) -> None:
    first = build(executable_card)
    second = build(executable_card)
    assert first.model_dump() == second.model_dump()


# ---------------------------------------------------------------------------
# What the builder must never do
# ---------------------------------------------------------------------------
def test_the_order_builder_reaches_no_broker_no_model_and_no_repository(repo_root) -> None:
    """Brief 42.3: it does not call a broker, an LLM, risk or sizing."""
    import ast

    source = (repo_root / "src" / "trading_system" / "execution" / "order_builder.py").read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for forbidden in (
        "trading_system.broker",
        "trading_system.agents",
        "trading_system.risk",
        "trading_system.allocation",
        "anthropic",
        "ib_async",
        "socket",
    ):
        assert not any(name.startswith(forbidden) for name in imported), (
            f"order_builder imports {forbidden}"
        )


def test_the_order_builder_reads_no_clock(repo_root) -> None:
    """The instant is passed in, so a built order is reproducible."""
    source = (repo_root / "src" / "trading_system" / "execution" / "order_builder.py").read_text()
    for forbidden in ("datetime.now(", "date.today(", "time.time("):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# IBKR translation: single leg
# ---------------------------------------------------------------------------
def test_a_single_leg_order_addresses_the_contract_by_id(make_intent) -> None:
    request = to_ibkr_order_request(make_intent())

    assert request.security_type == IBKR_OPTION_SECURITY_TYPE
    assert request.contract_id == 771234567
    assert request.action == "BUY"
    assert request.order_type == "LMT"
    assert request.total_quantity == 1
    assert request.limit_price == Decimal("6.05")
    assert request.time_in_force == "DAY"
    assert not request.is_combo


def test_the_order_carries_the_intent_id_as_its_reference(make_intent) -> None:
    """So a broker-side order can be traced back to the record that made it."""
    assert to_ibkr_order_request(make_intent()).order_ref == "intent-0001"


def test_an_order_is_always_transmitted(make_intent) -> None:
    """``transmit=False`` parks an order in TWS for a human; that would be a lie."""
    assert to_ibkr_order_request(make_intent()).transmit is True


def test_a_missing_contract_id_is_refused(make_intent) -> None:
    legs = [
        OptionLeg(
            underlying="NVDA",
            right=OptionRight.CALL,
            strike=Decimal("180.00"),
            expiration=EXPIRATION,
            action=LegAction.BUY,
            multiplier=100,
            broker_contract_id=None,
        )
    ]
    with pytest.raises(OrderTranslationError, match="conId"):
        to_ibkr_order_request(make_intent(legs=legs))


# ---------------------------------------------------------------------------
# IBKR translation: multi-leg
# ---------------------------------------------------------------------------
def test_a_straddle_becomes_one_combo_order(make_intent, straddle_legs) -> None:
    """Brief section 9: a multi-leg structure is ONE logical trade."""
    request = to_ibkr_order_request(
        make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_STRADDLE)
    )

    assert request.is_combo
    assert request.security_type == IBKR_BAG_SECURITY_TYPE
    assert request.contract_id is None
    assert len(request.combo_legs) == 2
    assert {leg.contract_id for leg in request.combo_legs} == {771234567, 771234568}
    assert all(leg.action == "BUY" for leg in request.combo_legs)


def test_a_strangle_becomes_one_combo_order(make_intent, straddle_legs) -> None:
    legs = [straddle_legs[0], straddle_legs[1].model_copy(update={"strike": Decimal("170.00")})]
    request = to_ibkr_order_request(
        make_intent(legs=legs, strategy_type=StrategyType.LONG_STRANGLE)
    )

    assert request.is_combo
    assert len(request.combo_legs) == 2


def test_combo_legs_are_ordered_deterministically(make_intent, straddle_legs) -> None:
    """Two runs must produce byte-identical requests, leg order included."""
    forward = to_ibkr_order_request(
        make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_STRADDLE)
    )
    reversed_legs = to_ibkr_order_request(
        make_intent(legs=list(reversed(straddle_legs)), strategy_type=StrategyType.LONG_STRADDLE)
    )
    assert forward == reversed_legs


def test_the_combo_limit_price_is_the_net_for_one_unit(make_intent, straddle_legs) -> None:
    request = to_ibkr_order_request(
        make_intent(
            legs=straddle_legs,
            strategy_type=StrategyType.LONG_STRADDLE,
            limit_price=Decimal("11.50"),
        )
    )
    assert request.limit_price == Decimal("11.50")


def test_a_combo_with_mixed_directions_is_refused(make_intent, straddle_legs) -> None:
    """An untested net-price sign convention is a credit order where a debit was meant."""
    legs = [straddle_legs[0], straddle_legs[1].model_copy(update={"action": LegAction.SELL})]

    with pytest.raises(OrderTranslationError, match="mixes leg directions"):
        to_ibkr_order_request(make_intent(legs=legs, strategy_type=StrategyType.LONG_STRADDLE))


def test_an_unsupported_multi_leg_structure_is_refused(make_intent, straddle_legs) -> None:
    """Never approximated with unrelated single-leg orders."""
    with pytest.raises(OrderTranslationError):
        to_ibkr_order_request(make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_CALL))


def test_the_translation_imports_nothing_from_ib_async(repo_root) -> None:
    """Which is what lets it be tested exhaustively without a gateway."""
    source = (
        repo_root / "src" / "trading_system" / "broker" / "ibkr" / "order_translation.py"
    ).read_text()
    assert "import ib_async" not in source
    assert "from ib_async" not in source
