"""Multi-leg structures (brief sections 42.8 and 9).

A straddle is one position, one authorisation and one order. The failure this
prevents is specific and expensive: submitting the legs independently can leave
a naked long call where a straddle was authorised — a position nobody approved,
against limits nobody checked for it, with a different payoff from the one the
research supported.

So the answer is a combo order, and where a combo cannot be built the answer is
``MULTI_LEG_UNSUPPORTED`` — a refusal, never an approximation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.broker.ibkr.order_translation import (
    OrderTranslationError,
    to_ibkr_order_request,
)
from trading_system.domain.enums import (
    ExecutionReasonCode,
    ExecutionState,
    LegAction,
    OptionRight,
    OrderStatus,
    OrderType,
    StrategyType,
    TradingMode,
)
from trading_system.domain.models import OptionLeg
from trading_system.execution.execution_engine import ExecutionEngine
from trading_system.execution.models import ExecutionLeg
from trading_system.execution.validation import ExecutionValidator

from .conftest import EXPIRATION, NOW

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(repository, clock):
    def _engine(broker):
        return ExecutionEngine(broker=broker, repository=repository, clock=clock)

    return _engine


@pytest.fixture
def straddle_record(make_record):
    """One record for a two-leg structure: one trade, not two."""
    return make_record(
        strategy=StrategyType.LONG_STRADDLE,
        legs=[
            ExecutionLeg(
                leg_index=0,
                contract_id=771234567,
                action=LegAction.BUY,
                right=OptionRight.CALL,
                underlying="NVDA",
                expiration=EXPIRATION,
                strike=Decimal("180.00"),
                multiplier=100,
                trading_class="NVDA",
                currency="EUR",
            ),
            ExecutionLeg(
                leg_index=1,
                contract_id=771234568,
                action=LegAction.BUY,
                right=OptionRight.PUT,
                underlying="NVDA",
                expiration=EXPIRATION,
                strike=Decimal("180.00"),
                multiplier=100,
                trading_class="NVDA",
                currency="EUR",
            ),
        ],
        reference_price=Decimal("1150.00"),
        reference_quote=Decimal("11.50"),
        submitted_price=Decimal("11.50"),
        capital_commitment=Decimal("1150.00"),
        maximum_loss=Decimal("1150.00"),
    )


# ---------------------------------------------------------------------------
# One structure, one order
# ---------------------------------------------------------------------------
def test_a_straddle_is_submitted_as_exactly_one_order(
    engine, straddle_record, make_intent, straddle_legs, fake_broker
) -> None:
    broker = fake_broker()

    outcome = engine(broker).submit(
        straddle_record,
        make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_STRADDLE),
    )

    assert broker.orders_submitted == 1, "two orders would risk a half-filled structure"
    assert len(broker.received) == 1
    assert outcome.record.state is ExecutionState.SUBMITTED


def test_the_one_order_carries_both_legs(
    engine, straddle_record, make_intent, straddle_legs, fake_broker
) -> None:
    broker = fake_broker()

    engine(broker).submit(
        straddle_record,
        make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_STRADDLE),
    )

    [sent] = broker.received
    assert len(sent.legs) == 2
    assert {leg.right for leg in sent.legs} == {OptionRight.CALL, OptionRight.PUT}


def test_a_strangle_is_one_order_too(engine, straddle_record, make_intent, fake_broker) -> None:
    legs = [
        OptionLeg(
            underlying="NVDA",
            right=OptionRight.CALL,
            strike=Decimal("190.00"),
            expiration=EXPIRATION,
            action=LegAction.BUY,
            multiplier=100,
            broker_contract_id=771234569,
        ),
        OptionLeg(
            underlying="NVDA",
            right=OptionRight.PUT,
            strike=Decimal("170.00"),
            expiration=EXPIRATION,
            action=LegAction.BUY,
            multiplier=100,
            broker_contract_id=771234570,
        ),
    ]
    broker = fake_broker()
    record = straddle_record.model_copy(update={"strategy": StrategyType.LONG_STRANGLE})

    engine(broker).submit(record, make_intent(legs=legs, strategy_type=StrategyType.LONG_STRANGLE))

    assert broker.orders_submitted == 1


def test_the_structure_fills_as_a_whole_or_not_at_all(
    engine, straddle_record, make_intent, straddle_legs, fake_broker
) -> None:
    """A combo's fill count is in *structure* units, not legs."""
    broker = fake_broker(
        status=OrderStatus.FILLED, filled_quantity=1, average_fill_price=Decimal("11.48")
    )

    outcome = engine(broker).submit(
        straddle_record,
        make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_STRADDLE),
    )

    assert outcome.record.state is ExecutionState.FILLED
    assert outcome.record.filled_quantity == 1


def test_a_half_filled_structure_is_never_reported_as_filled(
    engine, straddle_record, make_intent, straddle_legs, fake_broker
) -> None:
    """Brief 42.8: an incomplete structure must not silently become FILLED."""
    broker = fake_broker(
        status=OrderStatus.FILLED, filled_quantity=1, average_fill_price=Decimal("11.48")
    )
    record = straddle_record.model_copy(update={"quantity": 4})

    outcome = engine(broker).submit(
        record,
        make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_STRADDLE, quantity=4),
    )

    assert outcome.record.state is ExecutionState.PARTIALLY_FILLED
    assert outcome.record.filled_quantity == 1
    assert outcome.record.remaining_quantity == 3


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------
def test_a_leg_with_no_contract_id_refuses_the_whole_structure(make_intent, straddle_legs) -> None:
    legs = [straddle_legs[0], straddle_legs[1].model_copy(update={"broker_contract_id": None})]

    with pytest.raises(OrderTranslationError, match="conId"):
        to_ibkr_order_request(make_intent(legs=legs, strategy_type=StrategyType.LONG_STRADDLE))


def test_a_broker_rejection_rejects_the_whole_structure(
    engine, straddle_record, make_intent, straddle_legs, fake_broker
) -> None:
    """There is one order, so there is one rejection and no orphan leg."""
    broker = fake_broker(status=OrderStatus.REJECTED, message="combo not supported")

    outcome = engine(broker).submit(
        straddle_record,
        make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_STRADDLE),
    )

    assert outcome.record.state is ExecutionState.REJECTED
    assert outcome.record.filled_quantity == 0
    assert broker.orders_submitted == 1


def test_independent_leg_orders_cannot_be_configured(system_config) -> None:
    """The configuration refuses to load rather than permitting the unsafe path."""
    from pydantic import ValidationError

    from trading_system.infrastructure.settings import ExecutionConfig

    payload = system_config.execution.model_dump() | {"allow_independent_leg_orders": True}
    with pytest.raises(ValidationError, match="naked long call"):
        ExecutionConfig.model_validate(payload)


def test_disabling_combos_leaves_no_way_to_submit_a_structure(system_config) -> None:
    from pydantic import ValidationError

    from trading_system.infrastructure.settings import ExecutionConfig

    payload = system_config.execution.model_dump() | {"multi_leg_as_combo": False}
    with pytest.raises(ValidationError, match="no way to submit"):
        ExecutionConfig.model_validate(payload)


def test_a_structure_the_translation_does_not_support_is_refused(
    make_intent, straddle_legs
) -> None:
    """Refused deterministically — never approximated with unrelated orders."""
    with pytest.raises(OrderTranslationError):
        to_ibkr_order_request(make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_PUT))


def test_legs_across_expirations_are_refused_by_validation(
    system_config, approved_allocation
) -> None:
    from datetime import timedelta

    from trading_system.data.market_calendar import MarketCalendar

    validator = ExecutionValidator(
        config=system_config.execution,
        campaign=system_config.campaign,
        calendar=MarketCalendar(system_config.data.market_calendar),
    )
    leg = approved_allocation.legs[0]
    other = leg.model_copy(
        update={
            "leg_index": 1,
            "contract_id": leg.contract_id + 1,
            "expiration": leg.expiration + timedelta(days=7),
        }
    )
    validation = validator.validate(
        approved_allocation.model_copy(update={"legs": [leg, other]}),
        execution_now=NOW,
        trading_mode=TradingMode.PAPER,
        order_type=OrderType.LIMIT,
        dry_run=True,
    )

    assert ExecutionReasonCode.MULTI_LEG_UNSUPPORTED in validation.reason_codes


# ---------------------------------------------------------------------------
# The simulator handles the structure too
# ---------------------------------------------------------------------------
def test_the_simulator_accepts_a_combo_as_one_order(
    writable_simulator, make_intent, straddle_legs
) -> None:
    result = writable_simulator.place_order(
        make_intent(legs=straddle_legs, strategy_type=StrategyType.LONG_STRADDLE)
    )

    assert writable_simulator.orders_submitted == 1
    assert len(writable_simulator.book.orders) == 1
    assert result.status is OrderStatus.SUBMITTED
