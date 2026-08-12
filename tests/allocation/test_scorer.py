"""Deterministic opportunity scoring (brief sections 5, 24).

The score decides **order** and nothing else. Three claims:

* it is computed from structured upstream facts and configured weights, with
  every component recorded, so a total can be recomputed by hand;
* it is deterministic in the strict sense — the same inputs give the same
  float, on any machine, because the arithmetic is decimal;
* an *absent* band scores as the least favourable value, never as zero and
  never as the benefit of the doubt. Those are three different claims and only
  one of them is honest.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from trading_system.allocation.scorer import BAND_VALUE, MAGNITUDE_VALUE, score_opportunity
from trading_system.domain.enums import ConfidenceLevel, ExpectedMagnitude
from trading_system.infrastructure.settings import CampaignRankingConfig
from trading_system.risk.models import OpportunityScore

pytestmark = pytest.mark.unit


@pytest.fixture
def ranking(system_config) -> CampaignRankingConfig:
    config: CampaignRankingConfig = system_config.campaign.ranking
    return config


def _score(ranking: CampaignRankingConfig, **overrides: Any) -> OpportunityScore:
    fields: dict[str, Any] = {
        "research_confidence": ConfidenceLevel.MEDIUM,
        "strategy_confidence": ConfidenceLevel.MEDIUM,
        "expected_magnitude": ExpectedMagnitude.MODERATE,
        "max_leg_spread_pct": 1.67,
        "spread_ceiling_pct": 10.0,
        "research_usable": True,
    }
    fields.update(overrides)
    return score_opportunity(ranking, **fields)


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------
def test_the_bands_are_ordered(ranking):
    """The only property anything may rely on. It is not a probability."""
    assert BAND_VALUE[ConfidenceLevel.LOW] < BAND_VALUE[ConfidenceLevel.MEDIUM]
    assert BAND_VALUE[ConfidenceLevel.MEDIUM] < BAND_VALUE[ConfidenceLevel.HIGH]
    assert MAGNITUDE_VALUE[ExpectedMagnitude.SMALL] < MAGNITUDE_VALUE[ExpectedMagnitude.LARGE]


def test_higher_confidence_scores_higher(ranking):
    low = _score(ranking, research_confidence=ConfidenceLevel.LOW)
    high = _score(ranking, research_confidence=ConfidenceLevel.HIGH)

    assert high.total > low.total


def test_an_absent_band_scores_as_the_least_favourable_not_as_zero(ranking):
    """A stated LOW and an unstated confidence are not the same claim.

    Scoring the unknown as zero would assert it is *worse* than a stated LOW,
    which nothing establishes.
    """
    absent = _score(ranking, research_confidence=None)
    low = _score(ranking, research_confidence=ConfidenceLevel.LOW)

    assert absent.research_confidence == low.research_confidence
    assert absent.research_confidence > 0.0


def test_an_unmeasured_spread_scores_as_the_worst_case(ranking):
    """An unmeasured spread is not a narrow one."""
    unmeasured = _score(ranking, max_leg_spread_pct=None)

    assert unmeasured.spread_quality == 0.0


def test_a_tight_spread_scores_near_the_top(ranking):
    tight = _score(ranking, max_leg_spread_pct=0.0)
    wide = _score(ranking, max_leg_spread_pct=10.0)

    assert tight.spread_quality == 100.0
    assert wide.spread_quality == 0.0


def test_a_spread_beyond_the_ceiling_does_not_score_negative(ranking):
    beyond = _score(ranking, max_leg_spread_pct=500.0)

    assert beyond.spread_quality == 0.0
    assert beyond.total >= 0.0


def test_unusable_data_scores_zero_on_that_component(ranking):
    unusable = _score(ranking, research_usable=False)

    assert unusable.data_quality == 0.0


# ---------------------------------------------------------------------------
# The total
# ---------------------------------------------------------------------------
def test_the_total_can_be_recomputed_by_hand(ranking):
    """The whole point of recording the components and the weights."""
    score = _score(ranking)

    expected = sum(
        (
            Decimal(str(getattr(score, name))) * Decimal(str(weight))
            for name, weight in score.weights.items()
        ),
        Decimal("0"),
    )

    assert Decimal(str(score.total)) == expected.quantize(Decimal("0.01"))


def test_the_weights_in_force_are_recorded_on_the_score(ranking):
    score = _score(ranking)

    assert score.weights["research_confidence"] == ranking.research_confidence_weight
    assert sum(score.weights.values()) == pytest.approx(1.0)


def test_the_total_stays_inside_zero_to_one_hundred(ranking):
    best = _score(
        ranking,
        research_confidence=ConfidenceLevel.HIGH,
        strategy_confidence=ConfidenceLevel.HIGH,
        expected_magnitude=ExpectedMagnitude.LARGE,
        max_leg_spread_pct=0.0,
    )
    worst = _score(
        ranking,
        research_confidence=None,
        strategy_confidence=None,
        expected_magnitude=None,
        max_leg_spread_pct=None,
        research_usable=False,
    )

    assert best.total == 100.0
    assert 0.0 <= worst.total <= 100.0


def test_scoring_is_deterministic(ranking):
    assert _score(ranking).model_dump() == _score(ranking).model_dump()


def test_a_medium_medium_candidate_with_a_tight_spread_clears_the_shipped_floor(
    ranking, system_config
):
    """The shipped floor has to admit an ordinary good candidate.

    A threshold nothing can reach is not a policy, it is an outage — and a
    threshold everything clears is not a policy either. This pins the shipped
    combination so a weight change that quietly closes the funnel fails here.
    """
    score = _score(ranking)

    assert score.total >= system_config.campaign.min_opportunity_score


def test_a_weak_candidate_falls_below_the_shipped_floor(ranking, system_config):
    score = _score(
        ranking,
        research_confidence=ConfidenceLevel.LOW,
        strategy_confidence=ConfidenceLevel.LOW,
        expected_magnitude=ExpectedMagnitude.SMALL,
    )

    assert score.total < system_config.campaign.min_opportunity_score
