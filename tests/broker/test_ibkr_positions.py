"""Translating IBKR positions into domain models."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_system.broker.ibkr.positions import (
    option_positions,
    to_broker_position,
    to_broker_positions,
)
from trading_system.domain.enums import OptionRight, SecurityType

from .conftest import (
    BROKER_NOW,
    NAN,
    make_option_contract,
    make_portfolio_item,
    make_position,
    make_stock_contract,
)


@pytest.mark.unit
def test_option_position_is_fully_translated() -> None:
    position = to_broker_position(make_portfolio_item(), BROKER_NOW)

    assert position.symbol == "NVDA"
    assert position.security_type is SecurityType.OPTION
    assert position.quantity == Decimal("2")
    assert position.expiration == date(2026, 9, 18)
    assert position.strike == Decimal("180.0")
    assert position.right is OptionRight.CALL
    assert position.multiplier == 100
    assert position.source == "IBKR"
    assert position.is_option


@pytest.mark.unit
def test_money_fields_become_exact_decimals() -> None:
    position = to_broker_position(make_portfolio_item(market_price=6.25), BROKER_NOW)

    assert isinstance(position.market_price, Decimal)
    # Decimal(str(6.25)) is exact; Decimal(6.25) would not be.
    assert position.market_price == Decimal("6.25")
    assert position.market_value == Decimal("1250.0")


@pytest.mark.unit
def test_average_cost_is_the_brokers_per_contract_cost() -> None:
    """IBKR reports 595.0 for a 5.95 option: price times multiplier, not price."""
    position = to_broker_position(make_portfolio_item(average_cost=595.0), BROKER_NOW)
    assert position.average_cost == Decimal("595.0")


@pytest.mark.unit
def test_stock_position_has_no_option_terms() -> None:
    item = make_portfolio_item(contract=make_stock_contract(), position=10.0)
    position = to_broker_position(item, BROKER_NOW)

    assert position.security_type is SecurityType.STOCK
    assert position.expiration is None
    # IBKR sends strike 0.0 for stock; that is absence, not a zero strike.
    assert position.strike is None
    assert position.right is None
    assert not position.is_option


@pytest.mark.unit
def test_bare_position_object_is_supported() -> None:
    """`Position` has avgCost and no valuation; the missing fields stay None."""
    position = to_broker_position(make_position(), BROKER_NOW)

    assert position.quantity == Decimal("2")
    assert position.average_cost == Decimal("595.0")
    assert position.market_price is None
    assert position.market_value is None
    assert position.unrealized_pnl is None


@pytest.mark.unit
def test_missing_valuation_is_none_not_zero() -> None:
    """ "Not reported" and "zero" are different facts and must stay different."""
    item = make_portfolio_item(market_price=NAN, market_value=NAN, unrealized=NAN)
    position = to_broker_position(item, BROKER_NOW)

    assert position.market_price is None
    assert position.market_value is None
    assert position.unrealized_pnl is None


@pytest.mark.unit
def test_short_position_keeps_its_sign() -> None:
    position = to_broker_position(make_portfolio_item(position=-3.0), BROKER_NOW)
    assert position.quantity == Decimal("-3")


@pytest.mark.unit
def test_closed_positions_are_dropped() -> None:
    """IBKR keeps reporting a closed contract at quantity 0."""
    items = [
        make_portfolio_item(position=2.0),
        make_portfolio_item(contract=make_stock_contract(), position=0.0),
    ]
    positions = to_broker_positions(items, BROKER_NOW)

    assert len(positions) == 1
    assert positions[0].symbol == "NVDA"


@pytest.mark.unit
def test_account_can_be_supplied_when_the_item_omits_it() -> None:
    item = make_portfolio_item(account="")
    position = to_broker_position(item, BROKER_NOW, account_id="DU7654321")
    assert position.account_id == "DU7654321"


@pytest.mark.unit
def test_position_without_an_account_is_rejected() -> None:
    with pytest.raises(ValueError, match="account"):
        to_broker_position(make_portfolio_item(account=""), BROKER_NOW)


@pytest.mark.unit
def test_position_without_a_contract_is_rejected() -> None:
    item = make_portfolio_item()
    item.contract = None
    with pytest.raises(ValueError, match="contract"):
        to_broker_position(item, BROKER_NOW)


@pytest.mark.unit
def test_position_without_a_quantity_is_rejected() -> None:
    item = make_portfolio_item()
    item.position = NAN
    with pytest.raises(ValueError, match="quantity"):
        to_broker_position(item, BROKER_NOW)


@pytest.mark.unit
def test_option_missing_its_terms_is_rejected() -> None:
    """A half-described option position is unusable and must not be accepted."""
    contract = make_option_contract()
    contract.right = ""
    with pytest.raises(ValueError, match="missing"):
        to_broker_position(make_portfolio_item(contract=contract), BROKER_NOW)


@pytest.mark.unit
def test_unknown_security_type_becomes_other() -> None:
    contract = make_stock_contract()
    contract.secType = "BAG"
    position = to_broker_position(make_portfolio_item(contract=contract), BROKER_NOW)
    assert position.security_type is SecurityType.OTHER


@pytest.mark.unit
def test_option_positions_filter() -> None:
    positions = to_broker_positions(
        [
            make_portfolio_item(),
            make_portfolio_item(contract=make_stock_contract(), position=10.0),
        ],
        BROKER_NOW,
    )
    assert len(option_positions(positions)) == 1


@pytest.mark.unit
def test_timestamps_are_utc() -> None:
    position = to_broker_position(make_portfolio_item(), BROKER_NOW)
    assert position.as_of.tzinfo is not None
    assert position.as_of.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
