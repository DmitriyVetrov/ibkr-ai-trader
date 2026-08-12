"""Quantity arithmetic (brief sections 9, 37.2, 37.3).

Quantity is introduced for the first time in Milestone 7, and there is exactly
one rule that matters: **it is the floor of the tightest ceiling, it is a whole
number, and it never rounds up.** A system that rounded 2.9 contracts to 3
would commit 3% more capital than any limit authorised, every time, silently.

The boundary tests here are deliberately mean — exact fits, and one cent either
side of an exact fit — because that is where a rounding bug lives. A test at
"roughly half the budget" would pass with almost any implementation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.allocation.budget_allocator import max_units
from trading_system.domain.enums import (
    AllocationOutcome,
    AllocationReason,
    Direction,
    RiskReasonCode,
    StrategyType,
)

pytestmark = pytest.mark.unit


def _unrelated(make_reservation, committed: str):
    """A held reservation that consumes budget and nothing else.

    A different underlying, strategy and direction, so a test about the budget
    ceiling is not quietly a test about concentration.
    """
    return make_reservation(
        symbol="AAPL",
        strategy=StrategyType.LONG_STRADDLE,
        direction=Direction.UNCERTAIN,
        capital_committed=Decimal(committed),
        max_loss=Decimal("0"),
    )


# ---------------------------------------------------------------------------
# max_units: the primitive
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("limit", "unit", "expected"),
    [
        ("0", "605.00", 0),
        ("604.99", "605.00", 0),
        ("605.00", "605.00", 1),
        ("605.01", "605.00", 1),
        ("1209.99", "605.00", 1),
        ("1210.00", "605.00", 2),
        ("4000.00", "605.00", 6),
        ("5000.00", "0.01", 500_000),
        ("1.00", "0.30", 3),
        ("999.99", "999.99", 1),
        ("999.98", "999.99", 0),
    ],
)
def test_max_units_is_an_exact_floor(limit: str, unit: str, expected: int) -> None:
    assert max_units(Decimal(limit), Decimal(unit)) == expected


@pytest.mark.parametrize("cost", ["0.01", "0.10", "0.30", "999.99", "5000.00"])
def test_an_exact_fit_yields_exactly_one_unit(cost: str) -> None:
    """The boundary the brief names: exactly the budget buys exactly one."""
    assert max_units(Decimal(cost), Decimal(cost)) == 1


@pytest.mark.parametrize("cost", ["0.01", "0.10", "0.30", "999.99", "5000.00"])
def test_one_cent_short_yields_nothing(cost: str) -> None:
    limit = Decimal(cost) - Decimal("0.01")

    assert max_units(limit, Decimal(cost)) == 0


def test_max_units_never_rounds_up() -> None:
    """The property, over a range rather than at a chosen point."""
    unit = Decimal("3.33")
    for cents in range(0, 2000):
        limit = Decimal(cents) / Decimal(100)
        units = max_units(limit, unit)
        assert Decimal(units) * unit <= limit, f"{units} x {unit} exceeds {limit}"
        assert Decimal(units + 1) * unit > limit, f"one more unit would still fit at {limit}"


def test_a_zero_unit_cost_yields_nothing_rather_than_dividing_by_zero() -> None:
    """A structure that costs nothing is a data fault the guards already refused."""
    assert max_units(Decimal("5000"), Decimal("0")) == 0


def test_a_negative_unit_cost_yields_nothing() -> None:
    assert max_units(Decimal("5000"), Decimal("-1")) == 0


def test_a_negative_limit_yields_nothing() -> None:
    """An over-committed campaign buys nothing; it does not buy a negative amount."""
    assert max_units(Decimal("-100"), Decimal("605.00")) == 0


def test_the_result_is_always_an_int() -> None:
    """There is no such thing as a third of an option contract."""
    units = max_units(Decimal("1000"), Decimal("3.33"))

    assert isinstance(units, int)
    assert units == 300


# ---------------------------------------------------------------------------
# Quantity through the engine
# ---------------------------------------------------------------------------
def test_a_candidate_too_large_for_one_unit_is_rejected_by_name(allocate, priced):
    """Not "quantity zero": the limit that bound is named.

    A refusal reported as a computed zero tells nobody which limit to look at.
    The risk engine tests one whole unit against every ceiling precisely so
    this comes back as MAX_ALLOCATION_PER_TRADE_EXCEEDED rather than as an
    arithmetic result.
    """
    [decision] = allocate([priced("605.00")], max_allocation_per_trade=Decimal("100.00"))

    assert decision.outcome is AllocationOutcome.REJECTED
    assert decision.quantity == 0
    assert decision.capital_committed == Decimal("0")
    assert RiskReasonCode.MAX_ALLOCATION_PER_TRADE_EXCEEDED in decision.evaluation.reason_codes


def test_a_risk_approved_candidate_always_sizes_to_at_least_one_contract(allocate, priced):
    """The invariant that makes a bare zero unreachable.

    Every ceiling the allocation engine divides by is also checked against one
    whole unit by the risk engine, so an approval implies at least one unit
    fits. The engine's zero-quantity branch is therefore defence in depth: it
    turns an impossible state into NO_TRADE rather than into a position of an
    unspecified size.
    """
    for cost in ("0.30", "1.00", "605.00", "1499.99"):
        [decision] = allocate([priced(cost)], min_allocation_per_trade=Decimal("0"))
        if decision.outcome is AllocationOutcome.APPROVED:
            assert decision.quantity >= 1, f"{cost} was approved but sized to nothing"


def test_quantity_one(allocate, priced):
    [decision] = allocate([priced("605.00")], max_allocation_per_trade=Decimal("700.00"))

    assert decision.outcome is AllocationOutcome.APPROVED
    assert decision.quantity == 1
    assert decision.capital_committed == Decimal("605.00")


def test_multiple_contracts(allocate, priced):
    [decision] = allocate(
        [priced("605.00")],
        max_allocation_per_trade=Decimal("2000.00"),
        max_risk_per_trade=Decimal("2000.00"),
        max_underlying_concentration_pct=40.0,
    )

    assert decision.quantity == 3
    assert decision.capital_committed == Decimal("1815.00")


def test_the_exact_budget_boundary_is_funded(allocate, priced, make_campaign, make_reservation):
    """4,000 allocatable, 3,395 committed, 605 left: exactly one more unit.

    The held reservation is a different underlying, a different strategy and a
    different direction, so the only ceiling in play is the campaign budget.
    """
    campaign = make_campaign(open_positions=[_unrelated(make_reservation, "3395.00")])

    [decision] = allocate([priced("605.00")], campaign=campaign)

    assert decision.outcome is AllocationOutcome.APPROVED
    assert decision.quantity == 1
    assert decision.calculation is not None
    assert decision.calculation.binding_constraint is AllocationReason.LIMITED_BY_BUDGET


def test_the_budget_exceeded_by_one_cent_funds_nothing(
    allocate, priced, make_campaign, make_reservation
):
    """3,395.01 committed leaves 604.99 — one cent short of a single unit."""
    campaign = make_campaign(open_positions=[_unrelated(make_reservation, "3395.01")])

    [decision] = allocate([priced("605.00")], campaign=campaign)

    assert decision.outcome is AllocationOutcome.REJECTED
    assert RiskReasonCode.INSUFFICIENT_CAMPAIGN_BUDGET in decision.evaluation.reason_codes


def test_the_exact_risk_boundary_is_funded(allocate, priced):
    """Max loss is the debit, so a 1,210 risk cap funds exactly two units."""
    [decision] = allocate(
        [priced("605.00")],
        max_risk_per_trade=Decimal("1210.00"),
        max_allocation_per_trade=Decimal("5000.00"),
    )

    assert decision.quantity == 2
    assert decision.total_max_loss == Decimal("1210.00")


def test_the_risk_boundary_exceeded_by_one_cent_funds_one_fewer(allocate, priced):
    [decision] = allocate(
        [priced("605.00")],
        max_risk_per_trade=Decimal("1209.99"),
        max_allocation_per_trade=Decimal("5000.00"),
    )

    assert decision.quantity == 1


def test_the_exact_concentration_boundary_is_funded(allocate, priced):
    """30% of 5,000 is 1,500, which funds two units at 605 and not three."""
    [decision] = allocate(
        [priced("605.00")],
        max_allocation_per_trade=Decimal("5000.00"),
        max_risk_per_trade=Decimal("5000.00"),
    )

    assert decision.quantity == 2
    assert decision.calculation is not None
    assert decision.calculation.units_by_underlying_concentration == 2, "1500 / 605 floors to 2"


def test_the_contract_cap_bounds_a_very_cheap_option(allocate, priced):
    """A cheap contract must not buy a position nobody intended."""
    [decision] = allocate(
        [priced("1.00")],
        max_contracts_per_trade=20,
        min_allocation_per_trade=Decimal("0"),
    )

    assert decision.quantity == 20
    assert decision.calculation is not None
    assert decision.calculation.binding_constraint is AllocationReason.LIMITED_BY_CONTRACT_CAP
    assert AllocationReason.FULL_ALLOCATION in decision.reasons


def test_buying_power_can_bind_before_the_campaign_does(allocate, priced, make_account):
    account = make_account(
        cash=Decimal("700.00"), available_funds=Decimal("700.00"), buying_power=Decimal("700.00")
    )

    [decision] = allocate(
        [priced("605.00")],
        account=account,
        max_allocation_per_trade=Decimal("5000.00"),
        max_risk_per_trade=Decimal("5000.00"),
    )

    assert decision.quantity == 1
    assert decision.calculation is not None
    assert decision.calculation.units_by_buying_power == 1
    assert decision.calculation.binding_constraint is AllocationReason.LIMITED_BY_BUYING_POWER


def test_an_unknown_broker_balance_constrains_nothing_here(allocate, priced, make_account):
    """The guard already refuses it; the calculation records the absence honestly."""
    account = make_account(cash=None, buying_power=None, available_funds=None)

    [decision] = allocate([priced("605.00")], account=account)

    assert decision.outcome is AllocationOutcome.REJECTED
    assert RiskReasonCode.INVALID_ACCOUNT_SNAPSHOT in decision.evaluation.reason_codes


def test_a_position_below_the_minimum_allocation_is_not_taken(allocate, priced):
    """A position too small to be worth holding is not held."""
    [decision] = allocate(
        [priced("1.00")],
        max_contracts_per_trade=1,
        min_allocation_per_trade=Decimal("250"),
    )

    assert decision.outcome is AllocationOutcome.NO_TRADE
    assert decision.quantity == 0
    assert "below the" in (decision.detail or "")


def test_the_calculation_records_every_ceiling_it_considered(allocate, priced):
    """'Why two and not three' must be answerable from the record alone."""
    [decision] = allocate([priced("605.00")])
    calculation = decision.calculation

    assert calculation is not None
    assert calculation.units_by_budget == 6
    assert calculation.units_by_risk == 2
    assert calculation.units_by_trade_cap == 2
    assert calculation.units_by_underlying_concentration == 2
    assert calculation.units_by_strategy_concentration == 4
    assert calculation.units_by_directional_exposure == 5
    assert calculation.units_by_contract_cap == 20
    assert calculation.quantity == min(
        calculation.units_by_budget,
        calculation.units_by_risk,
        calculation.units_by_trade_cap,
        calculation.units_by_underlying_concentration,
        calculation.units_by_strategy_concentration,
        calculation.units_by_directional_exposure,
        calculation.units_by_contract_cap,
    )


def test_a_calculation_claiming_more_than_its_own_ceilings_cannot_be_built():
    """The model refuses a quantity that no ceiling authorised."""
    from trading_system.allocation.models import QuantityCalculation
    from trading_system.domain.enums import MaxLossBasis

    with pytest.raises(ValueError, match="not the minimum of its own ceilings"):
        QuantityCalculation(
            quantity=5,
            unit_cost=Decimal("100"),
            unit_max_loss=Decimal("100"),
            max_loss_basis=MaxLossBasis.NET_DEBIT_PAID,
            units_by_budget=2,
            units_by_risk=5,
            units_by_trade_cap=5,
            units_by_underlying_concentration=5,
            units_by_strategy_concentration=5,
            units_by_directional_exposure=5,
            units_by_contract_cap=5,
            binding_constraint=AllocationReason.LIMITED_BY_BUDGET,
        )


# ---------------------------------------------------------------------------
# Money safety
# ---------------------------------------------------------------------------
def test_every_monetary_result_is_a_decimal(allocate, priced):
    [decision] = allocate([priced("605.00")])

    assert isinstance(decision.capital_committed, Decimal)
    assert isinstance(decision.total_max_loss, Decimal)
    assert decision.calculation is not None
    assert isinstance(decision.calculation.unit_cost, Decimal)


def test_repeated_cent_arithmetic_stays_exact(allocate, priced):
    """0.1 + 0.2 is the canonical binary-float failure. Decimal has no such bug."""
    [decision] = allocate(
        [priced("0.30")],
        max_contracts_per_trade=3,
        min_allocation_per_trade=Decimal("0"),
    )

    assert decision.quantity == 3
    assert decision.capital_committed == Decimal("0.90")
    assert str(decision.capital_committed) == "0.90"
