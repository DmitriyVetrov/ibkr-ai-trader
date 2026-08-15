"""Building the order that closes one pre-existing holding.

A pure function, so every test here is exact. The two properties worth stating
plainly, because both are ways a system sells something it should not:

* **the quantity is what the broker holds** — not more, not rounded, not
  inferred from anything else;
* **the limit only ever asks for less** than the broker's own reported price,
  because the offset is negative and rounding is always down.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tests.cleanup.conftest import NOW, ORPHAN_CALL_KEY
from trading_system.domain.enums import (
    ExecutionIntent,
    ExecutionReasonCode,
    LegAction,
    OptionRight,
    OrderType,
    TradingMode,
)
from trading_system.domain.models import SystemVersions
from trading_system.execution.order_builder import OrderBuildError, build_cleanup_order_intent
from trading_system.infrastructure.settings import CleanupOrderConfig, SystemConfig

pytestmark = pytest.mark.unit


def _versions() -> SystemVersions:
    return SystemVersions(application_version="0.1.0", config_version="test")


def _build(config: CleanupOrderConfig, **overrides: object):
    defaults: dict[str, object] = {
        "cleanup_request_id": "cleanup-req-test",
        "contract_key": ORPHAN_CALL_KEY,
        "underlying": "SMH",
        "contract_id": 848575117,
        "right": OptionRight.CALL,
        "strike": Decimal("540.00"),
        "expiration": date(2026, 8, 21),
        "multiplier": 100,
        "quantity": Decimal("1"),
        "reference_quote": Decimal("48.77"),
        "local_symbol": "SMH   260821C00540000",
        "config": config,
        "trading_mode": TradingMode.PAPER,
        "created_at": NOW,
        "versions": _versions(),
    }
    return build_cleanup_order_intent(**(defaults | overrides))  # type: ignore[arg-type]


@pytest.fixture
def order_config(system_config: SystemConfig) -> CleanupOrderConfig:
    return system_config.cleanup.order


# ---------------------------------------------------------------------------
# Quantity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("held", ["1", "2", "10"])
def test_cleanup_quantity_equals_observed_quantity(
    held: str, order_config: CleanupOrderConfig
) -> None:
    intent = _build(order_config, quantity=Decimal(held))
    assert intent.quantity == int(held)


def test_no_oversell(order_config: CleanupOrderConfig) -> None:
    """Nothing anywhere rounds a quantity up, pads it, or adds a buffer."""
    intent = _build(order_config, quantity=Decimal("3"))
    assert intent.quantity == 3
    assert intent.legs[0].ratio == 1
    # One leg at ratio 1 means the order sells exactly `quantity` contracts.
    assert intent.quantity * intent.legs[0].ratio == 3


def test_a_short_holding_is_refused_rather_than_bought_back(
    order_config: CleanupOrderConfig,
) -> None:
    with pytest.raises(OrderBuildError) as excinfo:
        _build(order_config, quantity=Decimal("-1"))
    assert excinfo.value.reason_code is ExecutionReasonCode.SHORT_POSITION_NOT_SUPPORTED


def test_a_fractional_quantity_is_refused(order_config: CleanupOrderConfig) -> None:
    with pytest.raises(OrderBuildError) as excinfo:
        _build(order_config, quantity=Decimal("1.5"))
    assert excinfo.value.reason_code is ExecutionReasonCode.INVALID_QUANTITY


def test_a_zero_holding_is_refused(order_config: CleanupOrderConfig) -> None:
    with pytest.raises(OrderBuildError) as excinfo:
        _build(order_config, quantity=Decimal("0"))
    assert excinfo.value.reason_code is ExecutionReasonCode.POSITION_NOT_AT_BROKER


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
def test_the_order_sells_the_contract_the_broker_named(
    order_config: CleanupOrderConfig,
) -> None:
    intent = _build(order_config)
    leg = intent.legs[0]

    assert len(intent.legs) == 1
    assert leg.broker_contract_id == 848575117
    assert leg.action is LegAction.SELL
    assert leg.strike == Decimal("540.00")
    assert leg.expiration == date(2026, 8, 21)
    assert leg.right is OptionRight.CALL
    assert leg.multiplier == 100
    assert leg.occ_symbol == "SMH   260821C00540000"


@pytest.mark.parametrize("missing", [None, 0, -1])
def test_a_missing_contract_id_is_refused(
    missing: int | None, order_config: CleanupOrderConfig
) -> None:
    with pytest.raises(OrderBuildError) as excinfo:
        _build(order_config, contract_id=missing)
    assert excinfo.value.reason_code is ExecutionReasonCode.CONTRACT_ID_MISSING


def test_a_missing_multiplier_is_never_assumed_to_be_one_hundred(
    order_config: CleanupOrderConfig,
) -> None:
    with pytest.raises(OrderBuildError) as excinfo:
        _build(order_config, multiplier=None)
    assert excinfo.value.reason_code is ExecutionReasonCode.MULTIPLIER_MISSING


@pytest.mark.parametrize("field", ["right", "strike", "expiration"])
def test_an_incomplete_option_identity_is_refused(
    field: str, order_config: CleanupOrderConfig
) -> None:
    with pytest.raises(OrderBuildError) as excinfo:
        _build(order_config, **{field: None})
    assert excinfo.value.reason_code is ExecutionReasonCode.CONTRACT_INVALID


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------
def test_a_missing_price_is_refused_rather_than_substituted(
    order_config: CleanupOrderConfig,
) -> None:
    with pytest.raises(OrderBuildError) as excinfo:
        _build(order_config, reference_quote=None)
    assert excinfo.value.reason_code is ExecutionReasonCode.PRICE_UNAVAILABLE


def test_the_limit_never_asks_for_more_than_the_broker_reported(
    order_config: CleanupOrderConfig,
) -> None:
    reference = Decimal("48.77")
    intent = _build(order_config, reference_quote=reference)
    assert intent.limit_price is not None
    assert intent.limit_price < reference


def test_the_limit_rounds_down(order_config: CleanupOrderConfig) -> None:
    """-3% of 48.77 is 47.3069; the cent below is what is sent."""
    intent = _build(order_config, reference_quote=Decimal("48.77"))
    assert intent.limit_price == Decimal("47.30")


def test_a_price_that_would_round_to_nothing_is_refused(
    order_config: CleanupOrderConfig,
) -> None:
    tiny = order_config.model_copy(update={"limit_price_offset_pct": -50.0})
    with pytest.raises(OrderBuildError) as excinfo:
        _build(tiny, reference_quote=Decimal("0.01"))
    assert excinfo.value.reason_code is ExecutionReasonCode.INVALID_PRICE


def test_the_price_is_a_quote_not_money(order_config: CleanupOrderConfig) -> None:
    """48.77, never 4877.46. The multiplier is on the leg, not in the price."""
    intent = _build(order_config, reference_quote=Decimal("48.77"))
    assert intent.limit_price is not None
    assert intent.limit_price < Decimal("100")
    assert intent.legs[0].multiplier == 100


# ---------------------------------------------------------------------------
# What the intent does NOT carry
# ---------------------------------------------------------------------------
def test_the_intent_carries_no_authorisation(order_config: CleanupOrderConfig) -> None:
    intent = _build(order_config)
    assert intent.intent is ExecutionIntent.CLEANUP
    assert intent.purchase_card_id is None
    assert intent.risk_decision_id is None
    assert intent.strategy_type is None


def test_a_cleanup_intent_that_claimed_an_authorisation_fails_to_construct() -> None:
    from trading_system.domain.models import OptionLeg, OrderIntent

    with pytest.raises(ValueError, match="fabricate the provenance"):
        OrderIntent(
            intent_id="intent-x",
            intent=ExecutionIntent.CLEANUP,
            purchase_card_id="card-real",
            created_at=NOW,
            underlying="SMH",
            legs=[
                OptionLeg(
                    underlying="SMH",
                    right=OptionRight.CALL,
                    strike=Decimal("540"),
                    expiration=date(2026, 8, 21),
                    action=LegAction.SELL,
                    multiplier=100,
                    broker_contract_id=1,
                )
            ],
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("47.30"),
            trading_mode=TradingMode.PAPER,
            versions=_versions(),
        )


def test_an_ordinary_intent_still_requires_its_authorisation() -> None:
    """The regression: the conditional must not weaken the ordinary shape."""
    from trading_system.domain.models import OptionLeg, OrderIntent

    with pytest.raises(ValueError, match="must carry"):
        OrderIntent(
            intent_id="intent-x",
            created_at=NOW,
            underlying="SMH",
            legs=[
                OptionLeg(
                    underlying="SMH",
                    right=OptionRight.CALL,
                    strike=Decimal("540"),
                    expiration=date(2026, 8, 21),
                    action=LegAction.BUY,
                    multiplier=100,
                    broker_contract_id=1,
                )
            ],
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("47.30"),
            trading_mode=TradingMode.PAPER,
            versions=_versions(),
        )


# ---------------------------------------------------------------------------
# No invented structure
# ---------------------------------------------------------------------------
def test_the_builder_can_only_ever_produce_one_leg(
    order_config: CleanupOrderConfig,
) -> None:
    """There is no parameter through which a second holding could be bundled."""
    import inspect

    parameters = set(inspect.signature(build_cleanup_order_intent).parameters)
    assert "legs" not in parameters
    assert "targets" not in parameters
    assert _build(order_config).legs.__len__() == 1


def test_a_multi_leg_intent_with_no_strategy_cannot_reach_the_combo_translation() -> None:
    """A structure nobody recorded is never expressed as a combo."""
    from trading_system.broker.ibkr.order_translation import (
        OrderTranslationError,
        to_ibkr_order_request,
    )
    from trading_system.domain.models import OptionLeg, OrderIntent

    legs = [
        OptionLeg(
            underlying="SMH",
            right=right,
            strike=Decimal("542.50"),
            expiration=date(2026, 8, 21),
            action=LegAction.SELL,
            multiplier=100,
            broker_contract_id=contract_id,
        )
        for right, contract_id in ((OptionRight.CALL, 903223753), (OptionRight.PUT, 903224246))
    ]
    intent = OrderIntent(
        intent_id="intent-x",
        intent=ExecutionIntent.CLEANUP,
        created_at=NOW,
        underlying="SMH",
        legs=legs,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("47.30"),
        trading_mode=TradingMode.PAPER,
        versions=_versions(),
    )

    with pytest.raises(OrderTranslationError, match="invented structure"):
        to_ibkr_order_request(intent)


def test_the_intent_id_is_derived_and_stable(order_config: CleanupOrderConfig) -> None:
    first = _build(order_config)
    second = _build(order_config)
    other = _build(order_config, cleanup_request_id="cleanup-req-other")

    assert first.intent_id == second.intent_id
    assert first.intent_id != other.intent_id
    assert first.intent_id.startswith("intent-cleanup-")
