"""The Universe Selector agent — the system's first AI component.

Its entire job is ranking. It is handed a list of candidates that a
deterministic filter already validated, and it returns an ordering with
machine-readable reasons. It cannot add an asset, cannot restore an excluded
one, cannot exceed the size limit, cannot pick a contract, cannot express a
direction and cannot allocate money.

Those limits are not enforced by the prompt. The prompt states them; the code
guarantees them:

* the agent's *input* is :class:`~trading_system.universe.models.UniverseSelectionInput`,
  which structurally cannot carry a rejected asset;
* the agent's *output* passes through
  :func:`~trading_system.universe.validation.validate_ranking`, which rejects
  the whole response on any violation rather than repairing it;
* this module imports no broker, no provider, no repository and no network
  client — a test enforces that by parsing imports.

The generation-time JSON schema is built from the domain enums rather than
hand-written, so the vocabulary the model is offered and the vocabulary the
validator accepts cannot drift apart.
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
from trading_system.domain.enums import ConfidenceLevel, UniverseSelectionReason
from trading_system.universe.models import (
    UniverseAgentRanking,
    UniverseSelectionInput,
)
from trading_system.universe.validation import AgentOutputInvalidError, validate_ranking

__all__ = [
    "PROMPT_NAME",
    "RankingOutcome",
    "UniverseSelectorAgent",
    "ranking_output_schema",
]

PROMPT_NAME = "universe_selector"

#: Provider-reported stop reasons that mean "this is not an answer". Parsing a
#: refusal as content would turn a declined request into a silent empty
#: universe, which is precisely the failure the status enum exists to name.
_NON_ANSWER_STOP_REASONS = frozenset({"refusal", "max_tokens"})


class RankingOutcome:
    """A validated ranking together with the response that produced it."""

    __slots__ = ("ranking", "response")

    def __init__(self, ranking: UniverseAgentRanking, response: LLMResponse) -> None:
        self.ranking = ranking
        self.response = response


def ranking_output_schema() -> dict[str, Any]:
    """The JSON schema the model must generate against.

    Built from the enums so the offered vocabulary and the accepted vocabulary
    are the same object. Structurally simpler than the published
    ``schemas/universe_agent_ranking.json`` — no conditional subschemas — because
    generation-time constraints and validation-time constraints are different
    jobs: this one shapes the output, and the published schema plus
    :mod:`trading_system.universe.validation` decide whether to accept it.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["run_id", "rankings"],
        "properties": {
            "run_id": {"type": "string"},
            "rankings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["symbol", "selection", "reasons", "confidence"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "selection": {"enum": ["SELECTED", "NOT_SELECTED"]},
                        "rank": {
                            "type": ["integer", "null"],
                            "description": (
                                "Required for SELECTED, null for NOT_SELECTED. "
                                "Unique and contiguous from 1."
                            ),
                        },
                        "selection_score": {"type": ["number", "null"]},
                        "reasons": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"enum": [r.value for r in UniverseSelectionReason]},
                        },
                        "confidence": {"enum": [c.value for c in ConfidenceLevel]},
                        "rationale": {"type": ["string", "null"]},
                    },
                },
            },
            "notes": {"type": ["string", "null"]},
        },
    }


class UniverseSelectorAgent:
    """Ranks research candidates. Proposes; never decides.

    Takes an :class:`~trading_system.agents.base.LLMClient` rather than building
    one, so a test, a replayed fixture and a live model are the same code path
    and no test can accidentally reach a real API.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        max_output_tokens: int = 8000,
        timeout_seconds: float = 120.0,
        effort: str = "medium",
    ) -> None:
        self._client = client
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._effort = effort

    @property
    def prompt_version_fingerprint(self) -> str:
        """Hash of the prompt actually shipped, for the audit record."""
        return prompt_fingerprint(PROMPT_NAME)

    def rank(self, agent_input: UniverseSelectionInput, *, max_selected: int) -> RankingOutcome:
        """Ask the model to rank, then verify the answer against the input.

        Raises :class:`~trading_system.agents.base.AgentUnavailableError` if the
        model could not answer and
        :class:`~trading_system.agents.base.AgentInvalidOutputError` if it
        answered unusably. The caller distinguishes them because they are
        different operational problems.
        """
        request = StructuredRequest(
            system_prompt=load_prompt(PROMPT_NAME),
            user_content=self.build_user_content(agent_input, max_selected=max_selected),
            output_schema=ranking_output_schema(),
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            effort=self._effort,
        )

        started = time.perf_counter()
        response = self._client.complete(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if response.stop_reason in _NON_ANSWER_STOP_REASONS:
            raise AgentUnavailableError(
                f"the model returned no usable answer (stop_reason="
                f"{response.stop_reason}); a declined or truncated response is not "
                f"a ranking and is never parsed as one"
            )

        ranking = self.parse(response.text)
        validate_ranking(ranking, agent_input, max_selected=max_selected)

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
        return RankingOutcome(ranking, response)

    # --- request construction ---------------------------------------------
    @staticmethod
    def build_user_content(agent_input: UniverseSelectionInput, *, max_selected: int) -> str:
        """Serialise the input contract for the model.

        Sends the contract object and nothing else — no repository handle, no
        raw provider payload, no historical series. The agent's whole view of
        the world is what a deterministic layer decided it may see
        (Milestone 4 brief section 18).
        """
        payload = agent_input.model_dump(mode="json")
        return json.dumps(
            {
                "instruction": (
                    "Rank these candidates for research priority. Return one entry per "
                    "candidate. Select at most the maximum below; selecting fewer, or "
                    "none, is valid."
                ),
                "run_id": agent_input.run_id,
                "as_of": payload["as_of"],
                "max_selected_assets": max_selected,
                "universe_source": payload["universe_source"],
                "deterministic_filter_config": payload["deterministic_filter_config"],
                "candidate_assets": payload["candidate_assets"],
            },
            indent=2,
            sort_keys=True,
        )

    # --- response parsing --------------------------------------------------
    @staticmethod
    def parse(text: str) -> UniverseAgentRanking:
        """Turn the model's text into a ranking, or fail loudly.

        Tolerates a markdown fence, because that is a formatting habit rather
        than a semantic error. Tolerates nothing else: no regex scraping of
        symbols out of prose, no "best effort" reconstruction of a truncated
        object. A response we had to guess at is a response we cannot audit.
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
            return UniverseAgentRanking.model_validate(payload)
        except ValidationError as exc:
            raise AgentInvalidOutputError(
                f"the model's response does not satisfy the ranking contract: {exc}"
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
__all__ += ["AgentInvalidOutputError", "AgentOutputInvalidError", "AgentUnavailableError"]
