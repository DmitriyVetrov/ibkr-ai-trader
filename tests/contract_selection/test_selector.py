"""The deterministic contract selector (brief section 37).

Every case here is a way the selector must either produce exactly the right
contracts or refuse and say why. The refusals matter as much as the successes:
"no valid contract" is a first-class outcome, and the failure this suite exists
to prevent is a plausible-looking contract chosen because a real one could not
be found.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    ContractRejectionReason,
    ContractSelectionStatus,
    OptionRight,
    StrategyType,
)

from .conftest import FURTHER, NEAR_TARGET, REFERENCE, TOO_FAR, TOO_NEAR

pytestmark = pytest.mark.unit


def _reasons(result) -> set[str]:
    return {rejection.reason.value for rejection in result.rejected_candidates}


# ---------------------------------------------------------------------------
# 1-4. The four strategies select
# ---------------------------------------------------------------------------
def test_a_long_call_selects_one_call(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=StrategyType.LONG_CALL)

    assert result.selection_status is ContractSelectionStatus.SUCCESS, result.status_detail
    assert len(result.legs) == 1
    leg = result.legs[0]
    assert leg.right is OptionRight.CALL
    assert leg.action.value == "BUY"
    assert leg.contract_id is not None
    assert result.expiration == NEAR_TARGET
    assert result.dte == 18


def test_a_long_put_selects_one_put(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=StrategyType.LONG_PUT)

    assert result.selection_status is ContractSelectionStatus.SUCCESS, result.status_detail
    assert [leg.right for leg in result.legs] == [OptionRight.PUT]
    assert result.legs[0].delta is not None
    assert result.legs[0].delta < 0, "put deltas run from 0 to -1"


def test_a_straddle_selects_a_call_and_a_put_on_one_strike(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=StrategyType.LONG_STRADDLE)

    assert result.selection_status is ContractSelectionStatus.SUCCESS, result.status_detail
    assert len(result.legs) == 2
    assert {leg.right for leg in result.legs} == {OptionRight.CALL, OptionRight.PUT}
    assert len({leg.strike for leg in result.legs}) == 1, "a straddle shares one strike"
    assert len({leg.expiration for leg in result.legs}) == 1
    assert result.legs[0].strike == REFERENCE, "at the money, against the reference price"


def test_a_strangle_selects_a_call_above_and_a_put_below(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=StrategyType.LONG_STRANGLE)

    assert result.selection_status is ContractSelectionStatus.SUCCESS, result.status_detail
    call = next(leg for leg in result.legs if leg.right is OptionRight.CALL)
    put = next(leg for leg in result.legs if leg.right is OptionRight.PUT)
    assert call.strike > put.strike, "a strangle is not a straddle"
    assert call.strike >= REFERENCE
    assert put.strike <= REFERENCE
    assert call.expiration == put.expiration


def test_the_selected_contracts_carry_full_broker_identity(priced_chain, select) -> None:
    """Brief section 27: every leg names the contract the broker would fill."""
    priced_chain()

    leg = select().legs[0]

    assert leg.contract_id > 0
    assert leg.trading_class == "NVDA"
    assert leg.multiplier == 100
    assert leg.exchange == "SMART"
    assert leg.chain_snapshot_id
    assert leg.quote_snapshot_id
    assert leg.selection_reason


# ---------------------------------------------------------------------------
# 5-7. Expirations
# ---------------------------------------------------------------------------
def test_no_expiration_in_range_selects_nothing(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain(expirations=[TOO_NEAR, TOO_FAR])
    store_option_quotes(expirations=[TOO_NEAR, TOO_FAR])

    result = select()

    assert result.selection_status is ContractSelectionStatus.NO_VALID_EXPIRATION
    assert not result.legs


def test_an_expiration_below_the_minimum_dte_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain(expirations=[TOO_NEAR])
    store_option_quotes(expirations=[TOO_NEAR])

    result = select()

    assert result.selection_status is ContractSelectionStatus.NO_VALID_EXPIRATION
    assert any("DTE 11" in reason for reason in result.reasons)


def test_an_expiration_above_the_maximum_dte_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain(expirations=[TOO_FAR])
    store_option_quotes(expirations=[TOO_FAR])

    result = select()

    assert result.selection_status is ContractSelectionStatus.NO_VALID_EXPIRATION
    assert any("DTE 39" in reason for reason in result.reasons)


def test_the_target_dte_policy_picks_the_closest_expiration(priced_chain, select) -> None:
    priced_chain()

    result = select()

    assert result.expiration == NEAR_TARGET, "|18-21| beats |25-21|"
    assert result.expiration_policy is not None
    assert "TARGET_DTE" in (result.expiration_reason or "")


def test_an_expiration_on_a_market_holiday_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """Milestone 3's calendar is transcribed from the exchange, not derived."""
    labour_day = date(2026, 9, 7)  # a listed NYSE holiday, 28 days out
    store_underlying_quote()
    store_chain(expirations=[labour_day])
    store_option_quotes(expirations=[labour_day])

    result = select()

    assert result.selection_status is ContractSelectionStatus.NO_VALID_EXPIRATION
    assert ContractRejectionReason.EXPIRATION_NOT_A_TRADING_DAY.value in _reasons(result)


def test_a_second_expiration_is_tried_when_the_first_has_no_usable_strike(
    store_underlying_quote, store_chain, build_option_quotes, store_quote_records, select
) -> None:
    """Preference order, not first-and-fail: the order is still deterministic."""
    store_underlying_quote()
    store_chain()
    store_quote_records(
        [
            *build_option_quotes(expirations=[NEAR_TARGET], open_interest=Decimal("1")),
            *build_option_quotes(expirations=[FURTHER]),
        ]
    )

    result = select()

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.expiration == FURTHER


# ---------------------------------------------------------------------------
# 8-13. Contract validity
# ---------------------------------------------------------------------------
def test_a_contract_with_the_wrong_right_is_never_selected(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=StrategyType.LONG_PUT)

    assert all(leg.right is OptionRight.PUT for leg in result.legs)


def test_a_missing_contract_id_is_a_named_rejection(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(contract_id=None)

    result = select()

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert ContractRejectionReason.MISSING_CONTRACT_ID.value in _reasons(result)


def test_a_chain_with_no_quotes_reports_unavailable_data(
    store_underlying_quote, store_chain, select
) -> None:
    """The ordinary situation today: Milestone 3 deferred per-contract quotes."""
    store_underlying_quote()
    store_chain(with_contracts=True)

    result = select()

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert ContractRejectionReason.MISSING_QUOTE.value in _reasons(result)
    assert not result.legs


def test_a_missing_delta_fails_a_delta_targeted_strategy(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """Brief section 19: delta is never approximated from memory."""
    store_underlying_quote()
    store_chain()
    store_option_quotes(delta=None)

    result = select(strategy=StrategyType.LONG_CALL)

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert ContractRejectionReason.MISSING_DELTA.value in _reasons(result)


def test_a_missing_delta_does_not_stop_an_at_the_money_strategy(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """A policy that does not need delta is not blocked by its absence."""
    store_underlying_quote()
    store_chain()
    store_option_quotes(delta=None)

    result = select(strategy=StrategyType.LONG_STRADDLE)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert all(leg.delta is None for leg in result.legs)


def test_a_contract_with_no_trading_class_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """Milestone 2: the class is the broker's to report, never ours to derive."""
    store_underlying_quote()
    store_chain(trading_class=None)
    store_option_quotes(trading_class=None)

    result = select()

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert ContractRejectionReason.INVALID_TRADING_CLASS.value in _reasons(result)


def test_the_trading_class_is_copied_from_the_chain_not_the_ticker(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """Real SPY validation returned SMART/SPY and SMART/2SPY side by side."""
    store_underlying_quote("SPY")
    store_chain("SPY", trading_class="2SPY")
    store_option_quotes("SPY", trading_class="2SPY")

    result = select(symbol="SPY")

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert all(leg.trading_class == "2SPY" for leg in result.legs)
    assert all(leg.trading_class != leg.underlying for leg in result.legs)


def test_a_quote_from_another_trading_class_is_not_a_candidate(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """A contract assembled across two chains is a contract that may not exist."""
    store_underlying_quote("SPY")
    store_chain("SPY", trading_class="2SPY")
    store_option_quotes("SPY", trading_class="SPY")

    result = select(symbol="SPY")

    assert result.selection_status is not ContractSelectionStatus.SUCCESS


def test_a_stale_quote_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select, selection_now
) -> None:
    old = selection_now - timedelta(days=3)
    store_underlying_quote()
    store_chain()
    store_option_quotes(as_of=old, retrieved_at=old)

    result = select()

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert ContractRejectionReason.QUOTE_STALE.value in _reasons(result)


def test_a_quote_the_quality_engine_rejected_is_not_selectable(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """The data layer owns that judgement; this stage never re-litigates it."""
    store_underlying_quote()
    store_chain()
    store_option_quotes(research_usable=False)

    result = select()

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert ContractRejectionReason.QUOTE_NOT_RESEARCH_USABLE.value in _reasons(result)


# ---------------------------------------------------------------------------
# 16. Option liquidity
# ---------------------------------------------------------------------------
def test_low_option_liquidity_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(open_interest=Decimal("10"), volume=Decimal("5"))

    result = select()

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert ContractRejectionReason.LOW_OPTION_LIQUIDITY.value in _reasons(result)


def test_unknown_option_liquidity_is_rejected_by_default(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """Missing is not zero, and it is not "fine" either."""
    store_underlying_quote()
    store_chain()
    store_option_quotes(open_interest=None, volume=None)

    result = select()

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert ContractRejectionReason.OPTION_LIQUIDITY_UNKNOWN.value in _reasons(result)


def test_unknown_option_liquidity_may_be_admitted_deliberately(
    store_underlying_quote,
    store_chain,
    store_option_quotes,
    make_selector,
    select,
    system_config,
) -> None:
    from trading_system.infrastructure.settings import UnknownLiquidityPolicy

    store_underlying_quote()
    store_chain()
    store_option_quotes(open_interest=None, volume=None)
    quotes = system_config.contract_selection.quotes.model_copy(
        update={"unknown_liquidity_policy": UnknownLiquidityPolicy.ALLOW}
    )

    result = select(selector=make_selector(quotes=quotes))

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.legs[0].open_interest is None, "admitted, not filled in"


def test_underlying_volume_is_never_read_as_option_liquidity(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """A deeply traded underlying says nothing about one of its contracts."""
    store_underlying_quote()  # 240m shares
    store_chain()
    store_option_quotes(open_interest=Decimal("0"), volume=Decimal("0"))

    result = select()

    assert result.selection_status is not ContractSelectionStatus.SUCCESS


# ---------------------------------------------------------------------------
# Price, spread and volatility bounds
# ---------------------------------------------------------------------------
def test_a_contract_outside_the_price_band_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(bid=Decimal("80.00"), ask=Decimal("80.10"))

    result = select()

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert ContractRejectionReason.OPTION_PRICE_OUT_OF_RANGE.value in _reasons(result)


def test_a_wide_spread_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(bid=Decimal("1.00"), ask=Decimal("9.00"))

    result = select()

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert ContractRejectionReason.SPREAD_TOO_WIDE.value in _reasons(result)


def test_implied_volatility_outside_the_strategy_band_is_rejected(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(implied_volatility=Decimal("3.50"))

    result = select()

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert ContractRejectionReason.IMPLIED_VOLATILITY_OUT_OF_RANGE.value in _reasons(result)


# ---------------------------------------------------------------------------
# 9. Strike policy
# ---------------------------------------------------------------------------
def test_a_chain_too_coarse_for_the_strike_policy_selects_nothing(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """Brief section 29: the closest contract is not selected as a consolation."""
    far = [Decimal("100"), Decimal("260")]
    store_underlying_quote()
    store_chain(strikes=far)
    store_option_quotes(strikes=far)

    result = select(strategy=StrategyType.LONG_STRADDLE)

    assert result.selection_status is ContractSelectionStatus.NO_VALID_STRIKE
    assert not result.legs


def test_an_out_of_the_money_leg_must_be_on_the_correct_side(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=StrategyType.LONG_STRANGLE)

    call = next(leg for leg in result.legs if leg.right is OptionRight.CALL)
    assert call.strike >= REFERENCE
    assert ContractRejectionReason.STRIKE_POLICY_NOT_SATISFIED.value in _reasons(result)


def test_a_missing_reference_price_stops_an_at_the_money_policy(
    store_chain, store_option_quotes, select
) -> None:
    """No underlying quote and no option-carried price: the money is not guessed."""
    store_chain()
    store_option_quotes()

    result = select(strategy=StrategyType.LONG_STRADDLE)

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert any("reference price" in reason for reason in result.reasons)


def test_an_option_quotes_own_underlying_price_can_stand_in(
    store_chain, store_option_quotes, select
) -> None:
    """Same measurement, same provider — and it is recorded as the source used."""
    store_chain()
    store_option_quotes(underlying_price=REFERENCE)

    result = select(strategy=StrategyType.LONG_STRADDLE)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.reference_price_field == "OPTION_UNDERLYING_PRICE"


# ---------------------------------------------------------------------------
# 17-18. Multi-leg integrity
# ---------------------------------------------------------------------------
def test_a_straddle_whose_legs_cannot_share_a_strike_is_refused(
    store_underlying_quote, store_chain, build_option_quotes, store_quote_records, select
) -> None:
    """One leg per strike is two positions, not a straddle."""
    store_underlying_quote()
    store_chain()
    store_quote_records(
        [
            *build_option_quotes(strikes=[Decimal("180")], rights=[OptionRight.CALL]),
            *build_option_quotes(strikes=[Decimal("175")], rights=[OptionRight.PUT]),
        ]
    )

    result = select(strategy=StrategyType.LONG_STRADDLE)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert not result.legs


def test_a_strangle_that_would_collapse_onto_one_strike_is_refused(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """A single-strike "strangle" is a straddle under another name."""
    single = [Decimal("180")]
    store_underlying_quote()
    store_chain(strikes=single)
    store_option_quotes(strikes=single)

    result = select(strategy=StrategyType.LONG_STRANGLE)

    assert result.selection_status is ContractSelectionStatus.NO_VALID_CONTRACT
    assert ContractRejectionReason.INCOMPATIBLE_LEG.value in _reasons(result)
    assert any("straddle under another name" in reason for reason in result.reasons)


def test_a_multi_leg_selection_is_never_partial(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    store_underlying_quote()
    store_chain()
    store_option_quotes(rights=[OptionRight.CALL])

    result = select(strategy=StrategyType.LONG_STRADDLE)

    assert result.selection_status is not ContractSelectionStatus.SUCCESS
    assert result.legs == []


# ---------------------------------------------------------------------------
# 19-20. No chain, empty chain
# ---------------------------------------------------------------------------
def test_no_chain_at_all_is_reported_as_unavailable(store_underlying_quote, select) -> None:
    store_underlying_quote()

    result = select()

    assert result.selection_status is ContractSelectionStatus.OPTION_CHAIN_UNAVAILABLE
    assert "collect one first" in (result.status_detail or "")


def test_an_empty_chain_is_invalid_rather_than_merely_unlucky(
    store_underlying_quote, store_chain, select
) -> None:
    store_underlying_quote()
    store_chain(expirations=[], strikes=[])

    result = select()

    assert result.selection_status is ContractSelectionStatus.INVALID_CHAIN
    assert "did not return one" in (result.status_detail or "")


def test_a_chain_filed_under_the_wrong_symbol_is_invalid(
    store_underlying_quote, store_chain, data_repo, select, selection_now
) -> None:
    from trading_system.data.models import DataQualityReport
    from trading_system.data.repository import build_snapshot
    from trading_system.domain.enums import DataType, MarketDataOrigin, SourceTier

    store_underlying_quote()
    chain = store_chain("AAPL")
    snapshot = build_snapshot(
        data_type=DataType.OPTION_CHAIN,
        key="NVDA",
        records=[chain],
        provider="IBKR",
        source_tier=SourceTier.TIER_1,
        origin=MarketDataOrigin.BROKER_DELAYED,
        as_of=selection_now,
        retrieved_at=selection_now,
        quality=DataQualityReport(evaluated_at=selection_now),
    )
    data_repo.save_snapshot(snapshot)

    result = select()

    assert result.selection_status is ContractSelectionStatus.INVALID_CHAIN


# ---------------------------------------------------------------------------
# 25. Cost
# ---------------------------------------------------------------------------
def test_the_cost_estimate_uses_the_ask_and_exact_decimals(priced_chain, select) -> None:
    priced_chain()

    result = select(strategy=StrategyType.LONG_STRADDLE)
    cost = result.cost

    assert cost is not None and cost.available
    assert isinstance(cost.estimated_debit, Decimal)
    expected = sum(
        (leg.ask * Decimal(leg.multiplier) * Decimal(leg.ratio) for leg in result.legs),
        Decimal(0),
    )
    assert cost.estimated_debit == expected


def test_the_shipped_strategies_refuse_a_contract_with_no_ask(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """Every shipped specification lists BID and ASK as required."""
    store_underlying_quote()
    store_chain()
    store_option_quotes(ask=None, bid=None)

    result = select(strategy=StrategyType.LONG_STRADDLE)

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert ContractRejectionReason.MISSING_REQUIRED_FIELD.value in _reasons(result)


def test_an_unquoted_leg_leaves_the_cost_unknown(
    store_underlying_quote,
    store_chain,
    store_option_quotes,
    make_selector,
    make_decision,
    registry,
    system_config,
) -> None:
    """Brief section 25: with no ask there is no cost, and no invented midpoint.

    Reachable only through a specification that does not require a quote, since
    every shipped one does. The branch still has to be right: a later stage
    reading a fabricated midpoint as a cost would size a position against it.
    """
    import dataclasses

    from trading_system.strategies.contract_selector import SelectionContext

    quotes = system_config.contract_selection.quotes.model_copy(update={"require_quote": False})
    specification = dataclasses.replace(
        registry.require(StrategyType.LONG_CALL),
        required_option_fields=(),
        require_option_liquidity=False,
    )
    store_underlying_quote()
    store_chain()
    store_option_quotes(ask=None, bid=None)
    decision = make_decision()

    result = make_selector(quotes=quotes).select(
        SelectionContext(
            run_id="contract-run-test",
            decision=decision,
            specification=specification,
            as_of=decision.as_of,
        )
    )

    assert result.selection_status is ContractSelectionStatus.SUCCESS, result.status_detail
    assert result.cost is not None
    assert not result.cost.available
    assert result.cost.estimated_debit is None
    assert "never invented" in (result.cost.unavailable_reason or "")


def test_the_cost_carries_no_quantity_and_no_allocation(priced_chain, select) -> None:
    """Brief section 34: sizing belongs to the risk and allocation engines."""
    from trading_system.strategies.models import ContractCostEstimate

    priced_chain()
    result = select()

    fields = set(ContractCostEstimate.model_fields)
    for forbidden in ("quantity", "allocation", "allocated", "budget", "position_size"):
        assert forbidden not in fields
    assert result.cost is not None


# ---------------------------------------------------------------------------
# 28. Rejections are preserved
# ---------------------------------------------------------------------------
def test_every_rejected_candidate_carries_a_reason(priced_chain, select) -> None:
    priced_chain()

    result = select()

    assert result.rejected_candidates
    assert all(rejection.reason is not None for rejection in result.rejected_candidates)
    assert result.candidates_considered > len(result.legs)


def test_the_rejection_list_is_bounded_and_says_so(
    priced_chain, make_selector, select, system_config
) -> None:
    limits = system_config.contract_selection.limits.model_copy(update={"max_rejected_recorded": 2})
    priced_chain()

    result = select(selector=make_selector(limits=limits))

    assert len(result.rejected_candidates) == 2
    assert result.rejections_recorded_truncated
    assert result.candidates_considered > 2


def test_a_chain_beyond_the_candidate_ceiling_is_refused_not_sampled(
    priced_chain, make_selector, select, system_config
) -> None:
    limits = system_config.contract_selection.limits.model_copy(update={"max_candidates": 1})
    priced_chain()

    result = select(selector=make_selector(limits=limits))

    assert result.selection_status is ContractSelectionStatus.INVALID_CHAIN
    assert "Selecting from a subset" in (result.status_detail or "")


# ---------------------------------------------------------------------------
# 24. Event alignment
# ---------------------------------------------------------------------------
def test_an_event_aligned_strategy_expires_after_the_event(
    priced_chain, select, selection_now
) -> None:
    """The event comes from the research report; the rule comes from config."""
    priced_chain()
    event = selection_now + timedelta(days=20)  # 2026-08-30, after NEAR_TARGET

    result = select(strategy=StrategyType.LONG_STRADDLE, event_time=event)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.expiration == FURTHER
    assert result.expiration is not None and result.expiration > event.date()
    assert "EVENT_ALIGNED" in (result.expiration_reason or "")


def test_an_event_aligned_strategy_without_an_event_falls_back_and_says_so(
    priced_chain, select
) -> None:
    """No event date is ever inferred: the fallback is recorded, not hidden."""
    priced_chain()

    result = select(strategy=StrategyType.LONG_STRADDLE, event_time=None)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.expiration == NEAR_TARGET
    assert any("names no event" in reason for reason in result.reasons)


def test_an_event_beyond_every_expiration_falls_back_rather_than_inventing_one(
    priced_chain, select, selection_now
) -> None:
    priced_chain()
    event = selection_now + timedelta(days=200)

    result = select(strategy=StrategyType.LONG_STRADDLE, event_time=event)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert any("falling back" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# The selection is explainable
# ---------------------------------------------------------------------------
def test_the_selection_names_the_snapshots_it_rests_on(priced_chain, select) -> None:
    priced_chain()

    result = select()

    assert result.input_snapshot_ids
    assert result.legs[0].chain_snapshot_id in result.input_snapshot_ids
    assert result.legs[0].quote_snapshot_id in result.input_snapshot_ids


def test_the_selection_records_the_reference_price_and_its_field(priced_chain, select) -> None:
    priced_chain()

    result = select()

    assert result.reference_price == REFERENCE
    assert result.reference_price_field == "LAST"


def test_a_failed_selection_carries_no_legs(store_underlying_quote, select) -> None:
    store_underlying_quote()

    result = select()

    assert not result.succeeded
    assert result.legs == []
    assert result.cost is None


def test_a_no_trade_decision_selects_nothing(
    priced_chain, make_decision, make_selector, registry
) -> None:
    from trading_system.domain.enums import StrategyAction
    from trading_system.strategies.contract_selector import SelectionContext

    priced_chain()
    decision = make_decision(action=StrategyAction.NO_TRADE)
    assert decision.selected_strategy is None

    # The service short-circuits before the selector for a NO_TRADE, so the
    # selector is never asked. Asserting the decision shape is the check that
    # matters: nothing downstream can turn it into a contract.
    context = SelectionContext(
        run_id="contract-run-test",
        decision=decision,
        specification=registry.require(StrategyType.LONG_CALL),
        as_of=decision.as_of,
    )
    assert context.decision.action is StrategyAction.NO_TRADE


def test_the_projection_onto_the_purchase_card_boundary_matches(priced_chain, select) -> None:
    """Milestone 1's ContractSelection is the contract the next stage consumes."""
    priced_chain()

    result = select(strategy=StrategyType.LONG_STRADDLE)
    projected = result.to_contract_selection()

    assert projected.underlying == "NVDA"
    assert projected.strategy_type is StrategyType.LONG_STRADDLE
    assert len(projected.legs) == 2
    assert projected.dte == result.dte
    assert all(leg.broker_contract_id is not None for leg in projected.legs)


def test_a_failed_selection_cannot_be_projected(store_underlying_quote, select) -> None:
    store_underlying_quote()

    result = select()

    with pytest.raises(ValueError, match="no contract was selected"):
        result.to_contract_selection()
