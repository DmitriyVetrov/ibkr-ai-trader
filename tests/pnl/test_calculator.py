"""Realised profit and loss, from broker-confirmed fills and nothing else.

The claims this file exists to check are the ones that would be expensive to
get wrong, because the figure they produce is what the daily loss limit reads
before permitting the next trade:

* the arithmetic is right, in both directions, for every shipped strategy;
* a structure is **one** trade, not one per leg;
* every missing input produces ``NOT_AVAILABLE`` **with no figure attached**,
  rather than a plausible number assembled from an assumption.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.pnl import factories
from tests.pnl.factories import EXIT_AT, NOW
from trading_system.domain.enums import (
    CommissionStatus,
    OptionRight,
    OrderSide,
    PnLReasonCode,
    PnLStatus,
    StrategyType,
)
from trading_system.pnl.calculator import PnLCalculator, PnLInputs, session_date_of

pytestmark = pytest.mark.unit


def compute(entry, closing, *, strategy: StrategyType = StrategyType.LONG_CALL, **overrides):
    """Run the calculator over two lists of fills, with the shipped policy."""
    fields: dict[str, Any] = {
        "position_id": factories.POSITION,
        "campaign_id": factories.CAMPAIGN,
        "underlying": "NVDA",
        "strategy": strategy,
        "entry_fills": entry,
        "exit_fills": closing,
        "computed_at": EXIT_AT,
        "day_boundary_timezone": "America/New_York",
        "currency_precision": 2,
        "require_commission_for_net": True,
        "entry_execution_id": factories.ENTRY_EXECUTION,
        "exit_execution_ids": [factories.EXIT_EXECUTION],
    }
    fields.update(overrides)
    return PnLCalculator().compute(PnLInputs(**fields))


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------
def test_a_long_call_that_gained_reports_the_gain() -> None:
    """Bought 2 at 6.05, sold 2 at 8.05, multiplier 100: 400.00 gross."""
    result = compute(factories.entry_fills(), factories.exit_fills())

    assert result.status is PnLStatus.COMPLETE
    assert result.entry_cost == Decimal("1210.00")
    assert result.exit_proceeds == Decimal("1610.00")
    assert result.realized_gross_pnl == Decimal("400.00")
    # Two commissions of 1.50 on each side of one leg.
    assert result.total_commission == Decimal("3.00")
    assert result.realized_net_pnl == Decimal("397.00")
    assert result.commission_status is CommissionStatus.KNOWN
    assert result.currency == "EUR"


def test_a_long_call_that_lost_reports_the_loss() -> None:
    result = compute(
        factories.entry_fills(price=Decimal("6.05")),
        factories.exit_fills(price=Decimal("2.05")),
    )

    assert result.realized_gross_pnl == Decimal("-800.00")
    assert result.realized_net_pnl == Decimal("-803.00")
    assert result.is_loss


def test_a_long_put_is_computed_the_same_way() -> None:
    """Direction lives in the fills, not in a formula keyed on the strategy."""
    entry = [
        factories.fill(
            fill_id="fill-entry-put",
            key=factories.PUT_KEY,
            contract_id=factories.PUT_CONTRACT_ID,
            right=OptionRight.PUT,
            price=Decimal("4.00"),
        )
    ]
    closing = [
        factories.fill(
            fill_id="fill-exit-put",
            key=factories.PUT_KEY,
            contract_id=factories.PUT_CONTRACT_ID,
            right=OptionRight.PUT,
            side=OrderSide.SELL,
            price=Decimal("7.00"),
            executed_at=EXIT_AT,
            execution_id=factories.EXIT_EXECUTION,
        )
    ]
    result = compute(entry, closing, strategy=StrategyType.LONG_PUT)

    assert result.realized_gross_pnl == Decimal("600.00")


def test_the_return_is_over_the_cost_actually_paid() -> None:
    result = compute(factories.entry_fills(), factories.exit_fills())

    # 397.00 net over 1,210.00 cost.
    assert result.return_pct is not None
    assert round(result.return_pct, 2) == pytest.approx(32.81, abs=0.01)


# ---------------------------------------------------------------------------
# A structure is one trade
# ---------------------------------------------------------------------------
def test_a_straddle_reports_one_result_over_both_legs() -> None:
    """The call's gain and the put's loss are one number, not two trades."""
    entry, closing = factories.straddle_fills()
    result = compute(entry, closing, strategy=StrategyType.LONG_STRADDLE)

    assert result.status is PnLStatus.COMPLETE
    assert len(result.legs) == 2
    # Call: 600 -> 900 is +300. Put: 500 -> 200 is -300. The structure is flat.
    assert result.realized_gross_pnl == Decimal("0.00")
    assert result.entry_cost == Decimal("1100.00")
    assert result.exit_proceeds == Decimal("1100.00")


def test_a_straddle_that_gained_reports_the_structure_total() -> None:
    entry, closing = factories.straddle_fills(call_exit=Decimal("14.00"), put_exit=Decimal("1.00"))
    result = compute(entry, closing, strategy=StrategyType.LONG_STRADDLE)

    # 1,100 in, 1,500 out.
    assert result.realized_gross_pnl == Decimal("400.00")


def test_a_straddle_that_lost_reports_the_structure_total() -> None:
    entry, closing = factories.straddle_fills(call_exit=Decimal("1.00"), put_exit=Decimal("1.00"))
    result = compute(entry, closing, strategy=StrategyType.LONG_STRADDLE)

    assert result.realized_gross_pnl == Decimal("-900.00")
    assert result.is_loss


def test_a_strangle_is_matched_on_its_own_contracts() -> None:
    """Different strikes, so the two legs must not be matched against each other."""
    entry, closing = factories.straddle_fills(call_entry=Decimal("3.00"), put_entry=Decimal("2.50"))
    result = compute(entry, closing, strategy=StrategyType.LONG_STRANGLE)

    keys = {leg.key for leg in result.legs}
    assert keys == {factories.CALL_KEY, factories.PUT_KEY}
    assert all(leg.matched_quantity == Decimal("1") for leg in result.legs)


def test_the_legs_explain_the_total_without_being_trades_of_their_own() -> None:
    entry, closing = factories.straddle_fills()
    result = compute(entry, closing, strategy=StrategyType.LONG_STRADDLE)

    call = next(leg for leg in result.legs if leg.right is OptionRight.CALL)
    put = next(leg for leg in result.legs if leg.right is OptionRight.PUT)
    assert call.gross_pnl == Decimal("300.00")
    assert put.gross_pnl == Decimal("-300.00")
    assert result.realized_gross_pnl == call.gross_pnl + put.gross_pnl


# ---------------------------------------------------------------------------
# Partial closure
# ---------------------------------------------------------------------------
def test_a_partial_close_reports_only_the_matched_units() -> None:
    """Four opened, one closed: the result covers one and claims nothing else."""
    result = compute(
        factories.entry_fills(quantity=4),
        factories.exit_fills(quantity=1),
    )

    assert result.status is PnLStatus.PARTIAL
    assert PnLReasonCode.PARTIALLY_CLOSED in result.reason_codes
    assert result.matched_quantity == Decimal("1")
    assert result.opened_quantity == Decimal("4")
    # One quarter of 4 x 6.05 x 100 = 2,420.00.
    assert result.entry_cost == Decimal("605.00")
    assert result.exit_proceeds == Decimal("805.00")
    assert result.realized_gross_pnl == Decimal("200.00")


def test_a_prorated_entry_cost_says_it_was_prorated() -> None:
    result = compute(factories.entry_fills(quantity=3), factories.exit_fills(quantity=1))

    assert PnLReasonCode.ENTRY_COST_PRORATED in result.reason_codes
    assert result.detail is not None
    assert "prorated" in result.detail


def test_a_partial_close_never_attributes_the_whole_entry_cost() -> None:
    """The failure this guards: reporting a loss the position has not taken."""
    whole = compute(factories.entry_fills(quantity=4), factories.exit_fills(quantity=4))
    part = compute(factories.entry_fills(quantity=4), factories.exit_fills(quantity=1))

    assert whole.entry_cost is not None
    assert part.entry_cost is not None
    assert part.entry_cost < whole.entry_cost


# ---------------------------------------------------------------------------
# NOT_AVAILABLE, and never a plausible number
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("no_entry", PnLReasonCode.ENTRY_FILLS_UNAVAILABLE),
        ("no_exit", PnLReasonCode.EXIT_FILLS_UNAVAILABLE),
        ("no_multiplier", PnLReasonCode.MULTIPLIER_UNAVAILABLE),
        ("currency_mismatch", PnLReasonCode.CURRENCY_MISMATCH),
        ("unknown_execution", PnLReasonCode.EXECUTION_UNKNOWN),
    ],
)
def test_a_missing_input_produces_no_figure_at_all(case: str, reason) -> None:
    entry, closing = factories.entry_fills(), factories.exit_fills()
    overrides: dict[str, Any] = {}
    if case == "no_entry":
        entry = []
    elif case == "no_exit":
        closing = []
    elif case == "no_multiplier":
        entry = factories.entry_fills(multiplier=None)
    elif case == "currency_mismatch":
        closing = factories.exit_fills(currency="USD")
    elif case == "unknown_execution":
        overrides = {"execution_unknown": True}

    result = compute(entry, closing, **overrides)

    assert result.status is PnLStatus.NOT_AVAILABLE
    assert reason in result.reason_codes
    assert result.realized_gross_pnl is None
    assert result.realized_net_pnl is None
    assert result.return_pct is None


def test_a_missing_multiplier_is_never_assumed_to_be_a_hundred() -> None:
    """A standard US equity option is 100 and the first one that is not would
    be mispriced silently, in the figure the loss limit reads."""
    result = compute(factories.entry_fills(multiplier=None), factories.exit_fills())

    assert result.status is PnLStatus.NOT_AVAILABLE
    assert PnLReasonCode.MULTIPLIER_UNAVAILABLE in result.reason_codes


def test_a_cross_currency_pair_is_never_converted() -> None:
    result = compute(factories.entry_fills(), factories.exit_fills(currency="USD"))

    assert result.status is PnLStatus.NOT_AVAILABLE
    assert result.detail is not None
    assert "FX" in result.detail or "rate" in result.detail


def test_a_leg_with_fills_on_only_one_side_is_a_ledger_fault() -> None:
    entry, closing = factories.straddle_fills()
    result = compute(entry, closing[:1], strategy=StrategyType.LONG_STRADDLE)

    assert result.status is PnLStatus.NOT_AVAILABLE
    assert PnLReasonCode.UNMATCHED_LEG in result.reason_codes


# ---------------------------------------------------------------------------
# Commissions
# ---------------------------------------------------------------------------
def test_a_missing_commission_leaves_the_gross_figure_standing() -> None:
    """IBKR reports fills before commission reports. The gross result is real."""
    result = compute(factories.entry_fills(commission=None), factories.exit_fills())

    assert result.status is PnLStatus.COMPLETE
    assert result.realized_gross_pnl == Decimal("400.00")
    assert result.realized_net_pnl is None
    assert result.commission_status is CommissionStatus.PARTIAL
    assert PnLReasonCode.COMMISSION_UNAVAILABLE in result.reason_codes


def test_a_missing_commission_is_never_read_as_zero() -> None:
    """The failure: understating the cost of every trade the feed was slow about."""
    known = compute(factories.entry_fills(), factories.exit_fills())
    unknown = compute(factories.entry_fills(commission=None), factories.exit_fills(commission=None))

    assert known.total_commission == Decimal("3.00")
    assert unknown.total_commission is None
    assert unknown.realized_net_pnl is None


def test_the_best_available_figure_prefers_net_and_falls_back_to_gross() -> None:
    complete = compute(factories.entry_fills(), factories.exit_fills())
    partial = compute(factories.entry_fills(commission=None), factories.exit_fills())

    assert complete.best_available_pnl == complete.realized_net_pnl
    assert partial.best_available_pnl == partial.realized_gross_pnl


# ---------------------------------------------------------------------------
# The trading day
# ---------------------------------------------------------------------------
def test_the_session_is_the_exchange_local_day_not_the_utc_one() -> None:
    """21:30 UTC belongs to the New York session that has just ended."""
    late = datetime(2026, 8, 10, 21, 30, tzinfo=UTC)
    assert session_date_of(late, "America/New_York").isoformat() == "2026-08-10"
    # The same instant in UTC terms is already the 10th, so the distinction
    # only bites after midnight UTC.
    after_midnight = datetime(2026, 8, 11, 0, 30, tzinfo=UTC)
    assert session_date_of(after_midnight, "America/New_York").isoformat() == "2026-08-10"
    assert after_midnight.date().isoformat() == "2026-08-11"


def test_the_result_records_the_session_it_closed_in() -> None:
    result = compute(factories.entry_fills(), factories.exit_fills())

    assert result.session_date is not None
    assert result.session_date.isoformat() == "2026-08-10"
    assert result.opened_at == NOW
    assert result.closed_at == EXIT_AT


# ---------------------------------------------------------------------------
# Determinism and provenance
# ---------------------------------------------------------------------------
def test_the_same_fills_produce_the_same_record() -> None:
    first = compute(factories.entry_fills(), factories.exit_fills())
    second = compute(factories.entry_fills(), factories.exit_fills())

    assert first.pnl_id == second.pnl_id
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_every_broker_fill_id_is_recorded() -> None:
    """The audit trail back to the account, without copying anything out of it."""
    result = compute(factories.entry_fills(), factories.exit_fills())

    assert result.source_fill_ids == ["fill-entry-1", "fill-exit-1"]


def test_a_different_matched_quantity_is_a_different_result() -> None:
    """A position that closes in two tranches produces two genuine results."""
    first = compute(factories.entry_fills(quantity=4), factories.exit_fills(quantity=1))
    second = compute(factories.entry_fills(quantity=4), factories.exit_fills(quantity=4))

    assert first.pnl_id != second.pnl_id
