"""The Market Researcher agent (brief sections 46 and 53; specification 24.2).

Every test here uses a deterministic fake model. No test requires an API key,
touches the network, or is skipped for lack of a credential — the agent takes an
:class:`~trading_system.agents.base.LLMClient`, so a fake, a replayed fixture and
a live model are the same code path.

The suite is organised around what the agent must *refuse to do*. Malformed
JSON, a missing hypothesis, an unsupported one, a fabricated source, a
future-dated citation, an unlicensed confidence, a timeout, an unreachable
model — each produces a specific error, and none produces a partially accepted
outlook. The system fails closed.

There is one opt-in test at the end that calls a real model. It is marked
``llm``, skipped unless ``ALLOW_LIVE_TESTS=true``, places no trade, touches no
broker, and asserts only that a real response satisfies the same contract as a
fake one.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from trading_system.agents.base import (
    AgentInvalidOutputError,
    AgentTimeoutError,
    AgentUnavailableError,
    LLMResponse,
    ModelIdentity,
    StructuredRequest,
)
from trading_system.agents.market_researcher import (
    PROMPT_NAME,
    MarketResearcherAgent,
    research_output_schema,
)
from trading_system.domain.enums import (
    ClaimSupport,
    ConfidenceLevel,
    EvidenceDirection,
    EvidenceKind,
    EvidenceStance,
    MarketEventType,
    MarketHypothesis,
    RelevanceLevel,
    SourceTier,
)
from trading_system.research.models import (
    EventItem,
    EvidenceItem,
    ResearchDataQualitySummary,
    ResearchHorizon,
    ResearchInput,
    ResearchLimitsSnapshot,
    ResearchSourcePolicySnapshot,
    ResearchWindowSnapshot,
    SourceProvenance,
)
from trading_system.research.validation import ResearchOutputInvalidError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
RUN_ID = "research-agent-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class ScriptedClient:
    """Returns exactly what a test told it to, or raises exactly what it was given."""

    def __init__(
        self,
        text: str = "",
        *,
        error: Exception | None = None,
        stop_reason: str | None = "end_turn",
    ) -> None:
        self._text = text
        self._error = error
        self._stop_reason = stop_reason
        self.requests: list[StructuredRequest] = []

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity(
            provider="FAKE",
            model_name="fake-model-1",
            prompt_version="test-1.0.0",
            prompt_fingerprint="f" * 32,
        )

    def complete(self, request: StructuredRequest) -> LLMResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return LLMResponse(
            text=self._text,
            identity=self.identity,
            generated_at=NOW,
            stop_reason=self._stop_reason,
        )


def _provenance(
    *,
    snapshot: str = "snap-1",
    tier: SourceTier = SourceTier.TIER_2,
    name: str = "Reuters",
) -> SourceProvenance:
    return SourceProvenance(
        provider="FIXTURE_NEWS",
        source_tier=tier,
        retrieved_at=NOW - timedelta(hours=2),
        snapshot_id=snapshot,
        source_name=name,
        source_identifier=f"https://www.reuters.com/{snapshot}",
        published_at=NOW - timedelta(hours=3),
    )


def _evidence(
    evidence_id: str,
    *,
    kind: EvidenceKind = EvidenceKind.NEWS,
    tier: SourceTier = SourceTier.TIER_2,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        kind=kind,
        summary=f"Retrieved fact {evidence_id}",
        source=_provenance(snapshot=f"snap-{evidence_id}", tier=tier),
        occurred_at=NOW - timedelta(hours=3),
    )


def _event(event_id: str = "evt-earnings", *, announced: bool = True) -> EventItem:
    return EventItem(
        event_id=event_id,
        event_type=MarketEventType.EARNINGS,
        summary="Quarterly results",
        expected_event_time=NOW + timedelta(days=17),
        source=_provenance(tier=SourceTier.TIER_1, name="Company investor relations"),
        announced_at=NOW - timedelta(days=4) if announced else None,
        confirmed=True,
        days_until=17,
        within_horizon=True,
    )


@pytest.fixture
def research_input() -> ResearchInput:
    return ResearchInput(
        run_id=RUN_ID,
        symbol="NVDA",
        as_of=NOW,
        horizon=ResearchHorizon(min_days=14, max_days=31),
        news=[_evidence("ev-news"), _evidence("ev-other")],
        observations=[_evidence("ev-vol", kind=EvidenceKind.OPTION_MARKET)],
        events=[_event()],
        data_quality_summary=ResearchDataQualitySummary(
            research_usable=True, records_considered=3, records_research_usable=3
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
            config_version="test-sources", min_sources_per_report=1
        ),
    )


@pytest.fixture
def config(system_config):
    """The shipped research policy, with a low HIGH-confidence floor.

    Using the real configuration keeps the suite honest about what ships; the
    one override exists so a two-item fixture can still exercise HIGH.
    """
    research = system_config.research
    confidence = research.confidence.model_copy(update={"min_evidence_items_for_high": 2})
    return research.model_copy(update={"confidence": confidence})


def _agent(client: ScriptedClient, config) -> MarketResearcherAgent:
    return MarketResearcherAgent(client, config=config)


def _response(**overrides: object) -> str:
    payload: dict[str, object] = {
        "run_id": RUN_ID,
        "symbol": "NVDA",
        "hypothesis": "B",
        "confidence": "MEDIUM",
        "direction": "BULLISH",
        "expected_magnitude": "MODERATE",
        "horizon_days": 21,
        "thesis": "Demand is accelerating into the next quarter.",
        "expected_behavior": "A gradual drift higher over the horizon.",
        "evidence": [
            {
                "evidence_id": "ev-news",
                "claim": "Reported acceleration supports an upward bias.",
                "direction": "SUPPORTS_UP",
                "stance": "SUPPORTS",
                "relevance": "HIGH",
                "confidence": "MEDIUM",
            }
        ],
        "key_events": [],
        "bullish_catalysts": [],
        "bearish_catalysts": [],
        "risks": [{"category": "EVENT_RISK", "description": "Results could disappoint."}],
        "invalidation_conditions": [
            {"condition": "Guidance is cut.", "observable": "Issuer guidance range."}
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _evidence_entry(
    evidence_id: str = "ev-news",
    *,
    direction: str = "SUPPORTS_UP",
    stance: str = "SUPPORTS",
) -> dict[str, str]:
    return {
        "evidence_id": evidence_id,
        "claim": f"Reading of {evidence_id}.",
        "direction": direction,
        "stance": stance,
        "relevance": "HIGH",
        "confidence": "MEDIUM",
    }


# ---------------------------------------------------------------------------
# 1-5. A valid response for each hypothesis
# ---------------------------------------------------------------------------
def test_a_valid_b_response_is_accepted(research_input, config) -> None:
    outcome = _agent(ScriptedClient(_response()), config).research(research_input)

    assert outcome.output.hypothesis is MarketHypothesis.B
    assert outcome.output.direction.value == "BULLISH"
    assert outcome.output.evidence[0].direction is EvidenceDirection.SUPPORTS_UP


def test_a_valid_c_response_is_accepted(research_input, config) -> None:
    text = _response(
        hypothesis="C",
        direction="BEARISH",
        evidence=[_evidence_entry(direction="SUPPORTS_DOWN")],
    )

    outcome = _agent(ScriptedClient(text), config).research(research_input)

    assert outcome.output.hypothesis is MarketHypothesis.C


def test_a_valid_a_response_is_accepted(research_input, config) -> None:
    """A: elevated move, no catalyst required, evidence that is not an event."""
    text = _response(
        hypothesis="A",
        direction="UNCERTAIN",
        evidence=[_evidence_entry("ev-vol", direction="SUPPORTS_LARGE_MOVE")],
    )

    outcome = _agent(ScriptedClient(text), config).research(research_input)

    assert outcome.output.hypothesis is MarketHypothesis.A
    assert outcome.output.key_events == []


def test_a_valid_d_response_is_accepted(research_input, config) -> None:
    """D: a specific, identified, dated, announced event inside the horizon."""
    text = _response(
        hypothesis="D",
        direction="UNCERTAIN",
        evidence=[_evidence_entry("ev-vol", direction="SUPPORTS_LARGE_MOVE")],
        key_events=[
            {
                "event_id": "evt-earnings",
                "expected_relevance": "HIGH",
                "directional_uncertainty": True,
                "rationale": "Results historically move the underlying sharply.",
            }
        ],
    )

    outcome = _agent(ScriptedClient(text), config).research(research_input)

    assert outcome.output.hypothesis is MarketHypothesis.D
    assert outcome.output.key_events[0].event_id == "evt-earnings"


def test_a_valid_e_response_is_accepted(research_input, config) -> None:
    """Insufficient evidence is a valid answer, not a failure."""
    text = _response(
        hypothesis="E",
        direction="NEUTRAL",
        explanation="Sources conflict and the data quality does not resolve them.",
        evidence=[_evidence_entry(direction="NEUTRAL", stance="NEUTRAL")],
    )

    outcome = _agent(ScriptedClient(text), config).research(research_input)

    assert outcome.output.hypothesis is MarketHypothesis.E
    assert outcome.output.explanation


# ---------------------------------------------------------------------------
# 6-9. Malformed and incomplete output
# ---------------------------------------------------------------------------
def test_malformed_json_is_rejected(research_input, config) -> None:
    with pytest.raises(AgentInvalidOutputError, match="not valid JSON"):
        _agent(ScriptedClient("{not json"), config).research(research_input)


def test_an_empty_response_is_rejected(research_input, config) -> None:
    with pytest.raises(AgentInvalidOutputError, match="empty response"):
        _agent(ScriptedClient("   "), config).research(research_input)


def test_a_json_array_is_rejected(research_input, config) -> None:
    with pytest.raises(AgentInvalidOutputError, match="expected a JSON object"):
        _agent(ScriptedClient("[]"), config).research(research_input)


def test_a_markdown_fence_is_tolerated(research_input, config) -> None:
    """A formatting habit, not a semantic error."""
    text = f"```json\n{_response()}\n```"

    outcome = _agent(ScriptedClient(text), config).research(research_input)

    assert outcome.output.hypothesis is MarketHypothesis.B


def test_a_missing_hypothesis_is_rejected(research_input, config) -> None:
    payload = json.loads(_response())
    del payload["hypothesis"]

    with pytest.raises(AgentInvalidOutputError, match="research contract"):
        _agent(ScriptedClient(json.dumps(payload)), config).research(research_input)


def test_an_unsupported_hypothesis_value_is_rejected(research_input, config) -> None:
    """The vocabulary is closed: there is no hypothesis F."""
    with pytest.raises(AgentInvalidOutputError, match="research contract"):
        _agent(ScriptedClient(_response(hypothesis="F")), config).research(research_input)


def test_an_unsupported_confidence_value_is_rejected(research_input, config) -> None:
    """A band, never a percentage."""
    with pytest.raises(AgentInvalidOutputError, match="research contract"):
        _agent(ScriptedClient(_response(confidence="82%")), config).research(research_input)


def test_a_missing_invalidation_condition_is_rejected(research_input, config) -> None:
    """A thesis that cannot be falsified cannot be monitored."""
    with pytest.raises(AgentInvalidOutputError, match="research contract"):
        _agent(ScriptedClient(_response(invalidation_conditions=[])), config).research(
            research_input
        )


def test_an_unexpected_field_is_rejected(research_input, config) -> None:
    """An extra field is a contract violation, not a bonus."""
    payload = json.loads(_response())
    payload["recommended_strike"] = 180

    with pytest.raises(AgentInvalidOutputError, match="research contract"):
        _agent(ScriptedClient(json.dumps(payload)), config).research(research_input)


# ---------------------------------------------------------------------------
# 10-11 and 16. Missing, unsupported and fabricated evidence
# ---------------------------------------------------------------------------
def test_ignoring_all_supplied_evidence_is_rejected(research_input, config) -> None:
    """A conclusion that references nothing cannot be audited."""
    with pytest.raises(ResearchOutputInvalidError) as caught:
        _agent(ScriptedClient(_response(evidence=[], confidence="LOW")), config).research(
            research_input
        )

    assert "NO_EVIDENCE_ASSESSED" in caught.value.codes


def test_a_fabricated_source_is_rejected(research_input, config) -> None:
    """The single most dangerous failure available to a research agent."""
    text = _response(evidence=[_evidence_entry("ev-this-was-never-supplied")])

    with pytest.raises(ResearchOutputInvalidError, match="never invent one"):
        _agent(ScriptedClient(text), config).research(research_input)


def test_a_fabricated_event_is_rejected(research_input, config) -> None:
    text = _response(
        hypothesis="D",
        direction="UNCERTAIN",
        evidence=[_evidence_entry("ev-vol", direction="SUPPORTS_LARGE_MOVE")],
        key_events=[
            {
                "event_id": "evt-imagined-fda-decision",
                "expected_relevance": "HIGH",
                "directional_uncertainty": True,
            }
        ],
    )

    with pytest.raises(ResearchOutputInvalidError, match="inventing a catalyst"):
        _agent(ScriptedClient(text), config).research(research_input)


def test_an_unsupported_claim_is_labelled_rather_than_rejected(research_input, config) -> None:
    """Better to see what was asserted without backing than to lose it."""
    text = _response(bullish_catalysts=[{"summary": "A hunch about sentiment", "evidence_ids": []}])

    outcome = _agent(ScriptedClient(text), config).research(research_input)

    assert outcome.output.bullish_catalysts[0].support is ClaimSupport.UNSUPPORTED


def test_a_claim_cannot_declare_support_it_does_not_have(research_input, config) -> None:
    text = _response(
        bullish_catalysts=[{"summary": "A hunch", "evidence_ids": [], "support": "SUPPORTED"}]
    )

    outcome = _agent(ScriptedClient(text), config).research(research_input)

    assert outcome.output.bullish_catalysts[0].support is ClaimSupport.UNSUPPORTED


# ---------------------------------------------------------------------------
# 12-15. Hypotheses without the evidence they claim
# ---------------------------------------------------------------------------
def test_d_without_an_event_is_rejected(research_input, config) -> None:
    text = _response(
        hypothesis="D",
        direction="UNCERTAIN",
        evidence=[_evidence_entry(direction="SUPPORTS_LARGE_MOVE")],
        key_events=[],
    )

    with pytest.raises(ResearchOutputInvalidError, match="belongs to hypothesis A"):
        _agent(ScriptedClient(text), config).research(research_input)


def test_d_with_an_unannounced_event_is_rejected(config) -> None:
    """Nothing established that an unannounced event was knowable at T."""
    unannounced = ResearchInput(
        run_id=RUN_ID,
        symbol="NVDA",
        as_of=NOW,
        horizon=ResearchHorizon(min_days=14, max_days=31),
        news=[_evidence("ev-news")],
        events=[_event(announced=False)],
        data_quality_summary=ResearchDataQualitySummary(research_usable=True),
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
        source_policy=ResearchSourcePolicySnapshot(config_version="test-sources"),
    )
    text = _response(
        hypothesis="D",
        direction="UNCERTAIN",
        evidence=[_evidence_entry(direction="SUPPORTS_LARGE_MOVE")],
        key_events=[
            {
                "event_id": "evt-earnings",
                "expected_relevance": "HIGH",
                "directional_uncertainty": True,
            }
        ],
    )

    with pytest.raises(ResearchOutputInvalidError, match="announced_at"):
        _agent(ScriptedClient(text), config).research(unannounced)


def test_b_without_upward_evidence_is_rejected(research_input, config) -> None:
    text = _response(evidence=[_evidence_entry(direction="NEUTRAL")])

    with pytest.raises(ResearchOutputInvalidError, match="hypothesis B"):
        _agent(ScriptedClient(text), config).research(research_input)


def test_c_without_downward_evidence_is_rejected(research_input, config) -> None:
    text = _response(
        hypothesis="C", direction="BEARISH", evidence=[_evidence_entry(direction="SUPPORTS_UP")]
    )

    with pytest.raises(ResearchOutputInvalidError, match="hypothesis C"):
        _agent(ScriptedClient(text), config).research(research_input)


def test_a_without_movement_evidence_is_rejected(research_input, config) -> None:
    text = _response(
        hypothesis="A", direction="UNCERTAIN", evidence=[_evidence_entry(direction="NEUTRAL")]
    )

    with pytest.raises(ResearchOutputInvalidError, match="hypothesis A"):
        _agent(ScriptedClient(text), config).research(research_input)


def test_a_that_is_really_d_is_rejected(research_input, config) -> None:
    """Brief section 4: the A/D distinction is enforced, not merely described."""
    text = _response(
        hypothesis="A",
        direction="UNCERTAIN",
        evidence=[_evidence_entry("ev-vol", direction="SUPPORTS_LARGE_MOVE")],
        key_events=[
            {
                "event_id": "evt-earnings",
                "expected_relevance": "HIGH",
                "directional_uncertainty": True,
            }
        ],
    )

    with pytest.raises(ResearchOutputInvalidError, match="hypothesis D"):
        _agent(ScriptedClient(text), config).research(research_input)


# ---------------------------------------------------------------------------
# 17. Future-dated evidence
# ---------------------------------------------------------------------------
def test_future_dated_evidence_cannot_be_cited_because_it_was_never_supplied(
    research_input, config
) -> None:
    """Point-in-time filtering happens upstream, so the id simply does not exist.

    That is the strong form of the guarantee: the agent is not asked to ignore
    tomorrow's news, it is never shown it, and a citation of one is
    indistinguishable from a fabrication — which is exactly how it is treated.
    """
    text = _response(evidence=[_evidence_entry("ev-tomorrows-headline")])

    with pytest.raises(ResearchOutputInvalidError, match="never invent one"):
        _agent(ScriptedClient(text), config).research(research_input)


def test_every_supplied_fact_predates_the_research_instant(research_input) -> None:
    for item in research_input.all_evidence:
        assert item.source.retrieved_at <= research_input.as_of


# ---------------------------------------------------------------------------
# 18-19. Timeout and unavailability
# ---------------------------------------------------------------------------
def test_a_timeout_surfaces_as_unavailable(research_input, config) -> None:
    client = ScriptedClient(error=AgentTimeoutError("no answer within 180s"))

    with pytest.raises(AgentTimeoutError):
        _agent(client, config).research(research_input)


def test_an_unreachable_model_surfaces_as_unavailable(research_input, config) -> None:
    client = ScriptedClient(error=AgentUnavailableError("connection refused"))

    with pytest.raises(AgentUnavailableError):
        _agent(client, config).research(research_input)


def test_a_refusal_is_not_parsed_as_an_outlook(research_input, config) -> None:
    client = ScriptedClient("", stop_reason="refusal")

    with pytest.raises(AgentUnavailableError, match="no usable answer"):
        _agent(client, config).research(research_input)


def test_a_truncated_generation_is_not_parsed_as_an_outlook(research_input, config) -> None:
    client = ScriptedClient(_response(), stop_reason="max_tokens")

    with pytest.raises(AgentUnavailableError, match="no usable answer"):
        _agent(client, config).research(research_input)


def test_a_stale_response_for_another_run_is_rejected(research_input, config) -> None:
    with pytest.raises(ResearchOutputInvalidError, match="run"):
        _agent(ScriptedClient(_response(run_id="a-different-run")), config).research(research_input)


def test_a_response_about_another_symbol_is_rejected(research_input, config) -> None:
    """Research contexts are isolated per underlying."""
    with pytest.raises(ResearchOutputInvalidError, match="isolated per underlying"):
        _agent(ScriptedClient(_response(symbol="AAPL")), config).research(research_input)


# ---------------------------------------------------------------------------
# 20. Contradictory evidence
# ---------------------------------------------------------------------------
def test_contradictory_evidence_is_preserved(research_input, config) -> None:
    """Disagreement is kept, never quietly dropped."""
    text = _response(
        evidence=[
            _evidence_entry("ev-news", direction="SUPPORTS_UP"),
            _evidence_entry("ev-other", direction="SUPPORTS_DOWN", stance="CONTRADICTS"),
        ],
        contradiction_resolution="Earnings momentum currently outweighs the valuation concern.",
    )

    outcome = _agent(ScriptedClient(text), config).research(research_input)

    assert len(outcome.output.evidence) == 2
    assert len(outcome.output.contradicting) == 1
    assert outcome.output.contradiction_resolution


def test_high_confidence_over_an_unresolved_contradiction_is_rejected(
    research_input, config
) -> None:
    text = _response(
        confidence="HIGH",
        evidence=[
            _evidence_entry("ev-news", direction="SUPPORTS_UP"),
            _evidence_entry("ev-other", direction="SUPPORTS_DOWN", stance="CONTRADICTS"),
        ],
    )

    with pytest.raises(ResearchOutputInvalidError, match="unexplained"):
        _agent(ScriptedClient(text), config).research(research_input)


# ---------------------------------------------------------------------------
# The request the agent actually sends
# ---------------------------------------------------------------------------
def test_the_request_carries_the_input_contract_and_nothing_else(research_input, config) -> None:
    client = ScriptedClient(_response())

    _agent(client, config).research(research_input)

    payload = json.loads(client.requests[0].user_content)
    assert payload["symbol"] == "NVDA"
    assert payload["run_id"] == RUN_ID
    assert "news" in payload
    assert "repository" not in payload
    assert "broker" not in json.dumps(payload).lower()


def test_the_request_uses_the_shipped_prompt(research_input, config) -> None:
    from trading_system.agents.prompts import load_prompt

    client = ScriptedClient(_response())

    _agent(client, config).research(research_input)

    assert client.requests[0].system_prompt == load_prompt(PROMPT_NAME)


def test_an_oversized_input_fails_rather_than_being_truncated(research_input, config) -> None:
    tight = config.model_copy(
        update={"limits": config.limits.model_copy(update={"max_input_characters": 1000})}
    )
    client = ScriptedClient(_response())

    with pytest.raises(AgentInvalidOutputError, match="not truncated"):
        MarketResearcherAgent(client, config=tight).research(research_input)

    assert client.requests == [], "the model was never called"


def test_latency_is_recorded_when_the_client_does_not_report_it(research_input, config) -> None:
    outcome = _agent(ScriptedClient(_response()), config).research(research_input)

    assert outcome.response.latency_ms is not None
    assert outcome.response.latency_ms >= 0


# ---------------------------------------------------------------------------
# The generation schema stays in step with the vocabulary
# ---------------------------------------------------------------------------
def test_the_generation_schema_is_a_valid_json_schema() -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(research_output_schema())


@pytest.mark.parametrize(
    ("field", "members"),
    [
        ("hypothesis", MarketHypothesis),
        ("confidence", ConfidenceLevel),
    ],
)
def test_the_generation_schema_offers_the_whole_vocabulary(field: str, members) -> None:
    """The offered vocabulary and the accepted vocabulary are the same object."""
    offered = research_output_schema()["properties"][field]["enum"]

    assert set(offered) == {member.value for member in members}


def test_the_generation_schema_offers_both_evidence_axes() -> None:
    evidence = research_output_schema()["properties"]["evidence"]["items"]["properties"]

    assert set(evidence["direction"]["enum"]) == {d.value for d in EvidenceDirection}
    assert set(evidence["stance"]["enum"]) == {s.value for s in EvidenceStance}
    assert set(evidence["relevance"]["enum"]) == {r.value for r in RelevanceLevel}


def test_the_generation_schema_forbids_unexpected_fields() -> None:
    schema = research_output_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["evidence"]["items"]["additionalProperties"] is False


def test_the_generation_schema_requires_an_invalidation_condition() -> None:
    schema = research_output_schema()

    assert "invalidation_conditions" in schema["required"]
    assert schema["properties"]["invalidation_conditions"]["minItems"] == 1


# ---------------------------------------------------------------------------
# 53. Opt-in live model validation
# ---------------------------------------------------------------------------
@pytest.mark.llm
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="requires ANTHROPIC_API_KEY; this test makes one real model call",
)
def test_a_real_model_satisfies_the_same_contract(research_input, config) -> None:
    """One real call, against the shipped prompt and the shipped schema.

    Skipped by default and doubly gated: the ``llm`` marker needs
    ``ALLOW_LIVE_TESTS=true`` and the key must be present. It reaches no
    broker, places no order, and asserts only structure — never that the model
    reached a particular conclusion, because that is not a property of the
    system.

    A ``ResearchOutputInvalidError`` here is a *pass* in the sense that
    matters: it means the deterministic layer caught a real model's real
    mistake, which is exactly its job. It is re-raised so the output can be
    inspected.
    """
    from trading_system.agents.anthropic_client import AnthropicLLMClient, anthropic_available
    from trading_system.agents.prompts import prompt_fingerprint

    if not anthropic_available():
        pytest.skip("the 'anthropic' extra is not installed")

    client = AnthropicLLMClient(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model_name=config.agent.model_name,
        prompt_version=config.agent.prompt_version,
        prompt_fingerprint=prompt_fingerprint(PROMPT_NAME),
    )

    outcome = MarketResearcherAgent(
        client,
        config=config,
        max_output_tokens=config.agent.max_output_tokens,
        timeout_seconds=config.agent.timeout_seconds,
        effort=config.agent.effort,
    ).research(research_input)

    output = outcome.output
    assert output.hypothesis in set(MarketHypothesis)
    assert output.confidence in set(ConfidenceLevel)
    assert research_input.horizon.contains(output.horizon_days)
    assert output.invalidation_conditions, "every outlook must be falsifiable"
    assert output.thesis.strip()
    for item in output.evidence:
        assert item.evidence_id in research_input.evidence_ids, "no invented source"
    assert outcome.response.identity.provider == "ANTHROPIC"
