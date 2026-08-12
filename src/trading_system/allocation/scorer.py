"""Deterministic opportunity scoring.

There will usually be more valid opportunities than budget. Something has to
decide which ones get funded, and the requirement on that something is not that
it be clever — it is that it be *transparent, deterministic, explainable and
testable*, because an allocation nobody can reconstruct is an allocation nobody
can audit.

So this is a weighted sum, and every part of it is visible:

* **Every input is a structured upstream fact.** A confidence *band* from a
  validated research report, a magnitude band, the measured spread, the data
  layer's own usability verdict. Nothing is read from prose, and nothing is
  read from a model's opinion of its own work.
* **Every component is recorded** on the stored decision, alongside the weights
  that were in force, so a total can be recomputed by hand from the record long
  after ``campaign.yaml`` has changed.
* **The weights live in configuration**, sum to one (enforced at load), and are
  the only tuning surface. There is no hidden term.

What this is *not*: a probability, a calibrated expectation, or an input to any
limit. A score decides **order**, never **permission** and never **size**. A
candidate scoring 99 with no budget left gets nothing, and a candidate scoring
71 does not get a larger position than one scoring 70 — it merely gets asked
first. Keeping the score out of the arithmetic is what stops an agent's
confidence leaking into a position size through the back door.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from trading_system.domain.enums import ConfidenceLevel, ExpectedMagnitude
from trading_system.infrastructure.settings import CampaignRankingConfig
from trading_system.risk.models import OpportunityScore

__all__ = ["BAND_VALUE", "MAGNITUDE_VALUE", "score_components", "score_opportunity"]

#: Where each confidence band sits on the 0-100 scale.
#:
#: A band representative, not a probability. No calibration has been measured
#: and none is claimed; the only property anything may rely on is the ordering
#: LOW < MEDIUM < HIGH. The same reasoning governs ``CONFIDENCE_BAND_VALUE`` in
#: the research package, and for the same reason: a decimal from a language
#: model implies a precision nobody has verified.
BAND_VALUE: dict[ConfidenceLevel, float] = {
    ConfidenceLevel.LOW: 40.0,
    ConfidenceLevel.MEDIUM: 70.0,
    ConfidenceLevel.HIGH: 100.0,
}

#: The same treatment for expected magnitude. A larger expected move is a
#: better use of a fixed premium, which is the whole of the reasoning.
MAGNITUDE_VALUE: dict[ExpectedMagnitude, float] = {
    ExpectedMagnitude.SMALL: 40.0,
    ExpectedMagnitude.MODERATE: 70.0,
    ExpectedMagnitude.LARGE: 100.0,
}

#: Absent bands score as the least favourable value rather than as zero or as
#: the middle. An outlook that stated no confidence has not earned the benefit
#: of the doubt, and scoring it zero would be a different claim — that it is
#: worse than a stated LOW, which nothing establishes.
_ABSENT = 40.0


def score_components(
    *,
    research_confidence: ConfidenceLevel | None,
    strategy_confidence: ConfidenceLevel | None,
    expected_magnitude: ExpectedMagnitude | None,
    max_leg_spread_pct: float | None,
    spread_ceiling_pct: float,
    research_usable: bool,
) -> dict[str, float]:
    """Each component on the 0-100 scale, before weighting.

    ``max_leg_spread_pct`` of ``None`` means the spread could not be measured —
    at least one leg was quoted on one side only. That scores as the worst
    case, not as a narrow spread: an unmeasured spread is not a good one, and
    the risk engine has already recorded the check as unevaluated.
    """
    spread_quality = 0.0
    if max_leg_spread_pct is not None and spread_ceiling_pct > 0:
        ratio = min(max_leg_spread_pct / spread_ceiling_pct, 1.0)
        spread_quality = max(0.0, 100.0 * (1.0 - ratio))
    elif max_leg_spread_pct is not None:
        # A ceiling of zero admits only a zero spread; anything wider is worst
        # case, and a zero spread is a perfect one.
        spread_quality = 100.0 if max_leg_spread_pct == 0 else 0.0

    return {
        "research_confidence": (
            BAND_VALUE[research_confidence] if research_confidence else _ABSENT
        ),
        "strategy_confidence": (
            BAND_VALUE[strategy_confidence] if strategy_confidence else _ABSENT
        ),
        "expected_magnitude": (
            MAGNITUDE_VALUE[expected_magnitude] if expected_magnitude else _ABSENT
        ),
        "spread_quality": spread_quality,
        "data_quality": 100.0 if research_usable else 0.0,
    }


def score_opportunity(
    ranking: CampaignRankingConfig,
    *,
    research_confidence: ConfidenceLevel | None,
    strategy_confidence: ConfidenceLevel | None,
    expected_magnitude: ExpectedMagnitude | None,
    max_leg_spread_pct: float | None,
    spread_ceiling_pct: float,
    research_usable: bool,
) -> OpportunityScore:
    """The weighted total, with every component and weight recorded.

    The total is computed in :class:`~decimal.Decimal` and quantised to two
    places with banker's rounding before being returned as a float. That is
    fussier than it looks: an ordering that depended on the last bits of a
    binary float would put two candidates in a different order on a different
    machine, and "deterministic allocation" would quietly stop being true.
    """
    components = score_components(
        research_confidence=research_confidence,
        strategy_confidence=strategy_confidence,
        expected_magnitude=expected_magnitude,
        max_leg_spread_pct=max_leg_spread_pct,
        spread_ceiling_pct=spread_ceiling_pct,
        research_usable=research_usable,
    )
    weights = {
        "research_confidence": ranking.research_confidence_weight,
        "strategy_confidence": ranking.strategy_confidence_weight,
        "expected_magnitude": ranking.expected_magnitude_weight,
        "spread_quality": ranking.spread_quality_weight,
        "data_quality": ranking.data_quality_weight,
    }

    total = sum(
        (Decimal(str(components[name])) * Decimal(str(weight)) for name, weight in weights.items()),
        Decimal("0"),
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    # Guard the 0-100 contract against a rounding artifact at the very top.
    bounded = min(max(total, Decimal("0")), Decimal("100"))

    return OpportunityScore(
        total=float(bounded),
        research_confidence=components["research_confidence"],
        strategy_confidence=components["strategy_confidence"],
        expected_magnitude=components["expected_magnitude"],
        spread_quality=components["spread_quality"],
        data_quality=components["data_quality"],
        weights=weights,
    )
