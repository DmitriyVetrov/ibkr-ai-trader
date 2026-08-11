"""Deterministic semantic validation (brief sections 13, 28, 29, 48, 49, 56, 57).

The prompt asks the agent to stay inside its boundaries; this is what enforces
them. Each test here describes a way a well-formed, schema-valid response can
still be unacceptable, and asserts that the whole report is rejected rather
than repaired — because a repaired outlook would be stored as the model's own.

The A/D distinction gets its own section. It is the one semantic rule the brief
singles out, and it is the one a plausible-looking change is most likely to
erase.
"""

from __future__ import annotations

import pytest

from trading_system.domain.enums import (
    ConfidenceLevel,
    Direction,
    EvidenceDirection,
    EvidenceKind,
    EvidenceStance,
    ExpectedMagnitude,
    MarketHypothesis,
    RelevanceLevel,
    ResearchDataGap,
    RiskCategory,
    SourceTier,
)
from trading_system.research.models import (
    AgentEventAssessment,
    AgentEvidenceAssessment,
    Catalyst,
    EventItem,
    EvidenceItem,
    InvalidationCondition,
    ResearchAgentOutput,
    ResearchDataQualitySummary,
    ResearchHorizon,
    ResearchInput,
    ResearchLimitsSnapshot,
    ResearchSourcePolicySnapshot,
    ResearchWindowSnapshot,
    RiskAssessment,
    SourceProvenance,
)
from trading_system.research.validation import (
    ResearchOutputInvalidError,
    validate_agent_output,
)

from .conftest import RESEARCH_NOW

pytestmark = pytest.mark.unit

RUN_ID = "research-validation-test"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _provenance(
    *, tier: SourceTier = SourceTier.TIER_2, name: str = "Reuters", snapshot: str = "snap-1"
) -> SourceProvenance:
    return SourceProvenance(
        provider="FIXTURE_NEWS",
        source_tier=tier,
        retrieved_at=RESEARCH_NOW,
        snapshot_id=snapshot,
        source_name=name,
        source_identifier=f"https://example.test/{snapshot}",
        published_at=RESEARCH_NOW,
    )


def _evidence(
    evidence_id: str,
    *,
    kind: EvidenceKind = EvidenceKind.NEWS,
    tier: SourceTier = SourceTier.TIER_2,
    name: str = "Reuters",
    usable: bool = True,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        kind=kind,
        summary=f"A fact identified as {evidence_id}",
        source=_provenance(tier=tier, name=name, snapshot=f"snap-{evidence_id}"),
        occurred_at=RESEARCH_NOW,
        research_usable=usable,
    )


def _event(
    event_id: str = "ev-earnings",
    *,
    announced: bool = True,
    within_horizon: bool = True,
) -> EventItem:
    from datetime import timedelta

    from trading_system.domain.enums import MarketEventType

    return EventItem(
        event_id=event_id,
        event_type=MarketEventType.EARNINGS,
        summary="Quarterly results",
        expected_event_time=RESEARCH_NOW + timedelta(days=17 if within_horizon else 90),
        source=_provenance(tier=SourceTier.TIER_1, name="Company investor relations"),
        announced_at=RESEARCH_NOW - timedelta(days=4) if announced else None,
        confirmed=True,
        days_until=17 if within_horizon else 90,
        within_horizon=within_horizon,
    )


def _input(
    *,
    evidence: list[EvidenceItem] | None = None,
    events: list[EventItem] | None = None,
    research_usable: bool = True,
    gaps: list[ResearchDataGap] | None = None,
    min_sources: int = 1,
) -> ResearchInput:
    items = evidence if evidence is not None else [_evidence("ev-1"), _evidence("ev-2")]
    return ResearchInput(
        run_id=RUN_ID,
        symbol="NVDA",
        as_of=RESEARCH_NOW,
        horizon=ResearchHorizon(min_days=14, max_days=31),
        news=items,
        events=events or [],
        data_quality_summary=ResearchDataQualitySummary(
            research_usable=research_usable,
            records_considered=len(items),
            records_research_usable=len(items),
            gaps=gaps or [],
        ),
        window=ResearchWindowSnapshot(
            news_lookback_days=14,
            event_lookahead_days=45,
            event_lookback_days=14,
            historical_lookback_days=90,
            fundamentals_lookback_days=400,
            regulatory_lookback_days=120,
            volatility_annualization_days=252,
        ),
        limits=ResearchLimitsSnapshot(
            max_evidence_items=40,
            max_news_items=25,
            max_events=15,
            max_regulatory_items=10,
            max_fundamental_periods=4,
        ),
        source_policy=ResearchSourcePolicySnapshot(
            config_version="test-sources", min_sources_per_report=min_sources
        ),
    )


def _assessment(
    evidence_id: str,
    *,
    direction: EvidenceDirection = EvidenceDirection.SUPPORTS_UP,
    stance: EvidenceStance = EvidenceStance.SUPPORTS,
) -> AgentEvidenceAssessment:
    return AgentEvidenceAssessment(
        evidence_id=evidence_id,
        claim=f"Reading of {evidence_id}",
        direction=direction,
        stance=stance,
        relevance=RelevanceLevel.HIGH,
        confidence=ConfidenceLevel.MEDIUM,
    )


def _output(**overrides: object) -> ResearchAgentOutput:
    payload: dict[str, object] = {
        "run_id": RUN_ID,
        "symbol": "NVDA",
        "hypothesis": MarketHypothesis.B,
        "confidence": ConfidenceLevel.MEDIUM,
        "direction": Direction.BULLISH,
        "expected_magnitude": ExpectedMagnitude.MODERATE,
        "horizon_days": 21,
        "thesis": "Demand is accelerating.",
        "expected_behavior": "A gradual drift higher over the horizon.",
        "evidence": [_assessment("ev-1")],
        "key_events": [],
        "bullish_catalysts": [],
        "bearish_catalysts": [],
        "risks": [
            RiskAssessment(category=RiskCategory.EVENT_RISK, description="Results could miss.")
        ],
        "invalidation_conditions": [
            InvalidationCondition(condition="Guidance is cut.", observable="Issuer guidance.")
        ],
    }
    payload.update(overrides)
    return ResearchAgentOutput.model_validate(payload)


def _validate(output: ResearchAgentOutput, research_input: ResearchInput, config) -> None:
    validate_agent_output(output, research_input, config=config.research)


def _codes(exc: ResearchOutputInvalidError) -> set[str]:
    return set(exc.codes)


# ---------------------------------------------------------------------------
# 48. Source provenance: a fabricated id must fail
# ---------------------------------------------------------------------------
def test_a_fabricated_evidence_id_is_rejected(make_research_config) -> None:
    """The single most dangerous failure available to a research agent."""
    config = make_research_config()

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(evidence=[_assessment("ev-invented")]), _input(), config)

    assert "FABRICATED_EVIDENCE" in _codes(caught.value)


def test_a_fabricated_event_id_is_rejected(make_research_config) -> None:
    config = make_research_config()
    output = _output(
        hypothesis=MarketHypothesis.D,
        direction=Direction.UNCERTAIN,
        key_events=[
            AgentEventAssessment(
                event_id="ev-invented-event",
                expected_relevance=RelevanceLevel.HIGH,
                directional_uncertainty=True,
            )
        ],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(events=[_event()]), config)

    assert "FABRICATED_EVENT" in _codes(caught.value)


def test_a_fabricated_id_inside_a_catalyst_is_rejected(make_research_config) -> None:
    """Every citation is checked, not only the evidence list."""
    config = make_research_config()
    output = _output(
        bullish_catalysts=[
            Catalyst(summary="Something good", evidence_ids=["ev-invented"], support="SUPPORTED")
        ]
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "FABRICATED_EVIDENCE" in _codes(caught.value)


def test_a_real_evidence_id_passes(make_research_config) -> None:
    _validate(_output(), _input(), make_research_config())


# ---------------------------------------------------------------------------
# 28 and 49. Hypothesis semantics
# ---------------------------------------------------------------------------
def test_b_requires_upward_evidence(make_research_config) -> None:
    config = make_research_config()
    output = _output(evidence=[_assessment("ev-1", direction=EvidenceDirection.NEUTRAL)])

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "UNSUPPORTED_HYPOTHESIS_B" in _codes(caught.value)


def test_c_requires_downward_evidence(make_research_config) -> None:
    config = make_research_config()
    output = _output(
        hypothesis=MarketHypothesis.C,
        direction=Direction.BEARISH,
        evidence=[_assessment("ev-1", direction=EvidenceDirection.SUPPORTS_UP)],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "UNSUPPORTED_HYPOTHESIS_C" in _codes(caught.value)


def test_c_with_downward_evidence_passes(make_research_config) -> None:
    output = _output(
        hypothesis=MarketHypothesis.C,
        direction=Direction.BEARISH,
        evidence=[_assessment("ev-1", direction=EvidenceDirection.SUPPORTS_DOWN)],
    )

    _validate(output, _input(), make_research_config())


def test_a_requires_evidence_of_an_elevated_move(make_research_config) -> None:
    """Uncertainty about direction is not by itself a reason to expect a move."""
    config = make_research_config()
    output = _output(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        evidence=[_assessment("ev-1", direction=EvidenceDirection.NEUTRAL)],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "UNSUPPORTED_HYPOTHESIS_A" in _codes(caught.value)


def test_a_with_large_move_evidence_passes(make_research_config) -> None:
    output = _output(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        evidence=[
            _assessment(
                "ev-vol",
                direction=EvidenceDirection.SUPPORTS_LARGE_MOVE,
            )
        ],
    )
    research_input = _input(evidence=[_evidence("ev-vol", kind=EvidenceKind.OPTION_MARKET)])

    _validate(output, research_input, make_research_config())


def test_d_requires_a_specific_event(make_research_config) -> None:
    """Without an identified event this is normal volatility, which is A."""
    config = make_research_config()
    output = _output(
        hypothesis=MarketHypothesis.D,
        direction=Direction.UNCERTAIN,
        key_events=[],
        evidence=[_assessment("ev-1", direction=EvidenceDirection.SUPPORTS_LARGE_MOVE)],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "HYPOTHESIS_D_WITHOUT_EVENT" in _codes(caught.value)


def test_d_with_a_real_event_passes(make_research_config) -> None:
    output = _output(
        hypothesis=MarketHypothesis.D,
        direction=Direction.UNCERTAIN,
        evidence=[_assessment("ev-1", direction=EvidenceDirection.SUPPORTS_LARGE_MOVE)],
        key_events=[
            AgentEventAssessment(
                event_id="ev-earnings",
                expected_relevance=RelevanceLevel.HIGH,
                directional_uncertainty=True,
            )
        ],
    )

    _validate(output, _input(events=[_event()]), make_research_config())


def test_e_requires_an_explanation(make_research_config) -> None:
    """Enforced by the model itself: 'other' with no reason is not an outlook."""
    with pytest.raises(ValueError, match="hypothesis E requires"):
        _output(
            hypothesis=MarketHypothesis.E,
            direction=Direction.NEUTRAL,
            explanation=None,
            evidence=[_assessment("ev-1", direction=EvidenceDirection.NEUTRAL)],
        )


def test_e_with_an_explanation_passes(make_research_config) -> None:
    output = _output(
        hypothesis=MarketHypothesis.E,
        direction=Direction.NEUTRAL,
        explanation="Sources conflict and the data quality does not resolve them.",
        evidence=[_assessment("ev-1", direction=EvidenceDirection.NEUTRAL)],
    )

    _validate(output, _input(), make_research_config())


# ---------------------------------------------------------------------------
# 4 and 13. A is not D
# ---------------------------------------------------------------------------
def test_a_may_not_rest_only_on_a_dated_event(make_research_config) -> None:
    """An expected move that hangs on an announcement is D, not A."""
    config = make_research_config()
    output = _output(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        evidence=[_assessment("ev-event", direction=EvidenceDirection.SUPPORTS_LARGE_MOVE)],
    )
    research_input = _input(evidence=[_evidence("ev-event", kind=EvidenceKind.CORPORATE_EVENT)])

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, research_input, config)

    assert "HYPOTHESIS_A_RESTS_ON_AN_EVENT" in _codes(caught.value)


def test_a_may_not_name_a_material_event_inside_the_horizon(make_research_config) -> None:
    config = make_research_config()
    output = _output(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        evidence=[_assessment("ev-vol", direction=EvidenceDirection.SUPPORTS_LARGE_MOVE)],
        key_events=[
            AgentEventAssessment(
                event_id="ev-earnings",
                expected_relevance=RelevanceLevel.HIGH,
                directional_uncertainty=True,
            )
        ],
    )
    research_input = _input(
        evidence=[_evidence("ev-vol", kind=EvidenceKind.OPTION_MARKET)],
        events=[_event()],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, research_input, config)

    assert "HYPOTHESIS_A_NAMES_A_MATERIAL_EVENT" in _codes(caught.value)


def test_a_may_mention_a_low_relevance_event(make_research_config) -> None:
    """The rule targets a catalyst thesis wearing an A label, not any mention."""
    output = _output(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        evidence=[_assessment("ev-vol", direction=EvidenceDirection.SUPPORTS_LARGE_MOVE)],
        key_events=[
            AgentEventAssessment(
                event_id="ev-earnings",
                expected_relevance=RelevanceLevel.LOW,
                directional_uncertainty=True,
            )
        ],
    )
    research_input = _input(
        evidence=[_evidence("ev-vol", kind=EvidenceKind.OPTION_MARKET)],
        events=[_event()],
    )

    _validate(output, research_input, make_research_config())


# ---------------------------------------------------------------------------
# 57. Mandatory event fields for D
# ---------------------------------------------------------------------------
def test_d_fails_when_the_event_has_no_announcement_time(make_research_config) -> None:
    """Nothing establishes that an unannounced event was knowable at T."""
    config = make_research_config()
    output = _output(
        hypothesis=MarketHypothesis.D,
        direction=Direction.UNCERTAIN,
        evidence=[_assessment("ev-1", direction=EvidenceDirection.SUPPORTS_LARGE_MOVE)],
        key_events=[
            AgentEventAssessment(
                event_id="ev-earnings",
                expected_relevance=RelevanceLevel.HIGH,
                directional_uncertainty=True,
            )
        ],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(events=[_event(announced=False)]), config)

    assert "INCOMPLETE_EVENT_FOR_HYPOTHESIS_D" in _codes(caught.value)


def test_d_fails_when_the_event_falls_outside_the_horizon(make_research_config) -> None:
    config = make_research_config()
    output = _output(
        hypothesis=MarketHypothesis.D,
        direction=Direction.UNCERTAIN,
        evidence=[_assessment("ev-1", direction=EvidenceDirection.SUPPORTS_LARGE_MOVE)],
        key_events=[
            AgentEventAssessment(
                event_id="ev-earnings",
                expected_relevance=RelevanceLevel.HIGH,
                directional_uncertainty=True,
            )
        ],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(events=[_event(within_horizon=False)]), config)

    assert "HYPOTHESIS_D_EVENT_OUTSIDE_HORIZON" in _codes(caught.value)


# ---------------------------------------------------------------------------
# Direction agrees with the hypothesis
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("hypothesis", "direction"),
    [
        (MarketHypothesis.B, Direction.BEARISH),
        (MarketHypothesis.C, Direction.BULLISH),
        (MarketHypothesis.A, Direction.BULLISH),
        (MarketHypothesis.D, Direction.BEARISH),
    ],
)
def test_a_direction_contradicting_the_hypothesis_is_rejected(
    hypothesis: MarketHypothesis, direction: Direction, make_research_config
) -> None:
    config = make_research_config()
    output = _output(
        hypothesis=hypothesis,
        direction=direction,
        evidence=[
            _assessment("ev-1", direction=EvidenceDirection.SUPPORTS_UP),
            _assessment("ev-2", direction=EvidenceDirection.SUPPORTS_DOWN),
        ],
        key_events=[
            AgentEventAssessment(
                event_id="ev-earnings",
                expected_relevance=RelevanceLevel.MEDIUM,
                directional_uncertainty=True,
            )
        ],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(events=[_event()]), config)

    assert "DIRECTION_CONTRADICTS_HYPOTHESIS" in _codes(caught.value)


# ---------------------------------------------------------------------------
# 8. Horizon
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("days", [1, 13, 32, 365])
def test_an_outlook_outside_the_configured_horizon_is_rejected(
    days: int, make_research_config
) -> None:
    """A long-term thesis is not an answer to a short-term question."""
    config = make_research_config()

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(horizon_days=days), _input(), config)

    assert "HORIZON_OUT_OF_RANGE" in _codes(caught.value)


def test_a_horizon_inside_the_window_passes(make_research_config) -> None:
    _validate(_output(horizon_days=14), _input(), make_research_config())
    _validate(_output(horizon_days=31), _input(), make_research_config())


# ---------------------------------------------------------------------------
# 55-56. Confidence is constrained, not declared
# ---------------------------------------------------------------------------
def test_high_confidence_needs_enough_evidence(make_research_config) -> None:
    config = make_research_config(min_evidence_items_for_high=3)

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(confidence=ConfidenceLevel.HIGH), _input(), config)

    assert "HIGH_CONFIDENCE_TOO_LITTLE_EVIDENCE" in _codes(caught.value)


def test_high_confidence_is_refused_on_unusable_data(make_research_config) -> None:
    config = make_research_config(min_evidence_items_for_high=1)
    research_input = _input(research_usable=False)

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(confidence=ConfidenceLevel.HIGH), research_input, config)

    assert "HIGH_CONFIDENCE_ON_UNUSABLE_DATA" in _codes(caught.value)


def test_high_confidence_is_refused_on_only_low_tier_sources(make_research_config) -> None:
    config = make_research_config(
        min_evidence_items_for_high=1, min_source_tier_for_high=SourceTier.TIER_2
    )
    research_input = _input(evidence=[_evidence("ev-1", tier=SourceTier.TIER_4, name="Some blog")])

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(confidence=ConfidenceLevel.HIGH), research_input, config)

    assert "HIGH_CONFIDENCE_ON_LOW_TIER_SOURCES" in _codes(caught.value)


def test_high_confidence_is_refused_with_too_many_data_gaps(make_research_config) -> None:
    config = make_research_config(min_evidence_items_for_high=1, max_data_gaps_for_high=1)
    research_input = _input(
        gaps=[
            ResearchDataGap.NEWS_UNAVAILABLE,
            ResearchDataGap.EVENTS_UNAVAILABLE,
            ResearchDataGap.IMPLIED_VOLATILITY_UNAVAILABLE,
        ]
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(confidence=ConfidenceLevel.HIGH), research_input, config)

    assert "HIGH_CONFIDENCE_WITH_MISSING_DATA" in _codes(caught.value)


def test_high_confidence_is_refused_over_an_unresolved_contradiction(
    make_research_config,
) -> None:
    config = make_research_config(min_evidence_items_for_high=1)
    output = _output(
        confidence=ConfidenceLevel.HIGH,
        evidence=[
            _assessment("ev-1", direction=EvidenceDirection.SUPPORTS_UP),
            _assessment(
                "ev-2",
                direction=EvidenceDirection.SUPPORTS_DOWN,
                stance=EvidenceStance.CONTRADICTS,
            ),
        ],
        contradiction_resolution=None,
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "HIGH_CONFIDENCE_WITH_UNRESOLVED_CONTRADICTION" in _codes(caught.value)


def test_high_confidence_stands_when_the_contradiction_is_resolved(
    make_research_config,
) -> None:
    config = make_research_config(min_evidence_items_for_high=2)
    output = _output(
        confidence=ConfidenceLevel.HIGH,
        evidence=[
            _assessment("ev-1", direction=EvidenceDirection.SUPPORTS_UP),
            _assessment(
                "ev-2",
                direction=EvidenceDirection.SUPPORTS_DOWN,
                stance=EvidenceStance.CONTRADICTS,
            ),
        ],
        contradiction_resolution="The tier-1 filing outweighs the tier-3 commentary.",
    )

    _validate(output, _input(), config)


def test_confidence_above_low_needs_at_least_one_evidence_item(make_research_config) -> None:
    config = make_research_config(min_evidence_items_for_medium=1)
    output = _output(confidence=ConfidenceLevel.MEDIUM, evidence=[])

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "CONFIDENCE_ABOVE_LOW_WITHOUT_EVIDENCE" in _codes(caught.value)


def test_a_rejected_report_is_never_silently_downgraded(make_research_config) -> None:
    """The validator raises. It has no path that edits and accepts."""
    config = make_research_config(min_evidence_items_for_high=5)
    output = _output(confidence=ConfidenceLevel.HIGH)

    with pytest.raises(ResearchOutputInvalidError):
        _validate(output, _input(), config)

    assert output.confidence is ConfidenceLevel.HIGH, "the object is untouched"


# ---------------------------------------------------------------------------
# 29. Contradictory evidence is preserved
# ---------------------------------------------------------------------------
def test_contradicting_evidence_is_valid_and_kept(make_research_config) -> None:
    output = _output(
        evidence=[
            _assessment("ev-1", direction=EvidenceDirection.SUPPORTS_UP),
            _assessment(
                "ev-2",
                direction=EvidenceDirection.SUPPORTS_DOWN,
                stance=EvidenceStance.CONTRADICTS,
            ),
        ],
        contradiction_resolution="Earnings momentum currently outweighs the valuation concern.",
    )

    _validate(output, _input(), make_research_config())

    assert len(output.contradicting) == 1


# ---------------------------------------------------------------------------
# 10. Unsupported claims are labelled, not deleted
# ---------------------------------------------------------------------------
def test_a_catalyst_with_no_evidence_is_marked_unsupported() -> None:
    catalyst = Catalyst(summary="A hunch", evidence_ids=[])

    assert catalyst.support.value == "UNSUPPORTED"


def test_a_claim_cannot_declare_itself_supported_while_citing_nothing() -> None:
    """The declared value is discarded and recomputed from the citations."""
    assert Catalyst(summary="A hunch", evidence_ids=[], support="SUPPORTED").support.value == (
        "UNSUPPORTED"
    )


def test_an_unsupported_claim_does_not_invalidate_the_report(make_research_config) -> None:
    """Better to see what was asserted without backing than to lose it."""
    output = _output(
        bullish_catalysts=[Catalyst(summary="A hunch with no evidence", evidence_ids=[])]
    )

    _validate(output, _input(), make_research_config())


# ---------------------------------------------------------------------------
# 23. No contract selection, even in prose
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prose",
    [
        "Buy the 680 call before the results.",
        "We should purchase 180 calls into the event.",
        "Sell the 150 puts to fund it.",
    ],
)
def test_a_contract_recommendation_in_prose_is_rejected(prose: str, make_research_config) -> None:
    config = make_research_config()

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(expected_behavior=prose), _input(), config)

    assert "CONTRACT_RECOMMENDED" in _codes(caught.value)


def test_a_strategy_name_in_prose_is_rejected(make_research_config) -> None:
    config = make_research_config()

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(thesis="This suits a LONG_STRADDLE."), _input(), config)

    assert "STRATEGY_RECOMMENDED" in _codes(caught.value)


def test_discussing_implied_volatility_in_the_abstract_is_allowed(
    make_research_config,
) -> None:
    """The guard must not reject the analysis the agent exists to produce."""
    output = _output(
        expected_behavior=(
            "Implied volatility appears elevated relative to the 90-day realized figure, "
            "though the two are measured over different horizons. Expiries cluster around "
            "the results date."
        )
    )

    _validate(output, _input(), make_research_config())


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_a_response_for_another_run_is_rejected(make_research_config) -> None:
    config = make_research_config()

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(run_id="a-different-run"), _input(), config)

    assert "RUN_ID_MISMATCH" in _codes(caught.value)


def test_a_response_about_another_symbol_is_rejected(make_research_config) -> None:
    """Research contexts are isolated per underlying (brief section 33)."""
    config = make_research_config()

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(_output(symbol="AAPL"), _input(), config)

    assert "SYMBOL_MISMATCH" in _codes(caught.value)


def test_the_same_fact_may_not_be_weighed_twice(make_research_config) -> None:
    config = make_research_config()
    output = _output(evidence=[_assessment("ev-1"), _assessment("ev-1")])

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "DUPLICATE_EVIDENCE" in _codes(caught.value)


def test_ignoring_every_supplied_fact_is_rejected(make_research_config) -> None:
    """A conclusion that references nothing cannot be audited."""
    config = make_research_config()
    output = _output(confidence=ConfidenceLevel.LOW, evidence=[])

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    assert "NO_EVIDENCE_ASSESSED" in _codes(caught.value)


# ---------------------------------------------------------------------------
# Every problem is reported, not only the first
# ---------------------------------------------------------------------------
def test_all_violations_are_collected(make_research_config) -> None:
    """An operator debugging a prompt needs the whole picture."""
    config = make_research_config()
    output = _output(
        run_id="wrong-run",
        horizon_days=200,
        evidence=[_assessment("ev-invented", direction=EvidenceDirection.NEUTRAL)],
    )

    with pytest.raises(ResearchOutputInvalidError) as caught:
        _validate(output, _input(), config)

    codes = _codes(caught.value)
    assert {"RUN_ID_MISMATCH", "HORIZON_OUT_OF_RANGE", "FABRICATED_EVIDENCE"} <= codes
