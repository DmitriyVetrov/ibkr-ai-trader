"""Contract selection is reproducible (brief section 38).

Given identical inputs — the same decision, the same stored chain and quotes,
the same configuration and the same ``as_of`` — the selected contracts must be
identical. Not "equivalent", not "the same strike usually": identical, field for
field, including the rejections and the reasons.

This is the property that makes a past decision reconstructable. A selector
whose answer depended on dictionary ordering, filesystem ordering or the wall
clock would produce a record that could never be re-derived, and every
evaluation of it would be an act of faith.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.domain.enums import ContractSelectionStatus, OptionRight, StrategyType

pytestmark = pytest.mark.unit

STRATEGIES = [
    StrategyType.LONG_CALL,
    StrategyType.LONG_PUT,
    StrategyType.LONG_STRADDLE,
    StrategyType.LONG_STRANGLE,
]


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.value)
def test_repeated_selection_produces_an_identical_record(priced_chain, select, strategy) -> None:
    priced_chain()

    first = select(strategy=strategy)
    second = select(strategy=strategy)
    third = select(strategy=strategy)

    assert first.selection_status is ContractSelectionStatus.SUCCESS
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert second.model_dump(mode="json") == third.model_dump(mode="json")


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.value)
def test_the_selection_id_is_derived_from_the_contracts_chosen(
    priced_chain, select, strategy
) -> None:
    priced_chain()

    first = select(strategy=strategy)
    second = select(strategy=strategy)

    assert first.selection_id == second.selection_id
    assert first.selection_id.startswith(f"contract-{first.symbol}-")


def test_two_selectors_over_the_same_store_agree(priced_chain, make_selector, select) -> None:
    """Two independently constructed selectors are not two opinions."""
    priced_chain()

    first = select(selector=make_selector())
    second = select(selector=make_selector())

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_rejections_are_recorded_in_a_stable_order(priced_chain, select) -> None:
    priced_chain()

    first = select()
    second = select()

    assert [r.model_dump(mode="json") for r in first.rejected_candidates] == [
        r.model_dump(mode="json") for r in second.rejected_candidates
    ]


def test_a_tie_between_strikes_breaks_the_same_way_every_time(
    store_underlying_quote, store_chain, store_option_quotes, select
) -> None:
    """Two strikes equidistant from the target: the lower one, always."""
    symmetric = [Decimal("175"), Decimal("185")]
    store_underlying_quote()
    store_chain(strikes=symmetric)
    store_option_quotes(strikes=symmetric)

    results = [select(strategy=StrategyType.LONG_STRADDLE) for _ in range(5)]

    assert all(r.selection_status is ContractSelectionStatus.SUCCESS for r in results)
    strikes = {leg.strike for result in results for leg in result.legs}
    assert strikes == {Decimal("175")}, "the lower strike wins a tie, deterministically"


def test_a_failure_is_reproducible_too(store_underlying_quote, select) -> None:
    """A record of why nothing was selected must also be re-derivable."""
    store_underlying_quote()

    first = select()
    second = select()

    assert first.selection_status is ContractSelectionStatus.OPTION_CHAIN_UNAVAILABLE
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_the_same_chain_in_a_different_storage_order_selects_the_same_contract(
    store_underlying_quote, store_chain, build_option_quotes, store_quote_records, select
) -> None:
    """Candidate order comes from an explicit sort, not from insertion order."""
    store_underlying_quote()
    store_chain()
    forward = build_option_quotes()
    store_quote_records(list(reversed(forward)))

    result = select(strategy=StrategyType.LONG_STRANGLE)

    call = next(leg for leg in result.legs if leg.right is OptionRight.CALL)
    put = next(leg for leg in result.legs if leg.right is OptionRight.PUT)
    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert call.strike == Decimal("190"), "180 x 1.05 = 189, nearest listed strike is 190"
    assert put.strike == Decimal("170"), "180 x 0.95 = 171, nearest listed strike is 170"
