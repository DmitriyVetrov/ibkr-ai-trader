"""Deterministic execution preconditions (brief sections 42.1 and 67).

Everything checkable without touching a broker is checked before a connection
is opened — partly for clarity, mostly because Milestone 2 established that the
execution path must not spend its one reliable round trip discovering something
it already knew.

Every test here asserts a *named* reason code rather than merely that something
was refused. "It said no" is not a diagnosis, and an operator whose order did
not go out needs to know which of fourteen things to fix.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.data.market_calendar import MarketCalendar
from trading_system.domain.enums import (
    AllocationOutcome,
    ExecutionReasonCode,
    LegAction,
    OrderType,
    RiskOutcome,
    RiskReasonCode,
    TradingMode,
)
from trading_system.execution.validation import ExecutionValidator

from .conftest import NOW

pytestmark = pytest.mark.unit


@pytest.fixture
def validator(execution_disabled_config) -> ExecutionValidator:
    """Validation with execution switched OFF, which is how the system ships."""
    return ExecutionValidator(
        config=execution_disabled_config.execution,
        campaign=execution_disabled_config.campaign,
        calendar=MarketCalendar(execution_disabled_config.data.market_calendar),
    )


@pytest.fixture
def check(validator):
    def _check(allocation, **overrides):
        kwargs = {
            "execution_now": NOW,
            "trading_mode": TradingMode.PAPER,
            "order_type": OrderType.LIMIT,
            # The validator under test has execution.enabled=false, so a
            # validation of a submission would always fail on that alone. Most
            # tests here are about the *other* checks, so they validate as a dry
            # run and the enablement switch gets its own test below.
            "dry_run": True,
        }
        kwargs.update(overrides)
        return validator.validate(allocation, **kwargs)

    return _check


def _codes(validation) -> set[ExecutionReasonCode]:
    return set(validation.reason_codes)


# ---------------------------------------------------------------------------
# The authorisation itself
# ---------------------------------------------------------------------------
def test_an_approved_allocation_passes(check, approved_allocation) -> None:
    assert check(approved_allocation).ok


def test_a_rejected_allocation_is_refused(check, approved_allocation) -> None:
    rejected = approved_allocation.model_copy(
        update={"outcome": AllocationOutcome.REJECTED, "risk_outcome": RiskOutcome.REJECTED}
    )
    assert ExecutionReasonCode.ALLOCATION_NOT_APPROVED in _codes(check(rejected))


def test_a_no_trade_allocation_is_refused(check, approved_allocation) -> None:
    no_trade = approved_allocation.model_copy(update={"outcome": AllocationOutcome.NO_TRADE})
    assert ExecutionReasonCode.ALLOCATION_NOT_APPROVED in _codes(check(no_trade))


def test_an_already_allocated_authorisation_is_refused(check, approved_allocation) -> None:
    """Not a near miss: it means the capital is already reserved elsewhere."""
    duplicate = approved_allocation.model_copy(
        update={
            "outcome": AllocationOutcome.ALREADY_ALLOCATED,
            "risk_outcome": RiskOutcome.REJECTED,
            "reason_codes": [RiskReasonCode.DUPLICATE_OPPORTUNITY],
        }
    )
    assert ExecutionReasonCode.ALLOCATION_NOT_APPROVED in _codes(check(duplicate))


def test_a_dry_run_authorisation_is_refused(check, approved_allocation) -> None:
    diagnostic = approved_allocation.model_copy(update={"dry_run": True})
    assert ExecutionReasonCode.ALLOCATION_IS_DRY_RUN in _codes(check(diagnostic))


def test_submission_is_refused_while_execution_is_disabled(check, approved_allocation) -> None:
    """The shipped configuration ships OFF, and that is the point."""
    validation = check(approved_allocation, dry_run=False)
    assert ExecutionReasonCode.EXECUTION_DISABLED in _codes(validation)


def test_a_dry_run_is_permitted_while_execution_is_disabled(check, approved_allocation) -> None:
    """Which is what makes the switch reviewable rather than merely restrictive."""
    assert check(approved_allocation, dry_run=True).ok


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------
def test_live_mode_is_refused(check, approved_allocation) -> None:
    validation = check(approved_allocation, trading_mode=TradingMode.LIVE)
    assert ExecutionReasonCode.LIVE_GUARD_FAILED in _codes(validation)


def test_dry_run_mode_cannot_submit(check, approved_allocation) -> None:
    validation = check(approved_allocation, trading_mode=TradingMode.DRY_RUN, dry_run=False)
    assert ExecutionReasonCode.PAPER_MODE_REQUIRED in _codes(validation)


def test_a_market_order_is_not_a_permitted_type(check, approved_allocation) -> None:
    validation = check(approved_allocation, order_type=OrderType.MARKET)
    assert ExecutionReasonCode.ORDER_TYPE_NOT_PERMITTED in _codes(validation)


# ---------------------------------------------------------------------------
# Quantity, contract, structure
# ---------------------------------------------------------------------------
def test_a_zero_quantity_is_refused(check, approved_allocation) -> None:
    validation = check(approved_allocation.model_copy(update={"quantity": 0}))
    assert ExecutionReasonCode.INVALID_QUANTITY in _codes(validation)


def test_a_negative_quantity_is_refused(check, approved_allocation) -> None:
    validation = check(approved_allocation.model_copy(update={"quantity": -1}))
    assert ExecutionReasonCode.INVALID_QUANTITY in _codes(validation)


def test_a_missing_contract_id_is_refused(check, approved_allocation) -> None:
    legs = [approved_allocation.legs[0].model_copy(update={"contract_id": 0})]
    validation = check(approved_allocation.model_copy(update={"legs": legs}))
    assert ExecutionReasonCode.CONTRACT_ID_MISSING in _codes(validation)


def test_a_missing_multiplier_is_refused(check, approved_allocation) -> None:
    """100 is common for US equity options and is not a default anything assumes."""
    legs = [approved_allocation.legs[0].model_copy(update={"multiplier": 0})]
    validation = check(approved_allocation.model_copy(update={"legs": legs}))
    assert ExecutionReasonCode.MULTIPLIER_MISSING in _codes(validation)


def test_malformed_legs_are_refused(check, approved_allocation) -> None:
    validation = check(approved_allocation.model_copy(update={"legs": []}))
    assert ExecutionReasonCode.CONTRACT_INVALID in _codes(validation)


def test_a_short_leg_is_refused(check, approved_allocation) -> None:
    legs = [approved_allocation.legs[0].model_copy(update={"action": LegAction.SELL})]
    validation = check(approved_allocation.model_copy(update={"legs": legs}))
    assert ExecutionReasonCode.SHORT_LEG_NOT_SUPPORTED in _codes(validation)


def test_a_leg_on_another_underlying_is_refused(check, approved_allocation) -> None:
    legs = [approved_allocation.legs[0].model_copy(update={"underlying": "AAPL"})]
    validation = check(approved_allocation.model_copy(update={"legs": legs}))
    assert ExecutionReasonCode.CONTRACT_INVALID in _codes(validation)


def test_a_repeated_contract_id_is_refused(check, approved_allocation) -> None:
    """Two legs of one strategy are two different contracts."""
    leg = approved_allocation.legs[0]
    legs = [leg, leg.model_copy(update={"leg_index": 1})]
    validation = check(approved_allocation.model_copy(update={"legs": legs}))
    assert ExecutionReasonCode.CONTRACT_INVALID in _codes(validation)


def test_legs_spanning_expirations_are_refused(check, approved_allocation) -> None:
    leg = approved_allocation.legs[0]
    other = leg.model_copy(
        update={
            "leg_index": 1,
            "contract_id": leg.contract_id + 1,
            "expiration": leg.expiration + timedelta(days=7),
        }
    )
    validation = check(approved_allocation.model_copy(update={"legs": [leg, other]}))
    assert ExecutionReasonCode.MULTI_LEG_UNSUPPORTED in _codes(validation)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def test_a_usd_contract_against_a_eur_campaign_is_refused(check, approved_allocation) -> None:
    """Milestone 7's refusal, preserved rather than undone.

    Converting at an invented rate would size a position wrongly by an amount
    nobody recorded.
    """
    legs = [approved_allocation.legs[0].model_copy(update={"currency": "USD"})]
    validation = check(approved_allocation.model_copy(update={"legs": legs, "currency": "USD"}))
    assert ExecutionReasonCode.CURRENCY_MISMATCH in _codes(validation)


def test_a_missing_reference_price_is_refused(check, approved_allocation) -> None:
    """No price means no limit price can be derived. None is invented."""
    validation = check(approved_allocation.model_copy(update={"unit_cost": None}))
    assert ExecutionReasonCode.PRICE_UNAVAILABLE in _codes(validation)


def test_a_zero_reference_price_is_refused(check, approved_allocation) -> None:
    validation = check(approved_allocation.model_copy(update={"unit_cost": Decimal("0")}))
    assert ExecutionReasonCode.INVALID_PRICE in _codes(validation)


# ---------------------------------------------------------------------------
# Price drift
# ---------------------------------------------------------------------------
def test_a_price_within_the_drift_ceiling_passes(check, approved_allocation) -> None:
    assert check(approved_allocation, observed_unit_price=Decimal("610.00")).ok


def test_a_price_beyond_the_drift_ceiling_is_refused(check, approved_allocation) -> None:
    """Execution does not chase the market: a changed trade needs a new authorisation."""
    validation = check(approved_allocation, observed_unit_price=Decimal("700.00"))
    assert ExecutionReasonCode.PRICE_DRIFT in _codes(validation)


def test_drift_is_only_checked_against_an_explicitly_supplied_price(
    check, approved_allocation
) -> None:
    """M8 never fetches its own quote before submitting.

    A second uncached round trip on the submission's connection is exactly what
    Milestone 2 found to be unreliable, so there is no silent comparison.
    """
    assert check(approved_allocation, observed_unit_price=None).ok


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def test_an_expired_authorisation_is_refused(check, approved_allocation, system_config) -> None:
    minutes = system_config.execution.allocation_validity_minutes
    later = NOW + timedelta(minutes=minutes + 1)

    validation = check(approved_allocation, execution_now=later)
    assert ExecutionReasonCode.EXECUTION_WINDOW_EXPIRED in _codes(validation)


def test_the_window_is_not_silently_extended(check, approved_allocation, system_config) -> None:
    """A boundary test at the exact edge and one minute past it."""
    minutes = system_config.execution.allocation_validity_minutes
    at_edge = check(approved_allocation, execution_now=NOW + timedelta(minutes=minutes))
    past_edge = check(
        approved_allocation, execution_now=NOW + timedelta(minutes=minutes, seconds=1)
    )

    assert ExecutionReasonCode.EXECUTION_WINDOW_EXPIRED not in _codes(at_edge)
    assert ExecutionReasonCode.EXECUTION_WINDOW_EXPIRED in _codes(past_edge)


def test_a_stale_price_reference_is_refused(check, approved_allocation, system_config) -> None:
    """Allocation validity and price validity are two different clocks."""
    seconds = system_config.execution.price_validity_seconds
    stale = approved_allocation.model_copy(
        update={
            "legs": [
                leg.model_copy(update={"quote_as_of": NOW - timedelta(seconds=seconds + 60)})
                for leg in approved_allocation.legs
            ]
        }
    )
    validation = check(stale)
    assert ExecutionReasonCode.PRICE_REFERENCE_STALE in _codes(validation)


def test_a_future_authorisation_is_a_point_in_time_error(check, approved_allocation) -> None:
    """A correctness bug, never a market outcome."""
    validation = check(approved_allocation, execution_now=NOW - timedelta(minutes=5))
    assert ExecutionReasonCode.POINT_IN_TIME_ERROR in _codes(validation)


def test_a_future_quote_is_a_point_in_time_error(check, approved_allocation) -> None:
    """A future price would otherwise read as the freshest possible one."""
    ahead = approved_allocation.model_copy(
        update={
            "legs": [
                leg.model_copy(update={"quote_as_of": NOW + timedelta(hours=1)})
                for leg in approved_allocation.legs
            ]
        }
    )
    validation = check(ahead)
    assert ExecutionReasonCode.POINT_IN_TIME_ERROR in _codes(validation)


def test_staleness_is_judged_on_the_stalest_leg(check, approved_allocation, system_config) -> None:
    """A structure is only as fresh as its weakest link."""
    seconds = system_config.execution.price_validity_seconds
    legs = list(approved_allocation.legs)
    fresh_and_stale = approved_allocation.model_copy(
        update={
            "legs": [
                legs[0].model_copy(update={"quote_as_of": NOW}),
                legs[0].model_copy(
                    update={
                        "leg_index": 1,
                        "contract_id": legs[0].contract_id + 1,
                        "quote_as_of": NOW - timedelta(seconds=seconds + 60),
                    }
                ),
            ]
        }
    )
    assert ExecutionReasonCode.PRICE_REFERENCE_STALE in _codes(check(fresh_and_stale))


# ---------------------------------------------------------------------------
# Market session
# ---------------------------------------------------------------------------
def test_a_closed_market_refuses_before_the_broker_is_called(check, approved_allocation) -> None:
    """Determined from the transcribed calendar, never invented."""
    from datetime import UTC, datetime

    saturday = datetime(2026, 8, 15, 14, 30, tzinfo=UTC)
    stale_free = approved_allocation.model_copy(update={"decided_at": saturday, "as_of": saturday})
    validation = check(stale_free, execution_now=saturday)
    assert ExecutionReasonCode.MARKET_CLOSED in _codes(validation)


def test_outside_regular_hours_is_closed(check, approved_allocation) -> None:
    from datetime import UTC, datetime

    before_open = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
    early = approved_allocation.model_copy(update={"decided_at": before_open, "as_of": before_open})
    assert ExecutionReasonCode.MARKET_CLOSED in _codes(check(early, execution_now=before_open))


def test_a_year_the_calendar_does_not_cover_blocks(system_config, approved_allocation) -> None:
    """`block_on_unknown_session` decides whether an unknown session blocks a trade."""
    from datetime import UTC, datetime

    validator = ExecutionValidator(
        config=system_config.execution,
        campaign=system_config.campaign,
        calendar=MarketCalendar(system_config.data.market_calendar),
    )
    beyond = datetime(2031, 8, 12, 14, 30, tzinfo=UTC)
    allocation = approved_allocation.model_copy(update={"decided_at": beyond, "as_of": beyond})

    validation = validator.validate(
        allocation,
        execution_now=beyond,
        trading_mode=TradingMode.PAPER,
        order_type=OrderType.LIMIT,
        dry_run=True,
    )
    assert ExecutionReasonCode.MARKET_CLOSED in _codes(validation)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_every_failure_is_collected_not_just_the_first(check, approved_allocation) -> None:
    """An operator fixing a refused execution wants the whole list."""
    broken = approved_allocation.model_copy(
        update={
            "quantity": 0,
            "unit_cost": None,
            "legs": [approved_allocation.legs[0].model_copy(update={"currency": "USD"})],
        }
    )
    codes = _codes(check(broken))

    assert ExecutionReasonCode.INVALID_QUANTITY in codes
    assert ExecutionReasonCode.PRICE_UNAVAILABLE in codes
    assert ExecutionReasonCode.CURRENCY_MISMATCH in codes


def test_the_validator_never_repairs_what_it_refuses(check, approved_allocation) -> None:
    """It validates; it has no way to change what would be sent."""
    before = approved_allocation.model_dump()
    check(approved_allocation.model_copy(update={"quantity": 0}))
    assert approved_allocation.model_dump() == before


def test_a_naive_execution_instant_is_refused(validator, approved_allocation) -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone-aware"):
        validator.validate(
            approved_allocation,
            execution_now=datetime(2026, 8, 10, 14, 30),
            trading_mode=TradingMode.PAPER,
            order_type=OrderType.LIMIT,
            dry_run=True,
        )
