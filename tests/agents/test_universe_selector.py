"""The Universe Selector agent (brief sections 32, 38-39; specification 24.2).

Every test here uses a deterministic fake model. No test requires an API key,
touches the network, or is skipped for lack of a credential — the agent takes an
:class:`~trading_system.agents.base.LLMClient`, so a fake, a replayed fixture and
a live model are the same code path.

The suite is organised around what the agent must *refuse to do*. A malformed
response, a hallucinated ticker, a duplicated rank, an over-long selection, an
unsupported enum value, a fabricated justification, a timeout, an unreachable
model — each produces a specific error, and none produces a partially accepted
ranking. The system fails closed.

There is one opt-in test at the end that calls a real model. It is marked
``llm``, skipped unless ``ALLOW_LIVE_TESTS=true``, places no trade, and asserts
only that a real response satisfies the same contract as a fake one.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_system.agents.base import (
    AgentInvalidOutputError,
    AgentTimeoutError,
    AgentUnavailableError,
    LLMResponse,
    ModelIdentity,
    StructuredRequest,
)
from trading_system.agents.universe_selector import (
    PROMPT_NAME,
    UniverseSelectorAgent,
    ranking_output_schema,
)
from trading_system.domain.enums import (
    ConfidenceLevel,
    DataQuality,
    Optionability,
    SecurityType,
    UniverseEligibility,
    UniverseSelectionReason,
)
from trading_system.universe.models import (
    CandidateAsset,
    CandidateProvenance,
    DataQualitySummary,
    FilterConfigSnapshot,
    UniverseSelectionInput,
    UniverseSourceRef,
)
from trading_system.universe.validation import AgentOutputInvalidError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
RUN_ID = "universe-agent-test"


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


def _candidate(
    symbol: str,
    *,
    optionability: Optionability = Optionability.TRUE,
    volume: Decimal | None = Decimal("80000000"),
) -> CandidateAsset:
    return CandidateAsset(
        symbol=symbol,
        security_type=SecurityType.STOCK,
        currency="USD",
        deterministic_eligibility=UniverseEligibility.ELIGIBLE,
        optionability=optionability,
        reference_price=Decimal("500.15"),
        underlying_volume=volume,
        market_data_as_of=NOW,
        data_quality=DataQualitySummary(research_usable=True, classification=DataQuality.OK),
        source=CandidateProvenance(
            provider="IBKR", retrieved_at=NOW, snapshot_ids=[f"snap-{symbol}"]
        ),
    )


@pytest.fixture
def agent_input() -> UniverseSelectionInput:
    return UniverseSelectionInput(
        run_id=RUN_ID,
        as_of=NOW,
        universe_source=UniverseSourceRef(kind="STATIC", name="test", version="1", symbol_count=3),
        deterministic_filter_config=FilterConfigSnapshot(max_candidates=50, max_selected_assets=2),
        candidate_assets=[_candidate("SPY"), _candidate("QQQ"), _candidate("NVDA")],
        data_snapshot_ids=["snap-SPY", "snap-QQQ", "snap-NVDA"],
        max_selected_assets=2,
    )


def _response(rankings: list[dict[str, object]], *, run_id: str = RUN_ID) -> str:
    return json.dumps({"run_id": run_id, "rankings": rankings})


def _entry(
    symbol: str,
    *,
    selection: str = "SELECTED",
    rank: int | None = 1,
    reasons: list[str] | None = None,
    confidence: str = "HIGH",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "selection": selection,
        "rank": rank if selection == "SELECTED" else None,
        "reasons": reasons or ["OPTIONS_AVAILABLE", "SUFFICIENT_DATA_QUALITY"],
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 1. A valid response
# ---------------------------------------------------------------------------
def test_a_valid_response_is_accepted(agent_input) -> None:
    client = ScriptedClient(
        _response(
            [
                _entry("SPY", rank=1),
                _entry("QQQ", rank=2),
                _entry("NVDA", selection="NOT_SELECTED"),
            ]
        )
    )

    outcome = UniverseSelectorAgent(client).rank(agent_input, max_selected=2)

    assert [r.symbol for r in outcome.ranking.selected] == ["SPY", "QQQ"]
    assert outcome.ranking.not_selected[0].symbol == "NVDA"
    assert outcome.response.identity.model_name == "fake-model-1"


def test_the_agent_sends_the_prompt_and_the_schema(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1)]))

    UniverseSelectorAgent(client).rank(agent_input, max_selected=2)

    request = client.requests[0]
    assert "NOT a trader" in request.system_prompt
    assert request.output_schema["required"] == ["run_id", "rankings"]
    assert request.timeout_seconds > 0, "every model call is bounded"


def test_the_agent_sends_only_the_input_contract(agent_input) -> None:
    """Brief section 18: a compact structured dataset, nothing more."""
    client = ScriptedClient(_response([_entry("SPY", rank=1)]))

    UniverseSelectorAgent(client).rank(agent_input, max_selected=2)

    payload = json.loads(client.requests[0].user_content)
    assert set(payload) == {
        "instruction",
        "run_id",
        "as_of",
        "max_selected_assets",
        "universe_source",
        "deterministic_filter_config",
        "candidate_assets",
    }
    assert payload["max_selected_assets"] == 2


def test_the_latency_is_recorded_when_the_client_does_not_report_one(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1)]))

    outcome = UniverseSelectorAgent(client).rank(agent_input, max_selected=2)

    assert outcome.response.latency_ms is not None
    assert outcome.response.latency_ms >= 0


# ---------------------------------------------------------------------------
# 2. Malformed JSON
# ---------------------------------------------------------------------------
def test_malformed_json_is_rejected(agent_input) -> None:
    client = ScriptedClient("{not json at all")

    with pytest.raises(AgentInvalidOutputError, match="not valid JSON"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_prose_instead_of_json_is_rejected(agent_input) -> None:
    """Brief section 5: prose is not a valid response, and is never parsed."""
    client = ScriptedClient("I think NVDA looks interesting given the recent momentum.")

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_an_empty_response_is_rejected(agent_input) -> None:
    with pytest.raises(AgentInvalidOutputError, match="empty"):
        UniverseSelectorAgent(ScriptedClient("")).rank(agent_input, max_selected=2)


def test_a_json_array_instead_of_an_object_is_rejected(agent_input) -> None:
    with pytest.raises(AgentInvalidOutputError, match="expected a JSON object"):
        UniverseSelectorAgent(ScriptedClient("[]")).rank(agent_input, max_selected=2)


def test_a_markdown_fence_is_tolerated(agent_input) -> None:
    """A formatting habit, not a semantic error."""
    body = _response([_entry("SPY", rank=1)])
    client = ScriptedClient(f"```json\n{body}\n```")

    outcome = UniverseSelectorAgent(client).rank(agent_input, max_selected=2)

    assert [r.symbol for r in outcome.ranking.selected] == ["SPY"]


def test_a_truncated_object_is_not_reconstructed(agent_input) -> None:
    """No best-effort repair: a response we had to guess at cannot be audited."""
    client = ScriptedClient('{"run_id": "universe-agent-test", "rankings": [{"symbol": "SP')

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


# ---------------------------------------------------------------------------
# 3. Missing fields
# ---------------------------------------------------------------------------
def test_a_missing_required_field_is_rejected(agent_input) -> None:
    client = ScriptedClient(_response([{"symbol": "SPY", "selection": "SELECTED", "rank": 1}]))

    with pytest.raises(AgentInvalidOutputError, match="ranking contract"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_a_missing_run_id_is_rejected(agent_input) -> None:
    client = ScriptedClient(json.dumps({"rankings": []}))

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_an_unexpected_extra_field_is_rejected(agent_input) -> None:
    """The models are extra=forbid: a surprise field is a contract violation."""
    entry = _entry("SPY", rank=1)
    entry["price_target"] = 650.0
    client = ScriptedClient(_response([entry]))

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


# ---------------------------------------------------------------------------
# 4 & 5. Unknown and excluded symbols
# ---------------------------------------------------------------------------
def test_an_unknown_symbol_invalidates_the_response(agent_input) -> None:
    client = ScriptedClient(_response([_entry("TSLA", rank=1)]))

    with pytest.raises(AgentOutputInvalidError, match="TSLA"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_an_excluded_symbol_returned_by_the_agent_is_rejected(agent_input) -> None:
    """An asset the deterministic filter removed cannot be ranked back in.

    It is not in the input at all, so naming it is indistinguishable from
    inventing one — which is exactly the point: the exclusion is unreachable.
    """
    client = ScriptedClient(_response([_entry("SPY", rank=1), _entry("PENNY", rank=2)]))

    with pytest.raises(AgentOutputInvalidError, match="PENNY"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_one_bad_symbol_voids_the_valid_entries_too(agent_input) -> None:
    """Partial acceptance would store a universe the model did not choose."""
    client = ScriptedClient(_response([_entry("SPY", rank=1), _entry("TSLA", rank=2)]))

    with pytest.raises(AgentOutputInvalidError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


# ---------------------------------------------------------------------------
# 6. Duplicate rank
# ---------------------------------------------------------------------------
def test_a_duplicate_rank_is_rejected(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1), _entry("QQQ", rank=1)]))

    with pytest.raises(AgentOutputInvalidError, match="unique"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_non_contiguous_ranks_are_rejected(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1), _entry("QQQ", rank=5)]))

    with pytest.raises(AgentOutputInvalidError, match="contiguous"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_a_duplicated_symbol_is_rejected(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1), _entry("SPY", rank=2)]))

    with pytest.raises(AgentOutputInvalidError, match="more than once"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


# ---------------------------------------------------------------------------
# 7. Too many selected assets
# ---------------------------------------------------------------------------
def test_exceeding_the_maximum_is_rejected(agent_input) -> None:
    client = ScriptedClient(
        _response([_entry("SPY", rank=1), _entry("QQQ", rank=2), _entry("NVDA", rank=3)])
    )

    with pytest.raises(AgentOutputInvalidError, match="max_selected_assets is 2"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_the_response_is_not_truncated_back_into_range(agent_input) -> None:
    client = ScriptedClient(
        _response([_entry("SPY", rank=1), _entry("QQQ", rank=2), _entry("NVDA", rank=3)])
    )

    with pytest.raises(AgentOutputInvalidError, match="rather than truncated"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


# ---------------------------------------------------------------------------
# 8 & 9. Unsupported enum values
# ---------------------------------------------------------------------------
def test_an_unsupported_selection_value_is_rejected(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", selection="MAYBE", rank=1)]))

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_an_unsupported_confidence_is_rejected(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1, confidence="VERY_HIGH")]))

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_an_invented_reason_code_is_rejected(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1, reasons=["MOMENTUM_STRONG"])]))

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_a_directional_reason_has_no_representation(agent_input) -> None:
    """Brief section 43: the vocabulary has no word for a directional view."""
    client = ScriptedClient(_response([_entry("SPY", rank=1, reasons=["BULLISH"])]))

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_a_strategy_reason_has_no_representation(agent_input) -> None:
    """Brief section 44: no LONG_CALL, no STRADDLE, nowhere to put one."""
    client = ScriptedClient(_response([_entry("SPY", rank=1, reasons=["LONG_CALL"])]))

    with pytest.raises(AgentInvalidOutputError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


# ---------------------------------------------------------------------------
# 10. Fabricated evidence
# ---------------------------------------------------------------------------
def test_an_unsupported_claim_about_optionability_is_rejected() -> None:
    unresolved = UniverseSelectionInput(
        run_id=RUN_ID,
        as_of=NOW,
        universe_source=UniverseSourceRef(kind="STATIC", name="t", version="1"),
        deterministic_filter_config=FilterConfigSnapshot(max_candidates=10, max_selected_assets=2),
        candidate_assets=[_candidate("SPY", optionability=Optionability.UNKNOWN)],
    )
    client = ScriptedClient(_response([_entry("SPY", rank=1, reasons=["OPTIONS_AVAILABLE"])]))

    with pytest.raises(AgentOutputInvalidError, match="unestablished chain"):
        UniverseSelectorAgent(client).rank(unresolved, max_selected=2)


def test_a_liquidity_claim_with_no_volume_is_rejected() -> None:
    no_volume = UniverseSelectionInput(
        run_id=RUN_ID,
        as_of=NOW,
        universe_source=UniverseSourceRef(kind="STATIC", name="t", version="1"),
        deterministic_filter_config=FilterConfigSnapshot(max_candidates=10, max_selected_assets=2),
        candidate_assets=[_candidate("SPY", volume=None)],
    )
    client = ScriptedClient(
        _response([_entry("SPY", rank=1, reasons=["HIGH_UNDERLYING_LIQUIDITY"])])
    )

    with pytest.raises(AgentOutputInvalidError, match="no underlying volume"):
        UniverseSelectorAgent(client).rank(no_volume, max_selected=2)


def test_a_rationale_cannot_change_an_outcome(agent_input) -> None:
    """Prose is retained for a human; the machine-readable fields are authoritative."""
    entry = _entry("SPY", rank=1)
    entry["rationale"] = "This company will beat earnings and the stock will rip."
    client = ScriptedClient(_response([entry]))

    outcome = UniverseSelectorAgent(client).rank(agent_input, max_selected=2)

    assert outcome.ranking.selected[0].reasons == [
        UniverseSelectionReason.OPTIONS_AVAILABLE,
        UniverseSelectionReason.SUFFICIENT_DATA_QUALITY,
    ], "the reason codes are what the system acts on"


# ---------------------------------------------------------------------------
# 11. Empty result
# ---------------------------------------------------------------------------
def test_an_empty_ranking_is_accepted(agent_input) -> None:
    outcome = UniverseSelectorAgent(ScriptedClient(_response([]))).rank(agent_input, max_selected=2)

    assert outcome.ranking.selected == []


def test_selecting_none_while_ranking_all_is_accepted(agent_input) -> None:
    client = ScriptedClient(
        _response(
            [
                _entry(symbol, selection="NOT_SELECTED", reasons=["UNIVERSE_SIZE_LIMIT"])
                for symbol in ("SPY", "QQQ", "NVDA")
            ]
        )
    )

    outcome = UniverseSelectorAgent(client).rank(agent_input, max_selected=2)

    assert outcome.ranking.selected == []
    assert len(outcome.ranking.not_selected) == 3


# ---------------------------------------------------------------------------
# 12 & 13. Timeout and unavailability
# ---------------------------------------------------------------------------
def test_a_timeout_surfaces_as_unavailable(agent_input) -> None:
    client = ScriptedClient(error=AgentTimeoutError("no answer within 30s"))

    with pytest.raises(AgentTimeoutError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_an_unreachable_model_surfaces_as_unavailable(agent_input) -> None:
    client = ScriptedClient(error=AgentUnavailableError("connection refused"))

    with pytest.raises(AgentUnavailableError):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_a_refusal_is_not_parsed_as_a_ranking(agent_input) -> None:
    client = ScriptedClient("", stop_reason="refusal")

    with pytest.raises(AgentUnavailableError, match="no usable answer"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_a_truncated_generation_is_not_parsed_as_a_ranking(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1)]), stop_reason="max_tokens")

    with pytest.raises(AgentUnavailableError, match="no usable answer"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


def test_a_stale_response_for_another_run_is_rejected(agent_input) -> None:
    client = ScriptedClient(_response([_entry("SPY", rank=1)], run_id="a-different-run"))

    with pytest.raises(AgentOutputInvalidError, match="run"):
        UniverseSelectorAgent(client).rank(agent_input, max_selected=2)


# ---------------------------------------------------------------------------
# The generation schema stays in step with the vocabulary
# ---------------------------------------------------------------------------
def test_the_generation_schema_offers_exactly_the_allowed_reasons() -> None:
    """Built from the enums, so the offered and accepted vocabularies are one."""
    schema = ranking_output_schema()
    offered = set(
        schema["properties"]["rankings"]["items"]["properties"]["reasons"]["items"]["enum"]
    )

    assert offered == {reason.value for reason in UniverseSelectionReason}


def test_the_generation_schema_offers_exactly_the_allowed_confidences() -> None:
    schema = ranking_output_schema()
    offered = set(schema["properties"]["rankings"]["items"]["properties"]["confidence"]["enum"])

    assert offered == {level.value for level in ConfidenceLevel}


def test_the_generation_schema_forbids_extra_fields() -> None:
    schema = ranking_output_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["rankings"]["items"]["additionalProperties"] is False


def test_the_published_schema_and_the_enums_agree(load_schema) -> None:
    """The two must not drift: one is what we publish, the other what we accept."""
    published = load_schema("universe_agent_ranking")

    assert set(published["$defs"]["reason_code"]["enum"]) == {
        reason.value for reason in UniverseSelectionReason
    }


def test_a_response_validates_against_the_published_schema(agent_input, load_schema) -> None:
    from jsonschema import Draft202012Validator

    text = _response([_entry("SPY", rank=1), _entry("QQQ", selection="NOT_SELECTED")])
    ranking = UniverseSelectorAgent.parse(text)

    Draft202012Validator(load_schema("universe_agent_ranking")).validate(
        ranking.model_dump(mode="json")
    )


def test_the_agent_input_validates_against_its_published_schema(agent_input, load_schema) -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator(
        load_schema("universe_selection_input"),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(agent_input.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 39. Optional live-model check
# ---------------------------------------------------------------------------
@pytest.mark.llm
def test_a_real_model_satisfies_the_same_contract(agent_input) -> None:
    """Opt-in only: requires ALLOW_LIVE_TESTS=true and a real API key.

    Places no trade and reaches no broker — it asks one model to rank three
    fixture assets and asserts the answer passes exactly the validation a fake
    response passes. Ordinary ``pytest`` never runs it and never needs a key.
    """
    from trading_system.agents.anthropic_client import AnthropicLLMClient, anthropic_available
    from trading_system.agents.prompts import prompt_fingerprint
    from trading_system.infrastructure.settings import Settings, load_config

    if not anthropic_available():
        pytest.skip("the 'anthropic' package is not installed; install the 'llm' extra")

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY is not set")

    model_name = load_config(Settings().config_dir).universe.ai_ranking.model_name
    client = AnthropicLLMClient(
        api_key=key,
        model_name=model_name,
        prompt_version="live-check",
        prompt_fingerprint=prompt_fingerprint(PROMPT_NAME),
    )

    outcome = UniverseSelectorAgent(client, max_output_tokens=4000, effort="low").rank(
        agent_input, max_selected=2
    )

    assert len(outcome.ranking.selected) <= 2
    assert all(entry.reasons for entry in outcome.ranking.rankings)
    assert all(entry.symbol in {"SPY", "QQQ", "NVDA"} for entry in outcome.ranking.rankings)
    assert outcome.response.identity.model_name
