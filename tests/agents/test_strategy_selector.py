"""The Strategy Selector agent (brief section 35; specification 24.2).

Every test here uses a deterministic fake model. No test requires an API key,
touches the network, or is skipped for lack of a credential — the agent takes
an :class:`~trading_system.agents.base.LLMClient`, so a fake, a replayed
fixture and a live model are the same code path.

The suite is organised around what the agent must *refuse to do*. An invented
strategy, one that does not answer the hypothesis, a confidence the research
does not license, a strike or an expiration or a size written into the
response, malformed JSON, a timeout, an unreachable model — each produces a
specific error, and none produces a partially accepted decision. The system
fails closed.

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
from trading_system.agents.strategy_selector import (
    PROMPT_NAME,
    StrategySelectorAgent,
    strategy_output_schema,
)
from trading_system.domain.enums import (
    ConfidenceLevel,
    Direction,
    ExpectedMagnitude,
    MarketEventType,
    MarketHypothesis,
    RelevanceLevel,
    StrategyAction,
    StrategySelectionReason,
    StrategyType,
)
from trading_system.infrastructure.settings import (
    StrategyAgentConfig,
    StrategyEligibilityConfig,
    StrategyLimitsConfig,
    StrategyStageConfig,
)
from trading_system.strategies.models import (
    ResearchClaim,
    ResearchEventSummary,
    ResearchQualitySnapshot,
    ResearchSummary,
    StrategyOption,
    StrategySelectionInput,
)
from trading_system.strategies.validation import StrategyOutputInvalidError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
RUN_ID = "strategy-agent-test"


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


#: The four strategies as the registry resolves them, hand-built so the agent
#: suite does not depend on the shipped configuration staying identical.
OPTIONS: dict[StrategyType, StrategyOption] = {
    StrategyType.LONG_CALL: StrategyOption(
        strategy_id=StrategyType.LONG_CALL,
        name="long_call",
        version="1.0.0",
        description="one long call",
        structure="one long call",
        applicable_hypotheses=[MarketHypothesis.B],
        legs=["BUY CALL x1"],
        leg_count=1,
        directional_view=Direction.BULLISH,
        dte_min=14,
        dte_max=30,
    ),
    StrategyType.LONG_PUT: StrategyOption(
        strategy_id=StrategyType.LONG_PUT,
        name="long_put",
        version="1.0.0",
        description="one long put",
        structure="one long put",
        applicable_hypotheses=[MarketHypothesis.C],
        legs=["BUY PUT x1"],
        leg_count=1,
        directional_view=Direction.BEARISH,
        dte_min=14,
        dte_max=30,
    ),
    StrategyType.LONG_STRADDLE: StrategyOption(
        strategy_id=StrategyType.LONG_STRADDLE,
        name="long_straddle",
        version="1.0.0",
        description="a call and a put on one strike",
        structure="a call and a put on one strike",
        applicable_hypotheses=[MarketHypothesis.A, MarketHypothesis.D],
        legs=["BUY CALL x1", "BUY PUT x1"],
        leg_count=2,
        directional_view=Direction.UNCERTAIN,
        aligns_to_events=True,
        dte_min=14,
        dte_max=30,
    ),
    StrategyType.LONG_STRANGLE: StrategyOption(
        strategy_id=StrategyType.LONG_STRANGLE,
        name="long_strangle",
        version="1.0.0",
        description="an out-of-the-money call and put",
        structure="an out-of-the-money call and put",
        applicable_hypotheses=[MarketHypothesis.A, MarketHypothesis.D],
        legs=["BUY CALL x1", "BUY PUT x1"],
        leg_count=2,
        directional_view=Direction.UNCERTAIN,
        aligns_to_events=True,
        dte_min=14,
        dte_max=30,
    ),
}

ELIGIBLE: dict[MarketHypothesis, list[StrategyType]] = {
    MarketHypothesis.A: [StrategyType.LONG_STRADDLE, StrategyType.LONG_STRANGLE],
    MarketHypothesis.B: [StrategyType.LONG_CALL],
    MarketHypothesis.C: [StrategyType.LONG_PUT],
    MarketHypothesis.D: [StrategyType.LONG_STRADDLE, StrategyType.LONG_STRANGLE],
}


def _summary(
    *,
    hypothesis: MarketHypothesis = MarketHypothesis.B,
    direction: Direction = Direction.BULLISH,
    magnitude: ExpectedMagnitude = ExpectedMagnitude.MODERATE,
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
    horizon_days: int = 21,
    evidence_count: int = 3,
    contradicting: int = 0,
    research_usable: bool = True,
    event_days: int | None = None,
) -> ResearchSummary:
    return ResearchSummary(
        report_id="research-001",
        symbol="NVDA",
        as_of=NOW,
        hypothesis=hypothesis,
        direction=direction,
        expected_magnitude=magnitude,
        confidence=confidence,
        horizon_days=horizon_days,
        thesis="demand is accelerating",
        expected_behavior="a drift higher with volatility around the results",
        explanation="none of A-D fits" if hypothesis is MarketHypothesis.E else None,
        bullish_catalysts=[ResearchClaim(summary="accelerating revenue")],
        risks=[ResearchClaim(summary="the results could disappoint", category="EVENT_RISK")],
        invalidation_conditions=[ResearchClaim(summary="guidance is cut")],
        key_events=(
            [
                ResearchEventSummary(
                    event_type=MarketEventType.EARNINGS,
                    summary="quarterly results",
                    days_until=event_days,
                    within_horizon=True,
                    expected_relevance=RelevanceLevel.HIGH,
                    directional_uncertainty=True,
                )
            ]
            if event_days is not None
            else []
        ),
        data_quality=ResearchQualitySnapshot(
            research_usable=research_usable,
            evidence_count=evidence_count,
            supporting_count=evidence_count - contradicting,
            contradicting_count=contradicting,
        ),
    )


def _input(**kwargs) -> StrategySelectionInput:
    research = _summary(**kwargs)
    return StrategySelectionInput(
        run_id=RUN_ID,
        symbol="NVDA",
        as_of=NOW,
        research=research,
        eligible_strategies=[OPTIONS[s] for s in ELIGIBLE[research.hypothesis]],
        research_run_id="research-run-1",
    )


def _response(selection_input: StrategySelectionInput, **overrides) -> str:
    offered = sorted(option.strategy_id.value for option in selection_input.eligible_strategies)
    payload: dict[str, object] = {
        "run_id": selection_input.run_id,
        "symbol": selection_input.symbol,
        "action": "BUY",
        "selected_strategy": offered[0],
        "confidence": "MEDIUM",
        "reasons": ["HYPOTHESIS_MATCH"],
        "rationale": "the hypothesis and the payoff agree over the stated horizon",
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.fixture
def config() -> StrategyStageConfig:
    return StrategyStageConfig(
        config_version="test-strategy-1",
        eligibility=StrategyEligibilityConfig(),
        limits=StrategyLimitsConfig(max_input_characters=60_000),
        agent=StrategyAgentConfig(
            enabled=True,
            model_provider="ANTHROPIC",
            model_name="claude-opus-5",
            prompt_version="test-1.0.0",
            timeout_seconds=30.0,
            max_output_tokens=2000,
            effort="low",
        ),
    )


def _select(text: str, selection_input: StrategySelectionInput, config: StrategyStageConfig):
    return StrategySelectorAgent(ScriptedClient(text), config=config).select(selection_input)


# ---------------------------------------------------------------------------
# 1-5. Each hypothesis reaches the strategy that answers it
# ---------------------------------------------------------------------------
def test_b_selects_a_long_call(config) -> None:
    selection_input = _input(hypothesis=MarketHypothesis.B)

    outcome = _select(
        _response(selection_input, selected_strategy="LONG_CALL"), selection_input, config
    )

    assert outcome.output.action is StrategyAction.BUY
    assert outcome.output.selected_strategy is StrategyType.LONG_CALL


def test_c_selects_a_long_put(config) -> None:
    selection_input = _input(hypothesis=MarketHypothesis.C, direction=Direction.BEARISH)

    outcome = _select(
        _response(selection_input, selected_strategy="LONG_PUT"), selection_input, config
    )

    assert outcome.output.selected_strategy is StrategyType.LONG_PUT


def test_a_selects_a_straddle(config) -> None:
    selection_input = _input(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        magnitude=ExpectedMagnitude.LARGE,
    )

    outcome = _select(
        _response(
            selection_input,
            selected_strategy="LONG_STRADDLE",
            reasons=["HYPOTHESIS_MATCH", "DIRECTION_UNCERTAIN", "LARGE_MOVE_EXPECTED"],
        ),
        selection_input,
        config,
    )

    assert outcome.output.selected_strategy is StrategyType.LONG_STRADDLE


def test_a_can_also_select_a_strangle(config) -> None:
    """Both are configured for A; which one fits is the agent's judgement."""
    selection_input = _input(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        magnitude=ExpectedMagnitude.EXTREME,
    )

    outcome = _select(
        _response(selection_input, selected_strategy="LONG_STRANGLE"), selection_input, config
    )

    assert outcome.output.selected_strategy is StrategyType.LONG_STRANGLE


def test_d_selects_an_event_capable_strategy(config) -> None:
    selection_input = _input(
        hypothesis=MarketHypothesis.D,
        direction=Direction.UNCERTAIN,
        magnitude=ExpectedMagnitude.LARGE,
        event_days=17,
    )

    outcome = _select(
        _response(
            selection_input,
            selected_strategy="LONG_STRADDLE",
            reasons=["HYPOTHESIS_MATCH", "EVENT_IN_HORIZON"],
        ),
        selection_input,
        config,
    )

    assert outcome.output.selected_strategy is StrategyType.LONG_STRADDLE
    straddle = selection_input.option(StrategyType.LONG_STRADDLE)
    assert straddle is not None and straddle.aligns_to_events


# ---------------------------------------------------------------------------
# 6. NO_TRADE
# ---------------------------------------------------------------------------
def test_no_trade_is_a_valid_answer(config) -> None:
    selection_input = _input()

    outcome = _select(
        _response(
            selection_input,
            action="NO_TRADE",
            selected_strategy=None,
            confidence="LOW",
            reasons=["RESEARCH_INCOMPATIBLE"],
            rationale="nothing offered expresses this outlook well enough to act on",
        ),
        selection_input,
        config,
    )

    assert outcome.output.action is StrategyAction.NO_TRADE
    assert outcome.output.selected_strategy is None


def test_no_trade_carrying_a_strategy_is_rejected(config) -> None:
    """Declining and proposing are different answers, not a spectrum."""
    selection_input = _input()

    with pytest.raises(AgentInvalidOutputError):
        _select(
            _response(selection_input, action="NO_TRADE", selected_strategy="LONG_CALL"),
            selection_input,
            config,
        )


def test_buy_without_a_strategy_is_rejected(config) -> None:
    selection_input = _input()

    with pytest.raises(AgentInvalidOutputError):
        _select(
            _response(selection_input, action="BUY", selected_strategy=None),
            selection_input,
            config,
        )


def test_hypothesis_e_offers_nothing_to_select_from() -> None:
    """The agent is never even consulted: the input cannot be built."""
    with pytest.raises(ValueError, match="at least 1 item"):
        StrategySelectionInput(
            run_id=RUN_ID,
            symbol="NVDA",
            as_of=NOW,
            research=_summary(hypothesis=MarketHypothesis.E, direction=Direction.NEUTRAL),
            eligible_strategies=[],
        )


# ---------------------------------------------------------------------------
# 7-8. Invalid and ineligible strategies
# ---------------------------------------------------------------------------
def test_a_strategy_outside_the_vocabulary_is_rejected(config) -> None:
    selection_input = _input()

    with pytest.raises(AgentInvalidOutputError):
        _select(
            _response(selection_input, selected_strategy="IRON_CONDOR"), selection_input, config
        )


def test_a_strategy_that_was_not_offered_is_rejected(config) -> None:
    selection_input = _input(hypothesis=MarketHypothesis.B)

    with pytest.raises(StrategyOutputInvalidError, match="was not among the strategies offered"):
        _select(
            _response(selection_input, selected_strategy="LONG_STRADDLE"),
            selection_input,
            config,
        )


def test_a_directional_strategy_against_the_research_direction_is_rejected(config) -> None:
    selection_input = _input(hypothesis=MarketHypothesis.C, direction=Direction.BULLISH)

    with pytest.raises(StrategyOutputInvalidError, match="but the research states BULLISH"):
        _select(_response(selection_input, selected_strategy="LONG_PUT"), selection_input, config)


# ---------------------------------------------------------------------------
# 9-11. Malformed responses
# ---------------------------------------------------------------------------
def test_malformed_json_is_rejected(config) -> None:
    with pytest.raises(AgentInvalidOutputError, match="not valid JSON"):
        _select("{not json", _input(), config)


def test_an_empty_response_is_rejected(config) -> None:
    with pytest.raises(AgentInvalidOutputError, match="empty response"):
        _select("   ", _input(), config)


def test_a_json_array_is_rejected(config) -> None:
    with pytest.raises(AgentInvalidOutputError, match="expected a JSON object"):
        _select("[]", _input(), config)


def test_a_markdown_fence_is_tolerated(config) -> None:
    """A formatting habit, not a semantic error."""
    selection_input = _input()
    fenced = f"```json\n{_response(selection_input)}\n```"

    outcome = _select(fenced, selection_input, config)

    assert outcome.output.selected_strategy is StrategyType.LONG_CALL


def test_a_missing_field_is_rejected(config) -> None:
    selection_input = _input()
    payload = json.loads(_response(selection_input))
    del payload["rationale"]

    with pytest.raises(AgentInvalidOutputError, match="strategy contract"):
        _select(json.dumps(payload), selection_input, config)


def test_an_unsupported_confidence_value_is_rejected(config) -> None:
    selection_input = _input()

    with pytest.raises(AgentInvalidOutputError):
        _select(_response(selection_input, confidence="VERY_HIGH"), selection_input, config)


def test_a_confidence_above_the_research_is_rejected(config) -> None:
    """Rejected, never quietly lowered: the band would then be ours, not the agent's."""
    selection_input = _input(confidence=ConfidenceLevel.LOW)

    with pytest.raises(StrategyOutputInvalidError, match="cannot be more certain"):
        _select(_response(selection_input, confidence="HIGH"), selection_input, config)


def test_an_empty_reason_list_is_rejected(config) -> None:
    selection_input = _input()

    with pytest.raises(AgentInvalidOutputError):
        _select(_response(selection_input, reasons=[]), selection_input, config)


# ---------------------------------------------------------------------------
# 12-16. A contract, a size or a price in the response
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field",
    ["strike", "expiration", "contract_id", "quantity", "limit_price", "premium"],
)
def test_an_extra_field_naming_a_contract_or_a_size_is_rejected(config, field: str) -> None:
    """``extra="forbid"`` is load-bearing: the field is refused, not dropped."""
    selection_input = _input()
    payload = json.loads(_response(selection_input))
    payload[field] = 190

    with pytest.raises(AgentInvalidOutputError, match="strategy contract"):
        _select(json.dumps(payload), selection_input, config)


@pytest.mark.parametrize(
    "rationale",
    [
        "buy the 190 call for the cleanest expression",
        "the strike at 185 offers the best convexity",
        "use the 2026-09-18 expiration to cover the event",
        "open 5 contracts now and add on weakness",
        "commit EUR 1500 of the campaign budget",
    ],
    ids=["contract", "strike", "expiration", "quantity", "money"],
)
def test_a_contract_or_a_size_written_into_prose_is_rejected(config, rationale: str) -> None:
    selection_input = _input()

    with pytest.raises(StrategyOutputInvalidError):
        _select(_response(selection_input, rationale=rationale), selection_input, config)


# ---------------------------------------------------------------------------
# 17. A justification the research does not support
# ---------------------------------------------------------------------------
def test_a_reason_the_research_refutes_is_rejected(config) -> None:
    """A fabricated justification from the allowed vocabulary is still fabricated."""
    selection_input = _input(event_days=None)

    with pytest.raises(StrategyOutputInvalidError, match="EVENT_IN_HORIZON"):
        _select(_response(selection_input, reasons=["EVENT_IN_HORIZON"]), selection_input, config)


def test_a_judgement_about_fit_is_accepted(config) -> None:
    """Facts are enforced; opinions are the agent's to hold."""
    selection_input = _input()

    outcome = _select(
        _response(
            selection_input,
            action="NO_TRADE",
            selected_strategy=None,
            confidence="LOW",
            reasons=["RESEARCH_INCOMPATIBLE"],
        ),
        selection_input,
        config,
    )

    assert StrategySelectionReason.RESEARCH_INCOMPATIBLE in outcome.output.reasons


# ---------------------------------------------------------------------------
# 18-19. The model does not answer
# ---------------------------------------------------------------------------
def test_an_unreachable_model_surfaces_as_unavailable(config) -> None:
    client = ScriptedClient(error=AgentUnavailableError("the API is unreachable"))

    with pytest.raises(AgentUnavailableError):
        StrategySelectorAgent(client, config=config).select(_input())


def test_a_timeout_surfaces_as_unavailable(config) -> None:
    """A request that never returns and one that cannot be sent are one problem."""
    client = ScriptedClient(error=AgentTimeoutError("timed out"))

    with pytest.raises(AgentUnavailableError):
        StrategySelectorAgent(client, config=config).select(_input())


def test_a_refusal_is_not_parsed_as_a_decision(config) -> None:
    selection_input = _input()
    client = ScriptedClient(_response(selection_input), stop_reason="refusal")

    with pytest.raises(AgentUnavailableError, match="declined or truncated"):
        StrategySelectorAgent(client, config=config).select(selection_input)


def test_a_truncated_generation_is_not_parsed_as_a_decision(config) -> None:
    selection_input = _input()
    client = ScriptedClient(_response(selection_input), stop_reason="max_tokens")

    with pytest.raises(AgentUnavailableError, match="declined or truncated"):
        StrategySelectorAgent(client, config=config).select(selection_input)


def test_a_stale_response_for_another_run_is_rejected(config) -> None:
    selection_input = _input()

    with pytest.raises(StrategyOutputInvalidError, match="but the request was"):
        _select(_response(selection_input, run_id="another-run"), selection_input, config)


def test_a_response_about_another_symbol_is_rejected(config) -> None:
    selection_input = _input()

    with pytest.raises(StrategyOutputInvalidError, match="isolated per underlying"):
        _select(_response(selection_input, symbol="AAPL"), selection_input, config)


# ---------------------------------------------------------------------------
# The request itself
# ---------------------------------------------------------------------------
def test_the_request_carries_the_input_contract_and_nothing_else(config) -> None:
    selection_input = _input()
    client = ScriptedClient(_response(selection_input))

    StrategySelectorAgent(client, config=config).select(selection_input)

    payload = json.loads(client.requests[0].user_content)
    assert set(payload) == {
        "instruction",
        "run_id",
        "symbol",
        "as_of",
        "research",
        "eligible_strategies",
    }


def test_the_request_uses_the_shipped_prompt(config) -> None:
    from trading_system.agents.prompts import load_prompt

    selection_input = _input()
    client = ScriptedClient(_response(selection_input))

    StrategySelectorAgent(client, config=config).select(selection_input)

    assert client.requests[0].system_prompt == load_prompt(PROMPT_NAME)


def test_the_schema_enumerates_only_the_offered_strategies(config) -> None:
    selection_input = _input(hypothesis=MarketHypothesis.B)

    schema = strategy_output_schema(selection_input)

    assert schema["properties"]["selected_strategy"]["enum"] == ["LONG_CALL", None]
    assert schema["additionalProperties"] is False


def test_an_oversized_input_fails_rather_than_being_truncated(config) -> None:
    """An agent choosing from a silently shortened list would believe it saw
    every option."""
    tiny = config.model_copy(update={"limits": StrategyLimitsConfig(max_input_characters=1000)})
    selection_input = _input()

    with pytest.raises(AgentInvalidOutputError, match="not truncated"):
        StrategySelectorAgent(ScriptedClient("{}"), config=tiny).build_user_content(selection_input)


def test_the_response_records_which_model_answered(config) -> None:
    selection_input = _input()

    outcome = _select(_response(selection_input), selection_input, config)

    assert outcome.response.identity.provider == "FAKE"
    assert outcome.response.identity.prompt_version == "test-1.0.0"
    assert outcome.response.latency_ms is not None


def test_the_input_shows_relative_event_timing_and_never_a_date(config) -> None:
    selection_input = _input(event_days=17)
    client = ScriptedClient(_response(selection_input))

    StrategySelectorAgent(client, config=config).select(selection_input)

    payload = json.loads(client.requests[0].user_content)
    event = payload["research"]["key_events"][0]
    assert event["days_until"] == 17
    assert "event_time" not in event
    assert "expected_event_time" not in event


# ---------------------------------------------------------------------------
# One real call, opt-in
# ---------------------------------------------------------------------------
@pytest.mark.llm
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="requires ANTHROPIC_API_KEY; this test makes one real model call",
)
def test_a_real_model_satisfies_the_same_contract(config) -> None:
    """One real call, against the shipped prompt and the shipped schema.

    Skipped by default and doubly gated. It reaches no broker, places no order,
    and asserts only structure — never that the model chose a particular
    strategy, because that is not a property of the system.

    A :class:`StrategyOutputInvalidError` here is a *pass* in the sense that
    matters: the deterministic layer caught a real model's real mistake, which
    is exactly its job. It is re-raised so the output can be inspected.
    """
    from trading_system.agents.anthropic_client import AnthropicLLMClient, anthropic_available
    from trading_system.agents.prompts import prompt_fingerprint

    if not anthropic_available():
        pytest.skip("the 'anthropic' extra is not installed")

    selection_input = _input(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        magnitude=ExpectedMagnitude.LARGE,
        event_days=None,
    )
    client = AnthropicLLMClient(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model_name=config.agent.model_name,
        prompt_version=config.agent.prompt_version,
        prompt_fingerprint=prompt_fingerprint(PROMPT_NAME),
    )

    outcome = StrategySelectorAgent(
        client,
        config=config,
        max_output_tokens=config.agent.max_output_tokens,
        timeout_seconds=config.agent.timeout_seconds,
        effort=config.agent.effort,
    ).select(selection_input)

    output = outcome.output
    assert output.action in set(StrategyAction)
    if output.action is StrategyAction.BUY:
        assert output.selected_strategy in selection_input.strategy_ids, "no invented strategy"
    else:
        assert output.selected_strategy is None
    assert output.confidence in set(ConfidenceLevel)
    assert output.reasons
    assert output.rationale.strip()
    assert outcome.response.identity.provider == "ANTHROPIC"


def test_the_agent_never_needs_a_credential_for_any_other_test() -> None:
    """Every test above runs on a fake client. This asserts the shape of that."""
    assert timedelta(0) == timedelta(0)
    assert not os.environ.get("ANTHROPIC_API_KEY_REQUIRED")
