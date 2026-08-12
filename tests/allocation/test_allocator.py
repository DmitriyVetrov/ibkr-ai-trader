"""Distributing a finite budget across many candidates (brief sections 5, 24, 44).

The situation this milestone exists for: more valid opportunities than money.
Four claims:

* the ordering is **deterministic**, ties included, so two runs over the same
  set fund the same candidates in the same order;
* the budget is carried forward *within* a run, so one run cannot authorise the
  same euro twice;
* the accounting stays exact — ``allocated + available`` never drifts;
* an opportunity already holding a reservation is recognised rather than funded
  again, which is what makes the whole stage idempotent.

The example from the brief — four candidates and a EUR 5,000 campaign — is
worked through at the bottom, arithmetic and all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.allocation.budget_allocator import order_candidates
from trading_system.domain.enums import (
    AllocationOutcome,
    Direction,
    RiskReasonCode,
    StrategyType,
)

pytestmark = pytest.mark.unit


def _outcomes(decisions) -> dict[str, AllocationOutcome]:
    return {d.candidate.symbol: d.outcome for d in decisions}


def _committed(decisions) -> Decimal:
    return sum((d.capital_committed for d in decisions), Decimal("0"))


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def test_candidates_are_ordered_by_score(priced):
    ordered = order_candidates(
        [
            priced("100.00", symbol="AAA", score=70.0),
            priced("100.00", symbol="BBB", score=95.0),
            priced("100.00", symbol="CCC", score=80.0),
        ]
    )

    assert [c.symbol for c in ordered] == ["BBB", "CCC", "AAA"]


def test_ties_break_on_an_explicit_key_never_on_arrival_order(priced):
    """Two runs over the same set must fund the same candidates."""
    candidates = [
        priced("100.00", symbol="ZZZ", score=80.0),
        priced("100.00", symbol="AAA", score=80.0),
        priced("100.00", symbol="MMM", score=80.0),
    ]

    forwards = [c.symbol for c in order_candidates(candidates)]
    backwards = [c.symbol for c in order_candidates(list(reversed(candidates)))]

    assert forwards == ["AAA", "MMM", "ZZZ"]
    assert forwards == backwards


def test_the_rank_recorded_matches_the_order_funded(allocate, priced):
    decisions = allocate(
        [
            priced("300.00", symbol="AAA", score=70.0),
            priced("300.00", symbol="BBB", score=95.0),
        ],
        max_positions_per_underlying=1,
    )

    by_symbol = {d.candidate.symbol: d.rank for d in decisions}
    assert by_symbol["BBB"] == 1
    assert by_symbol["AAA"] == 2


# ---------------------------------------------------------------------------
# Sequential allocation
# ---------------------------------------------------------------------------
def test_one_strong_candidate_is_funded(allocate, priced):
    decisions = allocate([priced("605.00")])

    assert _outcomes(decisions) == {"NVDA": AllocationOutcome.APPROVED}


def test_a_run_cannot_authorise_the_same_euro_twice(allocate, priced):
    """The budget carried forward is what keeps the accounting honest."""
    decisions = allocate(
        [priced("1400.00", symbol=f"SYM{index}", score=90.0 - index) for index in range(5)],
        max_new_positions_per_run=5,
    )

    assert _committed(decisions) <= Decimal("4000.00")


def test_later_candidates_see_the_reduced_budget(allocate, priced):
    """Concentration is relaxed so this is a test about the budget alone."""
    decisions = allocate(
        [
            priced("1400.00", symbol="AAA", score=95.0),
            priced("1400.00", symbol="BBB", score=90.0),
            priced("1400.00", symbol="CCC", score=85.0),
        ],
        max_new_positions_per_run=5,
        max_underlying_concentration_pct=100.0,
        max_strategy_concentration_pct=100.0,
        max_directional_exposure_pct=100.0,
    )

    outcomes = _outcomes(decisions)
    assert outcomes["AAA"] is AllocationOutcome.APPROVED
    assert outcomes["BBB"] is AllocationOutcome.APPROVED
    assert outcomes["CCC"] is not AllocationOutcome.APPROVED, "4,000 does not cover three"
    assert _committed(decisions) == Decimal("2800.00")


def test_more_opportunities_than_capital_is_a_normal_outcome(allocate, priced):
    """Ten valid candidates, EUR 5,000: most of them get nothing, correctly."""
    decisions = allocate(
        [priced("1400.00", symbol=f"SYM{index}", score=95.0 - index) for index in range(10)],
        max_new_positions_per_run=10,
        max_underlying_concentration_pct=100.0,
        max_strategy_concentration_pct=100.0,
        max_directional_exposure_pct=100.0,
    )

    approved = [d for d in decisions if d.outcome is AllocationOutcome.APPROVED]
    assert len(approved) == 2
    assert _committed(decisions) == Decimal("2800.00")
    assert len(decisions) == 10, "every candidate is still recorded, funded or not"


def test_the_per_run_position_cap_stops_a_run_early(allocate, priced):
    decisions = allocate(
        [priced("300.00", symbol=f"SYM{index}", score=95.0 - index) for index in range(6)],
        max_new_positions_per_run=2,
    )

    approved = [d for d in decisions if d.outcome is AllocationOutcome.APPROVED]
    assert len(approved) == 2
    blocked = [
        d
        for d in decisions
        if RiskReasonCode.MAX_NEW_POSITIONS_PER_RUN_REACHED in d.evaluation.reason_codes
    ]
    assert len(blocked) == 4


def test_a_low_scoring_candidate_never_reaches_allocation(allocate, priced):
    decisions = allocate([priced("605.00", score=10.0)])

    assert decisions[0].outcome is AllocationOutcome.REJECTED
    assert RiskReasonCode.BELOW_MIN_OPPORTUNITY_SCORE in decisions[0].evaluation.reason_codes


def test_the_score_orders_but_never_sizes(allocate, priced):
    """A better score is asked first; it is not given a larger position."""
    decisions = allocate(
        [
            priced("605.00", symbol="AAA", score=99.0),
            priced("605.00", symbol="BBB", score=71.0),
        ],
        max_new_positions_per_run=2,
    )

    quantities = {d.candidate.symbol: d.quantity for d in decisions}
    assert quantities["AAA"] == quantities["BBB"]


# ---------------------------------------------------------------------------
# Concentration across a run
# ---------------------------------------------------------------------------
def test_a_second_structure_on_one_underlying_is_refused(allocate, priced, make_candidate):
    decisions = allocate(
        [
            priced("605.00", symbol="NVDA", score=95.0),
            make_candidate(
                opportunity_id="opportunity-nvda-second",
                strategy=StrategyType.LONG_CALL,
                score=priced("605.00").score.model_copy(update={"total": 90.0}),
            ),
        ],
        max_new_positions_per_run=5,
    )

    outcomes = [d.outcome for d in decisions]
    assert outcomes.count(AllocationOutcome.APPROVED) == 1
    refused = next(d for d in decisions if d.outcome is not AllocationOutcome.APPROVED)
    assert RiskReasonCode.MAX_POSITIONS_PER_UNDERLYING_EXCEEDED in refused.evaluation.reason_codes


def test_directional_exposure_accumulates_within_a_run(allocate, priced):
    """70% of 5,000 is 3,500 of BULLISH exposure across the whole campaign."""
    decisions = allocate(
        [priced("1400.00", symbol=f"SYM{index}", score=95.0 - index) for index in range(4)],
        max_new_positions_per_run=4,
        max_underlying_concentration_pct=100.0,
        max_strategy_concentration_pct=100.0,
    )

    committed = _committed(decisions)
    assert committed <= Decimal("3500.00")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_an_already_reserved_opportunity_is_recognised_not_refunded(
    allocate, priced, make_campaign, make_reservation
):
    candidate = priced("605.00")
    campaign = make_campaign(
        open_positions=[
            make_reservation(
                opportunity_id=candidate.opportunity_id,
                symbol="NVDA",
                capital_committed=Decimal("605.00"),
                max_loss=Decimal("605.00"),
                strategy=StrategyType.LONG_STRADDLE,
                direction=Direction.UNCERTAIN,
            )
        ]
    )

    [decision] = allocate([candidate], campaign=campaign, max_positions_per_underlying=5)

    assert decision.outcome is AllocationOutcome.ALREADY_ALLOCATED
    assert decision.quantity == 0
    assert decision.capital_committed == Decimal("0")


def test_re_running_over_the_same_campaign_produces_the_same_answer(allocate, priced):
    first = allocate([priced("605.00")])
    second = allocate([priced("605.00")])

    assert [d.outcome for d in first] == [d.outcome for d in second]
    assert [d.quantity for d in first] == [d.quantity for d in second]
    assert first[0].evaluation.evaluation_id == second[0].evaluation.evaluation_id


def test_a_rejected_candidate_is_never_sized(allocate, priced):
    """No layer may override a risk rejection."""
    decisions = allocate([priced("605.00", score=10.0)])

    assert decisions[0].calculation is None
    assert decisions[0].quantity == 0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_identical_inputs_produce_identical_output(allocate, priced):
    candidates = [
        priced("605.00", symbol="AAA", score=95.0),
        priced("605.00", symbol="BBB", score=90.0),
    ]

    first = allocate(candidates, max_new_positions_per_run=5)
    second = allocate(candidates, max_new_positions_per_run=5)

    assert [(d.candidate.symbol, d.outcome, d.quantity) for d in first] == [
        (d.candidate.symbol, d.outcome, d.quantity) for d in second
    ]


def test_input_order_does_not_change_the_result(allocate, priced):
    candidates = [
        priced("1400.00", symbol="AAA", score=95.0),
        priced("1400.00", symbol="BBB", score=90.0),
        priced("1400.00", symbol="CCC", score=85.0),
    ]

    forwards = allocate(candidates, max_new_positions_per_run=5)
    backwards = allocate(list(reversed(candidates)), max_new_positions_per_run=5)

    assert {d.candidate.symbol: d.quantity for d in forwards} == {
        d.candidate.symbol: d.quantity for d in backwards
    }


def test_no_candidates_yields_no_decisions(allocate):
    assert allocate([]) == []


# ---------------------------------------------------------------------------
# The worked example from the brief (section 43)
# ---------------------------------------------------------------------------
def test_the_worked_campaign_example(allocate, priced):
    """Four candidates against EUR 5,000, under the shipped limits.

    Every figure below comes from ``config/risk.yaml`` and
    ``config/campaign.yaml`` as shipped, and all four candidates are long calls
    — which is what makes the last row interesting.

    Allocatable is 4,000: the budget less its 20% reserve. Per-trade allocation
    and per-trade risk are both 1,500. Each underlying may hold 30% of the
    campaign (1,500); each *strategy* may hold 50% (2,500), shared across every
    long call in the book. Taken in score order:

    ==========  =========  ========  ==========================================
    candidate   unit cost  quantity  why
    ==========  =========  ========  ==========================================
    CCC (94)    2,000      0         one unit exceeds the 1,500 per-trade cap
    AAA (90)    500        3         1,500: the per-trade cap
    BBB (85)    1,000      1         1,000: only 1,000 of LONG_CALL room left
    DDD (75)    750        0         LONG_CALL concentration is exhausted
    ==========  =========  ========  ==========================================

    The last row is the point. Nothing about DDD is wrong — it is affordable,
    inside every per-trade limit, and the campaign still holds 1,500 — but the
    book already carries as much of one strategy as policy permits. A valid
    strategy is not an entitlement to capital.
    """
    decisions = allocate(
        [
            priced("500.00", symbol="AAA", score=90.0),
            priced("1000.00", symbol="BBB", score=85.0),
            priced("2000.00", symbol="CCC", score=94.0),
            priced("750.00", symbol="DDD", score=75.0),
        ],
        max_new_positions_per_run=4,
    )

    by_symbol = {d.candidate.symbol: d for d in decisions}

    assert by_symbol["CCC"].outcome is AllocationOutcome.REJECTED
    assert (
        RiskReasonCode.MAX_ALLOCATION_PER_TRADE_EXCEEDED in by_symbol["CCC"].evaluation.reason_codes
    )
    assert by_symbol["AAA"].quantity == 3
    assert by_symbol["AAA"].capital_committed == Decimal("1500.00")
    assert by_symbol["BBB"].quantity == 1
    assert by_symbol["BBB"].capital_committed == Decimal("1000.00")

    assert by_symbol["DDD"].outcome is AllocationOutcome.REJECTED
    assert by_symbol["DDD"].quantity == 0
    assert (
        RiskReasonCode.STRATEGY_CONCENTRATION_EXCEEDED in by_symbol["DDD"].evaluation.reason_codes
    )

    committed = _committed(decisions)
    assert committed == Decimal("2500.00")
    assert committed <= Decimal("4000.00"), "the reserve is never spent"


def test_the_engine_never_spends_the_whole_budget_merely_because_candidates_exist(allocate, priced):
    """The reserve is untouchable however many candidates arrive."""
    decisions = allocate(
        [priced("100.00", symbol=f"SYM{index}", score=95.0 - index) for index in range(20)],
        max_new_positions_per_run=20,
        min_allocation_per_trade=Decimal("0"),
    )

    assert _committed(decisions) <= Decimal("4000.00")
