"""The Strategy Selector agent — the system's third AI component.

Its entire job is one question, asked once per underlying:

    Given this research conclusion, which of these configured strategies best
    expresses it — or none?

It answers with an action, a strategy id, reason codes from a closed vocabulary
and a rationale. It does not choose a strike, an expiration, a contract, a
quantity or an amount of money, and it is never in a position to: those are the
deterministic contract selector's and the risk engine's, in that order.

Those limits are not enforced by the prompt. The prompt states them; the code
guarantees them:

* the agent's *input* is
  :class:`~trading_system.strategies.models.StrategySelectionInput`, which
  carries a projection of a research report and the metadata of the strategies
  eligible for its hypothesis — **no option chain, no strike list, no contract
  id, no account, no cash**. An agent that is never shown a contract cannot
  select one, which is a stronger guarantee than asking it not to;
* the agent's *output* has no field for a strike, an expiry, a right, a
  quantity or a price, and ``extra="forbid"`` means a response that invented
  one fails to parse rather than having it silently dropped;
* the response then passes through
  :func:`~trading_system.strategies.validation.validate_agent_output`, which
  rejects the whole decision on any violation rather than repairing it;
* this module imports no broker, no provider, no repository, no chain reader
  and no network client — a test enforces that by parsing imports,
  transitively.

The generation-time JSON schema is built from the domain enums and from the
strategies the input actually offered, so the vocabulary the model is given and
the vocabulary the validator accepts cannot drift apart, and a strategy that
was not offered is not even expressible in the response.
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
    StrategyAction,
    StrategySelectionReason,
)
from trading_system.infrastructure.settings import StrategyStageConfig
from trading_system.strategies.models import StrategyAgentOutput, StrategySelectionInput
from trading_system.strategies.validation import (
    StrategyOutputInvalidError,
    validate_agent_output,
)

__all__ = [
    "PROMPT_NAME",
    "StrategyOutcome",
    "StrategySelectorAgent",
    "strategy_output_schema",
]

PROMPT_NAME = "strategy_selector"

#: Provider-reported stop reasons that mean "this is not an answer". Parsing a
#: refusal or a truncation as content would turn a declined request into a
#: confident-looking trade proposal, which is precisely the failure the status
#: enum exists to name.
_NON_ANSWER_STOP_REASONS = frozenset({"refusal", "max_tokens"})


class StrategyOutcome:
    """A validated decision together with the response that produced it."""

    __slots__ = ("output", "response")

    def __init__(self, output: StrategyAgentOutput, response: LLMResponse) -> None:
        self.output = output
        self.response = response


def strategy_output_schema(selection_input: StrategySelectionInput) -> dict[str, Any]:
    """The JSON schema the model must generate against.

    ``selected_strategy`` is enumerated from the strategies this input actually
    offered, so a strategy the deterministic layer did not admit has no
    representation in the response at all. That is the same technique the
    universe input uses to make a rejected asset inexpressible, applied one
    stage later.

    Note what has no representation here: a strike, an expiration, a right, a
    delta, a contract id, a quantity, a limit price or a currency amount. The
    agent cannot express them because the schema gives it nowhere to put them.
    """
    offered = sorted(option.strategy_id.value for option in selection_input.eligible_strategies)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["run_id", "symbol", "action", "confidence", "reasons", "rationale"],
        "properties": {
            "run_id": {"type": "string"},
            "symbol": {"type": "string"},
            "action": {
                "enum": [a.value for a in StrategyAction],
                "description": (
                    "BUY names one of the offered strategies. NO_TRADE names none and is a "
                    "correct answer whenever no offered strategy expresses the research."
                ),
            },
            "selected_strategy": {
                "enum": [*offered, None],
                "description": (
                    "Required for BUY, null for NO_TRADE. Must be one of the strategies "
                    "offered in this request; nothing else is tradeable."
                ),
            },
            "confidence": {
                "enum": [c.value for c in ConfidenceLevel],
                "description": (
                    "A band, never a probability. It may not exceed the research "
                    "confidence: a strategy choice cannot be more certain than the view it "
                    "expresses."
                ),
            },
            "reasons": {
                "type": "array",
                "minItems": 1,
                "items": {"enum": [r.value for r in StrategySelectionReason]},
                "description": (
                    "Codes from this closed vocabulary. Each is checked against the "
                    "research report; a code the report contradicts rejects the decision."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Why this strategy matches this hypothesis, referring to the research. "
                    "No strike, no expiration date, no quantity, no amount of money."
                ),
            },
            "notes": {"type": ["string", "null"]},
        },
    }


class StrategySelectorAgent:
    """Chooses a configured strategy, or none. Proposes; never decides.

    Takes an :class:`~trading_system.agents.base.LLMClient` rather than building
    one, so a test, a replayed fixture and a live model are the same code path
    and no test can accidentally reach a real API.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        config: StrategyStageConfig,
        max_output_tokens: int = 6000,
        timeout_seconds: float = 120.0,
        effort: str = "medium",
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

    def select(self, selection_input: StrategySelectionInput) -> StrategyOutcome:
        """Ask the model to choose, then verify the answer against the input.

        Raises :class:`~trading_system.agents.base.AgentUnavailableError` if the
        model could not answer,
        :class:`~trading_system.agents.base.AgentInvalidOutputError` if it
        answered unusably, and
        :class:`~trading_system.strategies.validation.StrategyOutputInvalidError`
        if the answer is well-formed but the research does not license it. The
        caller distinguishes all three because they are different problems.
        """
        request = StructuredRequest(
            system_prompt=load_prompt(PROMPT_NAME),
            user_content=self.build_user_content(selection_input),
            output_schema=strategy_output_schema(selection_input),
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
                f"a declined or truncated response is not a decision and is never parsed "
                f"as one"
            )

        output = self.parse(response.text)
        validate_agent_output(output, selection_input, config=self._config)

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
        return StrategyOutcome(output, response)

    # --- request construction ---------------------------------------------
    def build_user_content(self, selection_input: StrategySelectionInput) -> str:
        """Serialise the input contract for the model.

        Sends the contract object and nothing else — no repository handle, no
        option chain, no broker state, no account balance. The agent's whole
        view of the world is a research conclusion and a list of configured
        strategies.

        Raises :class:`~trading_system.agents.base.AgentInvalidOutputError` if
        the payload exceeds the configured character ceiling. Failing is
        deliberate: silently sending a truncated payload would let the agent
        choose from a strategy list it believed was complete.
        """
        payload = selection_input.model_dump(mode="json")
        content = json.dumps(
            {
                "instruction": (
                    "Choose the one configured strategy that best expresses this research "
                    "conclusion, or answer NO_TRADE. You are choosing a strategy, not a "
                    "contract: no strike, no expiration, no quantity, no money. NO_TRADE is "
                    "a correct answer, not a failure."
                ),
                "run_id": selection_input.run_id,
                "symbol": selection_input.symbol,
                "as_of": payload["as_of"],
                "research": payload["research"],
                "eligible_strategies": payload["eligible_strategies"],
            },
            indent=2,
            sort_keys=True,
        )

        ceiling = self._config.limits.max_input_characters
        if len(content) > ceiling:
            raise AgentInvalidOutputError(
                f"the strategy input for {selection_input.symbol} is {len(content)} "
                f"characters, beyond the configured ceiling of {ceiling}. It is not "
                f"truncated: an agent choosing from a silently shortened list would believe "
                f"it saw every option. Lower the limits in config/strategy.yaml."
            )
        return content

    # --- response parsing --------------------------------------------------
    @staticmethod
    def parse(text: str) -> StrategyAgentOutput:
        """Turn the model's text into a decision, or fail loudly.

        Tolerates a markdown fence, because that is a formatting habit rather
        than a semantic error. Tolerates nothing else: no regex scraping of a
        strategy name out of prose, no "best effort" reconstruction of a
        truncated object. A response we had to guess at is a response we cannot
        audit — and here, guessing would mean guessing at a trade.
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
            return StrategyAgentOutput.model_validate(payload)
        except ValidationError as exc:
            raise AgentInvalidOutputError(
                f"the model's response does not satisfy the strategy contract: {exc}"
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
__all__ += ["AgentInvalidOutputError", "AgentUnavailableError", "StrategyOutputInvalidError"]
