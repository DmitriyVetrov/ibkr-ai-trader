"""The Market Researcher agent — the system's first analytical component.

Its entire job is one question, asked once per underlying:

    Given information actually available as of T, what is the most defensible
    expectation for this underlying over the configured short-term horizon, and
    what evidence supports that view?

It answers with a hypothesis (A-E), a confidence band, the evidence behind it,
the events that matter, the risks, and what would prove it wrong. It does not
choose an option, a strategy, a position size or a moment to trade.

Those limits are not enforced by the prompt. The prompt states them; the code
guarantees them:

* the agent's *input* is :class:`~trading_system.research.models.ResearchInput`,
  assembled point-in-time from the repository. It carries no strike, no expiry
  date, no right and no contract id — the option context it does carry is
  aggregate, keyed by days to expiration — so the agent is never shown a
  contract it could name;
* the agent cites evidence by **id**, and there is no field on its response for
  a source name, a URL or a publication date. Those facts are copied from the
  input into the report. An id the input did not contain is rejected by
  :func:`~trading_system.research.validation.validate_agent_output`, so an
  invented source is not merely discouraged, it is detectable;
* a violating response is rejected in full and never repaired;
* this module imports no broker, no provider, no repository and no network
  client — a test enforces that by parsing imports, transitively.

The generation-time JSON schema is built from the domain enums rather than
hand-written, so the vocabulary the model is offered and the vocabulary the
validator accepts cannot drift apart.

One research context per underlying. Several assets in one request would let
one company's story colour another's, and the resulting outlook could not be
attributed to either (Milestone 5 brief section 33).
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import ValidationError

from trading_system.agents.base import (
    AgentInvalidOutputError,
    AgentUnavailableError,
    LLMClient,
    LLMResponse,
    StructuredRequest,
)
from trading_system.agents.prompts import load_prompt, prompt_fingerprint
from trading_system.domain.enums import (
    ConfidenceLevel,
    Direction,
    EvidenceDirection,
    EvidenceStance,
    ExpectedMagnitude,
    MarketHypothesis,
    RelevanceLevel,
    RiskCategory,
)
from trading_system.infrastructure.settings import ResearchConfig
from trading_system.research.models import ResearchAgentOutput, ResearchInput
from trading_system.research.validation import (
    ResearchOutputInvalidError,
    validate_agent_output,
)

__all__ = [
    "PROMPT_NAME",
    "MarketResearcherAgent",
    "ResearchOutcome",
    "research_output_schema",
]

PROMPT_NAME = "market_researcher"

#: Provider-reported stop reasons that mean "this is not an answer". Parsing a
#: refusal or a truncation as content would turn a declined request into a
#: confident-looking outlook, which is precisely the failure the status enum
#: exists to name.
_NON_ANSWER_STOP_REASONS = frozenset({"refusal", "max_tokens"})


class ResearchOutcome:
    """A validated outlook together with the response that produced it."""

    __slots__ = ("output", "response")

    def __init__(self, output: ResearchAgentOutput, response: LLMResponse) -> None:
        self.output = output
        self.response = response


def research_output_schema() -> dict[str, Any]:
    """The JSON schema the model must generate against.

    Built from the enums so the offered vocabulary and the accepted vocabulary
    are the same object. Structurally simpler than the published
    ``schemas/research_agent_output.json`` — no conditional subschemas — because
    generation-time constraints and validation-time constraints are different
    jobs: this one shapes the output, and the published schema plus
    :mod:`trading_system.research.validation` decide whether to accept it.

    Note what has no representation here: a strike, an expiry, a right, a
    delta, a strategy, a quantity, a currency amount or a probability. The
    agent cannot express them because the schema gives it nowhere to put them.
    """
    claim_support = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Evidence ids backing this claim. An empty list is allowed and marks the "
            "claim UNSUPPORTED in the stored report; it is never grounds for inventing "
            "an id."
        ),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "run_id",
            "symbol",
            "hypothesis",
            "confidence",
            "direction",
            "expected_magnitude",
            "horizon_days",
            "thesis",
            "expected_behavior",
            "evidence",
            "risks",
            "invalidation_conditions",
        ],
        "properties": {
            "run_id": {"type": "string"},
            "symbol": {"type": "string"},
            "hypothesis": {
                "enum": [h.value for h in MarketHypothesis],
                "description": (
                    "A=strong move, no specific catalyst required, direction unknown; "
                    "B=predominantly up; C=predominantly down; D=sharp move around a "
                    "specific identified event, direction unknown; E=other, explanation "
                    "required."
                ),
            },
            "confidence": {
                "enum": [c.value for c in ConfidenceLevel],
                "description": "A band, never a probability. No percentages.",
            },
            "direction": {"enum": [d.value for d in Direction]},
            "expected_magnitude": {"enum": [m.value for m in ExpectedMagnitude]},
            "horizon_days": {"type": "integer"},
            "thesis": {"type": "string"},
            "expected_behavior": {"type": "string"},
            "explanation": {"type": ["string", "null"]},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "evidence_id",
                        "claim",
                        "direction",
                        "stance",
                        "relevance",
                        "confidence",
                    ],
                    "properties": {
                        "evidence_id": {
                            "type": "string",
                            "description": (
                                "Must be one of the evidence ids supplied in the input. "
                                "There is no field here for a source name, URL or date: "
                                "those are copied from the input."
                            ),
                        },
                        "claim": {"type": "string"},
                        "direction": {"enum": [d.value for d in EvidenceDirection]},
                        "stance": {"enum": [s.value for s in EvidenceStance]},
                        "relevance": {"enum": [r.value for r in RelevanceLevel]},
                        "confidence": {"enum": [c.value for c in ConfidenceLevel]},
                    },
                },
            },
            "key_events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["event_id", "expected_relevance", "directional_uncertainty"],
                    "properties": {
                        "event_id": {
                            "type": "string",
                            "description": "Must be one of the event ids supplied in the input.",
                        },
                        "expected_relevance": {"enum": [r.value for r in RelevanceLevel]},
                        "directional_uncertainty": {"type": "boolean"},
                        "rationale": {"type": ["string", "null"]},
                    },
                },
            },
            "bullish_catalysts": {"type": "array", "items": _catalyst_schema(claim_support)},
            "bearish_catalysts": {"type": "array", "items": _catalyst_schema(claim_support)},
            "risks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["category", "description"],
                    "properties": {
                        "category": {"enum": [c.value for c in RiskCategory]},
                        "description": {"type": "string"},
                        "evidence_ids": claim_support,
                    },
                },
            },
            "invalidation_conditions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["condition"],
                    "properties": {
                        "condition": {"type": "string"},
                        "observable": {
                            "type": ["string", "null"],
                            "description": "What one would have to look at to decide.",
                        },
                        "evidence_ids": claim_support,
                    },
                },
            },
            "contradiction_resolution": {"type": ["string", "null"]},
            "missing_information": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": ["string", "null"]},
        },
    }


def _catalyst_schema(claim_support: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary"],
        "properties": {
            "summary": {"type": "string"},
            "evidence_ids": claim_support,
        },
    }


class MarketResearcherAgent:
    """Forms a defensible short-term outlook. Proposes; never decides.

    Takes an :class:`~trading_system.agents.base.LLMClient` rather than building
    one, so a test, a replayed fixture and a live model are the same code path
    and no test can accidentally reach a real API.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        config: ResearchConfig,
        max_output_tokens: int = 12000,
        timeout_seconds: float = 180.0,
        effort: str = "high",
    ) -> None:
        self._client = client
        self._config = config
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._effort = effort

    @property
    def prompt_version_fingerprint(self) -> str:
        """Hash of the prompt actually shipped, for the audit record."""
        return prompt_fingerprint(PROMPT_NAME)

    def research(self, research_input: ResearchInput) -> ResearchOutcome:
        """Ask the model for an outlook, then verify the answer against the input.

        Raises :class:`~trading_system.agents.base.AgentUnavailableError` if the
        model could not answer, :class:`~trading_system.agents.base.AgentInvalidOutputError`
        if it answered unusably, and
        :class:`~trading_system.research.validation.ResearchOutputInvalidError`
        if the answer is well-formed but the evidence does not license it. The
        caller distinguishes all three because they are different problems.
        """
        request = StructuredRequest(
            system_prompt=load_prompt(PROMPT_NAME),
            user_content=self.build_user_content(research_input),
            output_schema=research_output_schema(),
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            effort=self._effort,
        )

        started = time.perf_counter()
        response = self._client.complete(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if response.stop_reason in _NON_ANSWER_STOP_REASONS:
            raise AgentUnavailableError(
                f"the model returned no usable answer (stop_reason={response.stop_reason}); "
                f"a declined or truncated response is not an outlook and is never parsed "
                f"as one"
            )

        output = self.parse(response.text)
        validate_agent_output(output, research_input, config=self._config)

        if response.latency_ms is None:
            response = LLMResponse(
                text=response.text,
                identity=response.identity,
                generated_at=response.generated_at,
                latency_ms=elapsed_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                stop_reason=response.stop_reason,
                notes=response.notes,
            )
        return ResearchOutcome(output, response)

    # --- request construction ---------------------------------------------
    def build_user_content(self, research_input: ResearchInput) -> str:
        """Serialise the input contract for the model.

        Sends the contract object and nothing else — no repository handle, no
        raw provider payload, no unbounded history. The agent's whole view of
        the world is what a deterministic layer decided it may see, assembled
        point-in-time.

        Raises :class:`~trading_system.agents.base.AgentInvalidOutputError` if
        the payload exceeds the configured character ceiling. Failing is
        deliberate: silently sending a truncated payload would let the agent
        reason from a partial picture while believing it was complete.
        """
        payload = research_input.model_dump(mode="json")
        content = json.dumps(
            {
                "instruction": (
                    "Form the most defensible expectation for this underlying over the "
                    "stated horizon, using only the facts below. Cite evidence by its "
                    "evidence_id. If the evidence does not support a defensible view, "
                    "answer E with an explanation — that is a correct answer, not a "
                    "failure."
                ),
                "run_id": research_input.run_id,
                "symbol": research_input.symbol,
                "as_of": payload["as_of"],
                "horizon": payload["horizon"],
                "market_snapshot": payload["market_snapshot"],
                "historical_context": payload["historical_context"],
                "option_context": payload["option_context"],
                "market_context": payload["market_context"],
                "news": payload["news"],
                "events": payload["events"],
                "regulatory_events": payload["regulatory_events"],
                "fundamentals": payload["fundamentals"],
                "observations": payload["observations"],
                "data_quality_summary": payload["data_quality_summary"],
                "window": payload["window"],
                "source_policy": payload["source_policy"],
            },
            indent=2,
            sort_keys=True,
        )

        ceiling = self._config.limits.max_input_characters
        if len(content) > ceiling:
            raise AgentInvalidOutputError(
                f"the research input for {research_input.symbol} is {len(content)} characters, "
                f"beyond the configured ceiling of {ceiling}. It is not truncated: an agent "
                f"reasoning from a silently shortened input would believe it saw everything. "
                f"Lower the window or the item limits in config/research.yaml."
            )
        return content

    # --- response parsing --------------------------------------------------
    @staticmethod
    def parse(text: str) -> ResearchAgentOutput:
        """Turn the model's text into an outlook, or fail loudly.

        Tolerates a markdown fence, because that is a formatting habit rather
        than a semantic error. Tolerates nothing else: no regex scraping of a
        hypothesis out of prose, no "best effort" reconstruction of a truncated
        object. A response we had to guess at is a response we cannot audit.

        ``support`` on catalysts, risks and invalidation conditions needs no
        handling here: :class:`~trading_system.research.models.SupportedClaim`
        recomputes it from the citations and discards anything the model
        supplied, so an unsupported claim cannot label itself supported.
        """
        cleaned = _strip_code_fence(text).strip()
        if not cleaned:
            raise AgentInvalidOutputError("the model returned an empty response")

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise AgentInvalidOutputError(f"the model's response is not valid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise AgentInvalidOutputError(f"expected a JSON object, got {type(payload).__name__}")

        try:
            return ResearchAgentOutput.model_validate(payload)
        except ValidationError as exc:
            raise AgentInvalidOutputError(
                f"the model's response does not satisfy the research contract: {exc}"
            ) from exc


def _strip_code_fence(text: str) -> str:
    """Remove a surrounding ```json fence if the model added one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body)


# Re-exported so callers can catch one family without importing two modules.
__all__ += ["AgentInvalidOutputError", "AgentUnavailableError", "ResearchOutputInvalidError"]
