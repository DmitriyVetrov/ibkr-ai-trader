"""Translating IBKR fills into domain models.

Executions are the ground truth for what was actually traded, so this
translation is strict: anything ambiguous is rejected rather than guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_system.broker.ibkr.executions import to_broker_execution, to_broker_executions
from trading_system.domain.enums import OrderSide, SecurityType

from .conftest import BROKER_NOW, NAN, make_fill, make_stock_contract


@pytest.mark.unit
def test_execution_is_fully_translated() -> None:
    execution = to_broker_execution(make_fill(), BROKER_NOW)

    assert execution.execution_id == "0000e1a7.68000001.01.01"
    assert execution.symbol == "NVDA"
    assert execution.security_type is SecurityType.OPTION
    assert execution.side is OrderSide.BUY
    assert execution.quantity == Decimal("2")
    assert execution.price == Decimal("5.95")
    assert execution.commission == Decimal("1.3")
    assert execution.currency == "USD"
    assert execution.source == "IBKR"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ibkr_side", "expected"),
    [("BOT", OrderSide.BUY), ("SLD", OrderSide.SELL)],
)
def test_ibkr_side_codes_are_translated(ibkr_side: str, expected: OrderSide) -> None:
    """IBKR reports fills as BOT/SLD, not BUY/SELL."""
    execution = to_broker_execution(make_fill(side=ibkr_side), BROKER_NOW)
    assert execution.side is expected


@pytest.mark.unit
def test_execution_links_to_its_order_by_perm_id() -> None:
    execution = to_broker_execution(make_fill(perm_id=900001), BROKER_NOW)
    assert execution.broker_order_id == "900001"


@pytest.mark.unit
def test_execution_timestamp_is_utc() -> None:
    execution = to_broker_execution(make_fill(), BROKER_NOW)
    assert execution.executed_at == datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


@pytest.mark.unit
def test_naive_execution_time_is_treated_as_utc() -> None:
    """IBKR documents execution times as UTC; a naive value must not shift."""
    fill = make_fill()
    fill.execution.time = datetime(2026, 8, 10, 14, 30)
    execution = to_broker_execution(fill, BROKER_NOW)
    assert execution.executed_at == datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


@pytest.mark.unit
def test_missing_commission_is_none_not_zero() -> None:
    """A commission of zero and an unreported commission are different facts."""
    execution = to_broker_execution(make_fill(commission=None), BROKER_NOW)
    assert execution.commission is None


@pytest.mark.unit
def test_stock_fill_is_supported() -> None:
    execution = to_broker_execution(
        make_fill(contract=make_stock_contract(), price=505.0), BROKER_NOW
    )
    assert execution.security_type is SecurityType.STOCK
    assert execution.price == Decimal("505.0")


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_execution_without_an_id_is_rejected() -> None:
    fill = make_fill()
    fill.execution.execId = ""
    with pytest.raises(ValueError, match="execId"):
        to_broker_execution(fill, BROKER_NOW)


@pytest.mark.unit
def test_execution_without_a_price_is_rejected() -> None:
    with pytest.raises(ValueError, match="price"):
        to_broker_execution(make_fill(price=NAN), BROKER_NOW)


@pytest.mark.unit
def test_execution_without_a_quantity_is_rejected() -> None:
    with pytest.raises(ValueError, match="quantity"):
        to_broker_execution(make_fill(shares=0.0), BROKER_NOW)


@pytest.mark.unit
def test_execution_with_an_unrecognised_side_is_rejected() -> None:
    with pytest.raises(ValueError, match="unrecognised side"):
        to_broker_execution(make_fill(side="???"), BROKER_NOW)


@pytest.mark.unit
def test_execution_without_a_timestamp_is_rejected() -> None:
    fill = make_fill()
    fill.execution.time = None
    fill.time = None
    with pytest.raises(ValueError, match="timestamp"):
        to_broker_execution(fill, BROKER_NOW)


@pytest.mark.unit
def test_fill_without_an_execution_is_rejected() -> None:
    fill = make_fill()
    fill.execution = None
    with pytest.raises(ValueError, match="execution"):
        to_broker_execution(fill, BROKER_NOW)


@pytest.mark.unit
def test_translating_many_executions() -> None:
    executions = to_broker_executions([make_fill(exec_id="a"), make_fill(exec_id="b")], BROKER_NOW)
    assert [e.execution_id for e in executions] == ["a", "b"]
