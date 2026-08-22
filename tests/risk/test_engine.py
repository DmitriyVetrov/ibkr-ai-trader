"""The risk engine's verdicts (brief section 37.1).

One claim per test, and each of them is a claim about *permission* rather than
about size: the engine decides whether a position may be taken at all, and a
candidate can be approved here and still receive nothing at allocation.

The critical invariant the whole architecture rests on is asserted at the
bottom of this file: **no AI agent can override a risk rejection.** It is
checked structurally — there is no field, argument or method through which one
could — rather than by asserting that a particular prompt does not try.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    DataQuality,
    Direction,
    MaxLossBasis,
    RiskCheckOutcome,
    RiskOutcome,
    RiskReasonCode,
    StrategyType,
    TradingMode,
)
from trading_system.fx.models import FxRateTable
from trading_system.risk.engine import RiskEngine
from trading_system.risk.limits import resolve_limits
from trading_system.risk.models import RiskEvaluation

from .conftest import NOW, eur_usd_rates

pytestmark = pytest.mark.unit


def _evaluate(limits, candidate, campaign, **kwargs) -> RiskEvaluation:
    return RiskEngine(limits).evaluate(candidate, campaign, as_of=NOW, **kwargs)


def _codes(evaluation: RiskEvaluation) -> set[RiskReasonCode]:
    return set(evaluation.reason_codes)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_valid_candidate_is_approved(risk_limits, make_candidate, make_campaign, make_account):
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())

    assert evaluation.outcome is RiskOutcome.APPROVED
    assert evaluation.reason_codes == [RiskReasonCode.OK]
    assert evaluation.unit_cost == Decimal("605.00")
    assert evaluation.unit_max_loss == Decimal("605.00")
    assert evaluation.max_loss_basis is MaxLossBasis.NET_DEBIT_PAID


def test_an_approval_contradicting_its_own_checks_cannot_be_constructed(
    risk_limits, make_candidate, make_campaign, make_account
):
    """The model refuses an APPROVED verdict whose checks failed."""
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())
    failed = evaluation.checks[0].model_copy(
        update={
            "outcome": RiskCheckOutcome.FAIL,
            "reason_code": RiskReasonCode.SPREAD_TOO_WIDE,
        }
    )

    with pytest.raises(ValueError, match="contradicts its own checks"):
        evaluation.model_copy(update={"checks": [failed]}).model_validate(
            evaluation.model_copy(update={"checks": [failed]}).model_dump()
        )


def test_the_engine_holds_no_state_between_candidates(
    risk_limits, make_candidate, make_campaign, make_account
):
    engine = RiskEngine(risk_limits)
    campaign, account = make_campaign(), make_account()

    first = engine.evaluate(make_candidate(), campaign, as_of=NOW, account=account)
    second = engine.evaluate(make_candidate(), campaign, as_of=NOW, account=account)

    assert first.model_dump() == second.model_dump()


# ---------------------------------------------------------------------------
# Capital and risk
# ---------------------------------------------------------------------------
def test_insufficient_campaign_budget_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    """A campaign with 4,000 allocatable and 3,800 committed cannot buy at 605."""
    campaign = make_campaign(
        open_positions=[
            make_reservation(capital_committed=Decimal("3800.00"), max_loss=Decimal("100.00"))
        ]
    )

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert evaluation.outcome is RiskOutcome.REJECTED
    assert RiskReasonCode.INSUFFICIENT_CAMPAIGN_BUDGET in _codes(evaluation)


def test_the_reserve_is_never_spendable(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    """USD 4,400 of a USD 5,500 envelope is allocatable; the last 1,100 is not.

    The figures are the shipped EUR 5,000 / EUR 1,000 converted at the suite's
    test rate. They are stated in the traded currency because that is what a
    reservation costs and what the limit is compared against.
    """
    campaign = make_campaign(
        open_positions=[
            make_reservation(capital_committed=Decimal("4400.00"), max_loss=Decimal("0"))
        ]
    )

    assert campaign.available == Decimal("0")
    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert RiskReasonCode.INSUFFICIENT_CAMPAIGN_BUDGET in _codes(evaluation)


def test_max_allocation_per_trade_is_enforced_on_a_single_unit(
    risk_limits, make_candidate, make_campaign, make_account, make_leg
):
    # The ceiling is EUR 1,500 declared, USD 1,650 converted. One unit at
    # USD 1,700 is over it.
    expensive = make_candidate(
        price_overrides={"unit_cost": Decimal("1700.00")},
        legs=[make_leg(bid=Decimal("16.95"), ask=Decimal("17.00"))],
    )

    evaluation = _evaluate(risk_limits, expensive, make_campaign(), account=make_account())

    assert RiskReasonCode.MAX_ALLOCATION_PER_TRADE_EXCEEDED in _codes(evaluation)


def test_max_risk_per_trade_is_enforced(
    risk_limits, make_candidate, make_campaign, make_account, make_leg
):
    """Max loss is the debit, so an over-large debit breaches the risk cap too."""
    evaluation = _evaluate(
        risk_limits,
        make_candidate(price_overrides={"unit_cost": Decimal("1700.00")}),
        make_campaign(),
        account=make_account(),
    )

    assert RiskReasonCode.MAX_RISK_PER_TRADE_EXCEEDED in _codes(evaluation)


def test_max_total_open_risk_is_enforced(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    campaign = make_campaign(
        open_positions=[
            make_reservation(capital_committed=Decimal("100.00"), max_loss=Decimal("3900.00"))
        ]
    )

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert RiskReasonCode.MAX_TOTAL_OPEN_RISK_EXCEEDED in _codes(evaluation)


def test_insufficient_broker_buying_power_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account
):
    """The most restrictive limit wins, and here it is the account.

    The campaign has 4,000 available; the broker says 500. A campaign envelope
    is not permission to spend money the account does not have.
    """
    account = make_account(
        cash=Decimal("500.00"), available_funds=Decimal("500.00"), buying_power=Decimal("500.00")
    )

    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=account)

    assert RiskReasonCode.INSUFFICIENT_BUYING_POWER in _codes(evaluation)


def test_a_large_broker_balance_does_not_widen_the_campaign(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    """A million-euro paper account cannot unlock a committed campaign."""
    campaign = make_campaign(
        open_positions=[make_reservation(capital_committed=Decimal("4000.00"))]
    )
    account = make_account(
        cash=Decimal("1000000.00"),
        available_funds=Decimal("1000000.00"),
        buying_power=Decimal("4000000.00"),
    )

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=account)

    assert RiskReasonCode.INSUFFICIENT_CAMPAIGN_BUDGET in _codes(evaluation)


# ---------------------------------------------------------------------------
# Concentration and position counts
# ---------------------------------------------------------------------------
def test_underlying_concentration_is_enforced(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    """NVDA may hold 30% of 5,000 = 1,500 and already holds 1,400."""
    campaign = make_campaign(
        open_positions=[
            make_reservation(
                symbol="NVDA",
                opportunity_id="opportunity-nvda-held",
                capital_committed=Decimal("1400.00"),
                max_loss=Decimal("1400.00"),
                strategy=StrategyType.LONG_STRADDLE,
            )
        ]
    )
    limits = risk_limits.model_copy(update={"max_positions_per_underlying": 2})

    evaluation = _evaluate(limits, make_candidate(), campaign, account=make_account())

    assert RiskReasonCode.UNDERLYING_CONCENTRATION_EXCEEDED in _codes(evaluation)


def test_strategy_concentration_is_enforced(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    """LONG_CALL may hold 50% of 5,000 = 2,500 and already holds 2,400."""
    campaign = make_campaign(
        open_positions=[
            make_reservation(
                symbol="AAPL",
                capital_committed=Decimal("2400.00"),
                max_loss=Decimal("100.00"),
                strategy=StrategyType.LONG_CALL,
            )
        ]
    )

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert RiskReasonCode.STRATEGY_CONCENTRATION_EXCEEDED in _codes(evaluation)


def test_directional_exposure_is_enforced(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    """BULLISH exposure may reach 70% of 5,000 = 3,500 and already stands at 3,400."""
    campaign = make_campaign(
        open_positions=[
            make_reservation(
                symbol="AAPL",
                capital_committed=Decimal("3400.00"),
                max_loss=Decimal("100.00"),
                strategy=StrategyType.LONG_PUT,
                direction=Direction.BULLISH,
            )
        ]
    )

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert RiskReasonCode.DIRECTIONAL_EXPOSURE_EXCEEDED in _codes(evaluation)


def test_max_open_positions_is_enforced(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    campaign = make_campaign(
        open_positions=[
            make_reservation(
                opportunity_id=f"opportunity-held-{index}",
                allocation_id=f"allocation-held-{index}",
                symbol=f"SYM{index}",
                capital_committed=Decimal("10.00"),
                max_loss=Decimal("10.00"),
            )
            for index in range(5)
        ]
    )

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert RiskReasonCode.MAX_POSITIONS_EXCEEDED in _codes(evaluation)


def test_one_structure_per_underlying_by_default(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    """Existing SPY long call, candidate SPY straddle: refused by configuration."""
    campaign = make_campaign(
        open_positions=[
            make_reservation(
                symbol="NVDA",
                opportunity_id="opportunity-nvda-held",
                capital_committed=Decimal("100.00"),
                max_loss=Decimal("100.00"),
            )
        ]
    )

    evaluation = _evaluate(
        risk_limits,
        make_candidate(strategy=StrategyType.LONG_CALL),
        campaign,
        account=make_account(),
    )

    assert RiskReasonCode.MAX_POSITIONS_PER_UNDERLYING_EXCEEDED in _codes(evaluation)


def test_the_per_run_position_cap_is_enforced(
    risk_limits, make_candidate, make_campaign, make_account
):
    evaluation = _evaluate(
        risk_limits,
        make_candidate(),
        make_campaign(),
        account=make_account(),
        new_positions_this_run=3,
    )

    assert RiskReasonCode.MAX_NEW_POSITIONS_PER_RUN_REACHED in _codes(evaluation)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------
def test_an_already_allocated_opportunity_is_refused(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    candidate = make_candidate()
    campaign = make_campaign(
        open_positions=[
            make_reservation(
                opportunity_id=candidate.opportunity_id,
                symbol="NVDA",
                capital_committed=Decimal("605.00"),
                max_loss=Decimal("605.00"),
            )
        ]
    )
    limits = risk_limits.model_copy(update={"max_positions_per_underlying": 5})

    evaluation = _evaluate(limits, candidate, campaign, account=make_account())

    assert RiskReasonCode.DUPLICATE_OPPORTUNITY in _codes(evaluation)


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------
def test_a_missing_price_is_rejected_and_never_replaced(
    risk_limits, make_candidate, make_campaign, make_account
):
    candidate = make_candidate(
        price_overrides={
            "available": False,
            "source": None,
            "unit_cost": None,
            "max_leg_spread_pct": None,
            "quote_as_of": None,
            "unavailable_reason": "no ask on leg 0",
        }
    )

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.PRICE_UNAVAILABLE in _codes(evaluation)
    assert evaluation.unit_cost is None, "a missing price must never become a number"
    assert evaluation.unit_max_loss is None


def test_a_zero_price_is_rejected(risk_limits, make_candidate, make_campaign, make_account):
    candidate = make_candidate(price_overrides={"unit_cost": Decimal("0")})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.INVALID_PRICE in _codes(evaluation)


def test_a_negative_price_cannot_even_be_expressed(make_candidate):
    """``ge=0`` on the model: a negative debit fails to parse, not to validate."""
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        make_candidate(price_overrides={"unit_cost": Decimal("-1.00")})


def test_a_float_price_is_refused_outright(make_candidate):
    """Money never comes from binary floating point (specification section 21)."""
    with pytest.raises(ValueError, match="must not be built from binary floating point"):
        make_candidate(price_overrides={"unit_cost": 605.00})


def test_a_stale_quote_is_rejected(risk_limits, make_candidate, make_campaign, make_account):
    """Age is measured against the decision instant, not against wall clock."""
    stale = NOW - timedelta(seconds=risk_limits.max_market_data_age_seconds + 1)
    candidate = make_candidate(price_overrides={"quote_as_of": stale})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.STALE_MARKET_DATA in _codes(evaluation)


def test_a_quote_with_no_timestamp_is_stale_not_fresh(
    risk_limits, make_candidate, make_campaign, make_account
):
    candidate = make_candidate(price_overrides={"quote_as_of": None})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.STALE_MARKET_DATA in _codes(evaluation)


def test_a_price_that_cannot_be_attributed_to_the_contract_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account, make_leg
):
    candidate = make_candidate(legs=[make_leg(quote_as_of=None)])

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.INVALID_PRICE in _codes(evaluation)


def test_an_unmeasured_spread_is_recorded_unevaluated_not_passed(
    risk_limits, make_candidate, make_campaign, make_account
):
    candidate = make_candidate(price_overrides={"max_leg_spread_pct": None})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    spread = next(check for check in evaluation.checks if check.name == "bid_ask_spread")
    assert spread.outcome is RiskCheckOutcome.NOT_EVALUATED
    assert "not a narrow one" in (spread.detail or "")


def test_a_spread_above_the_strategy_ceiling_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account
):
    candidate = make_candidate(price_overrides={"max_leg_spread_pct": 25.0})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.SPREAD_TOO_WIDE in _codes(evaluation)


def test_an_invalid_multiplier_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account, make_leg, make_profile
):
    """Two legs with different multipliers are not one position."""
    candidate = make_candidate(
        risk_profile=make_profile(strategy=StrategyType.LONG_STRADDLE, leg_count=2),
        strategy=StrategyType.LONG_STRADDLE,
        legs=[make_leg(), make_leg(leg_index=1, right="PUT", multiplier=10)],
    )

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.INVALID_MULTIPLIER in _codes(evaluation)


# ---------------------------------------------------------------------------
# Maximum loss
# ---------------------------------------------------------------------------
def test_max_loss_comes_from_the_strategy_not_from_a_generic_formula(
    risk_limits, make_candidate, make_campaign, make_account, make_profile
):
    """A strategy whose loss this engine cannot bound is refused, not estimated."""
    candidate = make_candidate(risk_profile=make_profile(max_loss_basis=MaxLossBasis.NOT_DEFINED))

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.MAX_LOSS_UNDEFINED in _codes(evaluation)
    assert evaluation.unit_max_loss is None


def test_a_long_debit_structure_risks_exactly_what_it_cost(
    risk_limits, make_candidate, make_campaign, make_account
):
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())

    assert evaluation.unit_max_loss == evaluation.unit_cost
    check = next(c for c in evaluation.checks if c.name == "max_loss_model")
    assert check.outcome is RiskCheckOutcome.PASS
    assert "what it cost" in (check.detail or "")


# ---------------------------------------------------------------------------
# Data quality, currency, account, point in time
# ---------------------------------------------------------------------------
def test_an_upstream_quality_failure_is_respected_not_re_graded(
    risk_limits, make_candidate, make_campaign, make_account
):
    candidate = make_candidate(research_usable=False, data_quality=DataQuality.UNUSABLE)

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.DATA_QUALITY_FAILED in _codes(evaluation)


def test_an_instrument_in_another_currency_is_refused_rather_than_converted(
    risk_limits, make_candidate, make_campaign, make_account, make_leg
):
    """A price is never converted, in either direction.

    The asymmetry with the capital limits - which *are* converted - is
    deliberate and is about what happens to the number next. A limit is
    compared and then discarded; a price becomes the limit price on an order,
    and the exchange expects that figure in the contract's own currency. A
    converted one would not be a rounding difference, it would be the wrong
    number on the wire.
    """
    candidate = make_candidate(legs=[make_leg(currency="EUR")], price_overrides={"currency": "EUR"})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.CURRENCY_MISMATCH in _codes(evaluation)


def test_the_shipped_configuration_accepts_a_us_listed_contract(
    risk_limits, make_candidate, make_campaign, make_account
):
    """The operational consequence that changed, pinned so it cannot regress.

    This test used to assert the opposite: the shipped EUR campaign refused
    every USD option, every time, and the only ways forward were to redenominate
    the campaign or to declare a dollar equal to a euro.

    Neither is what the system does now. The capital stays declared in EUR - the
    operator's IBKR base currency is unchanged and does not need to change - and
    the campaign trades USD because that is what a US-listed option is quoted
    in. An explicit rate connects them. A currency mismatch between an account
    and a campaign is the expected state, not an error.
    """
    usd = make_candidate()

    evaluation = _evaluate(risk_limits, usd, make_campaign(), account=make_account())

    assert evaluation.outcome is RiskOutcome.APPROVED
    assert RiskReasonCode.CURRENCY_MISMATCH not in _codes(evaluation)
    assert evaluation.fx is not None and evaluation.fx.ok, (
        "the verdict records the rate it rested on, so a stored authorisation "
        "can be re-derived without loading a configuration that has since moved"
    )


def test_without_a_rate_nothing_is_authorised_and_nothing_is_assumed_equal(
    unconvertible_limits, make_candidate, make_campaign, make_account
):
    """Case 2 and Case 6: no rate, no authorisation, no parity.

    The candidate is well-formed and affordable. What is missing is the one
    thing that would let a EUR envelope be compared with a USD price, and its
    absence is a rejection rather than an assumption.
    """
    account = make_account(fx_rates=FxRateTable())

    evaluation = _evaluate(unconvertible_limits, make_candidate(), make_campaign(), account=account)

    assert evaluation.outcome is RiskOutcome.REJECTED
    assert RiskReasonCode.FX_RATE_UNAVAILABLE in _codes(evaluation)


def test_a_stale_rate_is_its_own_reason_code(
    system_config, make_candidate, make_campaign, make_account
):
    """ "We could not find a rate" and "the rate is old" want different fixes."""
    window = system_config.campaign.currency_policy.max_rate_age_seconds
    old = NOW - timedelta(seconds=window + 1)
    limits = resolve_limits(system_config, fx_rates=eur_usd_rates(as_of=old), as_of=NOW)

    evaluation = _evaluate(
        limits,
        make_candidate(),
        make_campaign(),
        account=make_account(fx_rates=eur_usd_rates(as_of=old)),
    )

    assert RiskReasonCode.FX_RATE_STALE in _codes(evaluation)
    assert RiskReasonCode.FX_RATE_UNAVAILABLE not in _codes(evaluation)


def test_no_capacity_check_runs_when_the_limits_are_in_another_currency(
    unconvertible_limits, make_candidate, make_campaign, make_account
):
    """The rejection arrives *before* any figure is compared with any other.

    Skipping the capacity checks rather than running them against unconverted
    figures is the point: a comparison of EUR 5,000 against a USD price would
    produce a verdict, and the verdict would be wrong by the exchange rate
    while looking exactly like a clean one.
    """
    evaluation = _evaluate(
        unconvertible_limits,
        make_candidate(),
        make_campaign(),
        account=make_account(fx_rates=FxRateTable()),
    )

    names = {check.name for check in evaluation.checks}
    assert "campaign_currency_conversion" in names
    assert "campaign_budget" not in names
    assert "broker_available_funds" not in names


def test_the_account_balance_is_converted_before_it_is_compared(
    risk_limits, make_candidate, make_campaign, make_account
):
    """Case 4, and the comparison that used to read 5,000 EUR against a dollar.

    The account holds EUR and the contract costs USD. The check that decides
    whether the broker has the money records both figures and the rate between
    them, so the arithmetic can be checked by hand from the stored artifact.
    """
    evaluation = _evaluate(
        risk_limits,
        make_candidate(),
        make_campaign(),
        account=make_account(available_funds=Decimal("1000.00"), buying_power=None, cash=None),
    )

    funds = next(c for c in evaluation.checks if c.name == "broker_available_funds")
    assert funds.limit == "1100.00", "EUR 1,000 x 1.10, not EUR 1,000 read as dollars"
    assert "1000.00 EUR" in (funds.detail or "")
    assert "1.10" in (funds.detail or "")


def test_an_account_the_broker_quotes_no_rate_for_cannot_be_spent_against(
    risk_limits, make_candidate, make_campaign, make_account
):
    """The limits converted; the balance did not. That is still a refusal.

    The two conversions are separate facts that happen to share a rate today.
    A snapshot carrying no rates at all still fails, and it fails naming the
    account rather than the campaign, because that is where the fix is.
    """
    evaluation = _evaluate(
        risk_limits,
        make_candidate(),
        make_campaign(),
        account=make_account(fx_rates=FxRateTable()),
    )

    assert RiskReasonCode.FX_RATE_UNAVAILABLE in _codes(evaluation)
    failed = next(c for c in evaluation.checks if c.name == "account_currency_conversion")
    assert failed.outcome is RiskCheckOutcome.FAIL


def test_legs_in_two_currencies_are_refused(
    risk_limits, make_candidate, make_campaign, make_account, make_leg, make_profile
):
    candidate = make_candidate(
        risk_profile=make_profile(strategy=StrategyType.LONG_STRADDLE, leg_count=2),
        strategy=StrategyType.LONG_STRADDLE,
        legs=[make_leg(), make_leg(leg_index=1, right="PUT", currency="EUR")],
    )

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.CURRENCY_MISMATCH in _codes(evaluation)


def test_a_missing_account_snapshot_fails_closed(risk_limits, make_candidate, make_campaign):
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=None)

    assert RiskReasonCode.ACCOUNT_SNAPSHOT_UNAVAILABLE in _codes(evaluation)


def test_a_stale_account_snapshot_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account
):
    old = NOW - timedelta(seconds=risk_limits.max_account_snapshot_age_seconds + 1)
    account = make_account(as_of=old, captured_at=old)

    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=account)

    assert RiskReasonCode.ACCOUNT_SNAPSHOT_STALE in _codes(evaluation)


def test_an_account_reporting_no_balance_is_invalid_not_empty(
    risk_limits, make_candidate, make_campaign, make_account
):
    """An unreported balance is unknown, never zero."""
    account = make_account(cash=None, buying_power=None, available_funds=None)

    assert account.spendable is None
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=account)

    assert RiskReasonCode.INVALID_ACCOUNT_SNAPSHOT in _codes(evaluation)


def test_a_score_below_the_floor_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account, make_score
):
    candidate = make_candidate(score=make_score(total=10.0))

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert RiskReasonCode.BELOW_MIN_OPPORTUNITY_SCORE in _codes(evaluation)


# ---------------------------------------------------------------------------
# Daily loss
# ---------------------------------------------------------------------------
def test_an_untracked_daily_loss_is_unevaluated_not_passed(
    risk_limits, make_candidate, make_campaign, make_account
):
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())

    check = next(c for c in evaluation.checks if c.name == "daily_loss")
    assert check.outcome is RiskCheckOutcome.NOT_EVALUATED
    assert evaluation.outcome is RiskOutcome.APPROVED, "configuration does not require it yet"


def test_configuration_can_make_an_untracked_daily_loss_block_a_trade(
    risk_limits, make_candidate, make_campaign, make_account
):
    limits = risk_limits.model_copy(update={"require_daily_loss_tracking": True})

    evaluation = _evaluate(limits, make_candidate(), make_campaign(), account=make_account())

    assert RiskReasonCode.DAILY_LOSS_NOT_TRACKED in _codes(evaluation)


def test_a_breached_daily_loss_limit_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account
):
    # The limit is EUR 750 declared, USD 825 converted.
    campaign = make_campaign(realized_pnl_today=Decimal("-900.00"))

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED in _codes(evaluation)


def test_a_profitable_day_does_not_count_as_a_loss(
    risk_limits, make_candidate, make_campaign, make_account
):
    campaign = make_campaign(realized_pnl_today=Decimal("800.00"))

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert evaluation.outcome is RiskOutcome.APPROVED


# ---------------------------------------------------------------------------
# Trading mode
# ---------------------------------------------------------------------------
def test_live_without_its_guards_is_rejected(
    risk_limits, make_candidate, make_campaign, make_account
):
    evaluation = _evaluate(
        risk_limits,
        make_candidate(),
        make_campaign(),
        account=make_account(),
        trading_mode=TradingMode.LIVE,
        live_guards_satisfied=False,
    )

    assert RiskReasonCode.LIVE_MODE_GUARD_NOT_SATISFIED in _codes(evaluation)


# ---------------------------------------------------------------------------
# Determinism and the critical invariant
# ---------------------------------------------------------------------------
def test_identical_inputs_produce_an_identical_verdict(
    risk_limits, make_candidate, make_campaign, make_account
):
    first = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())
    second = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.evaluation_id == second.evaluation_id


def test_the_check_order_is_stable(risk_limits, make_candidate, make_campaign, make_account):
    """Reason-code order carries meaning, so check order must not drift."""
    first = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())
    second = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())

    assert [c.name for c in first.checks] == [c.name for c in second.checks]


def test_no_agent_can_override_a_rejection(risk_limits):
    """The critical invariant, asserted structurally.

    There is no argument, field or method through which a model's opinion could
    reach this engine — no rationale, no confidence the agent asserted about
    its own work, no override flag. A prompt that tried would have nowhere to
    put the answer.
    """
    import inspect

    parameters = set(inspect.signature(RiskEngine.evaluate).parameters)
    forbidden = {
        "override",
        "force",
        "rationale",
        "agent",
        "llm_client",
        "model",
        "approval",
        "confidence",
    }

    assert parameters & forbidden == set()
    assert set(inspect.signature(RiskEngine.__init__).parameters) == {"self", "limits"}

    source = inspect.getsource(RiskEngine)
    for name in ("LLMClient", "AnthropicLLMClient", "agents", "prompt"):
        assert name not in source


def test_a_rejection_never_reports_ok(risk_limits, make_candidate, make_campaign):
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=None)

    assert evaluation.outcome is RiskOutcome.REJECTED
    assert RiskReasonCode.OK not in evaluation.reason_codes


def test_every_rejection_is_machine_readable(risk_limits, make_candidate, make_campaign):
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=None)

    assert evaluation.reason_codes
    for check in evaluation.failed_checks:
        assert check.reason_code is not None
        assert check.describe()


def test_an_explanation_is_derived_from_the_checks(
    risk_limits, make_candidate, make_campaign, make_account
):
    """Prose is a rendering of the decision, never the decision itself."""
    candidate = make_candidate(price_overrides={"max_leg_spread_pct": 25.0})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())
    explanation = evaluation.explain()

    assert "SPREAD_TOO_WIDE" in explanation
    assert "25.00%" in explanation
    assert "10.0%" in explanation
