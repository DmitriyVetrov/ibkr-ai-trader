"""The ``llm.generate`` span, shared by all three agents (Milestone 11).

One helper, used identically by the universe selector, the market researcher
and the strategy selector, so the AI telemetry contract is written once and
cannot drift between them.

.. code-block:: python

    with llm_span(agent="market_researcher", client=self._client) as call:
        response = self._client.complete(request)
        call.record(response)

**What is emitted:** the agent, its version, the provider, the model, the
outcome, token counts and latency. Cost and shape.

**What is never emitted:** the prompt, the model's response, the research
context, the evidence, the candidate list — any of it, under any configuration.
Those live in the immutable domain artifact, where they can be audited, where
the retention policy is this system's own, and where nobody has to trust a
telemetry backend with the reasoning behind a trade. The privacy filter would
drop them anyway (``prompt`` and ``completion`` are forbidden substrings), but
the real guarantee is that this module never has them: it sees a *response
object*, and it reads five numbers off it.

Like everything else in this package: a no-op when telemetry is off, and it
never raises. An agent's model call must not be able to fail because a span
could not be started.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import (
    AGENT_NAME,
    AGENT_VERSION,
    LLM_DURATION_MS,
    LLM_INPUT_TOKENS,
    LLM_MODEL,
    LLM_OPERATION,
    LLM_OUTPUT_TOKENS,
    LLM_PROVIDER,
    LLM_STATUS,
)
from trading_system.observability.tracing import annotate, operation

__all__ = ["LlmCall", "llm_span"]


@dataclass(slots=True)
class LlmCall:
    """A model call in progress. :meth:`record` puts the outcome on the span."""

    span: Any
    agent: str
    provider: str
    model: str
    labels: dict[str, str]

    def record(self, response: Any) -> None:
        """Note what the call cost and how it went. Never raises.

        Reads only the response's *metadata*. ``response.text`` is deliberately
        untouched: the answer belongs in the artifact the caller is about to
        validate and store, not in a span.
        """
        try:
            attributes: dict[str, Any] = {LLM_STATUS: "OK"}
            for name, value in (
                (LLM_INPUT_TOKENS, getattr(response, "input_tokens", None)),
                (LLM_OUTPUT_TOKENS, getattr(response, "output_tokens", None)),
                (LLM_DURATION_MS, getattr(response, "latency_ms", None)),
            ):
                if value is not None:
                    attributes[name] = value
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason:
                attributes["llm.stop_reason"] = str(stop_reason)
            annotate(self.span, attributes)

            latency = getattr(response, "latency_ms", None)
            if latency is not None:
                _metrics.record_duration(
                    _metrics.LLM_LATENCY, float(latency) / 1000.0, labels=self.labels
                )
            tokens = (getattr(response, "input_tokens", 0) or 0) + (
                getattr(response, "output_tokens", 0) or 0
            )
            if tokens:
                _metrics.record_count(_metrics.LLM_TOKENS_TOTAL, int(tokens), labels=self.labels)
        except Exception:  # pragma: no cover - telemetry is never load-bearing
            return

    def record_error(self, error: BaseException) -> None:
        """Note that the call failed, and what kind of failure it was."""
        try:
            annotate(self.span, {LLM_STATUS: "ERROR", "error.type": type(error).__name__})
            _metrics.record_count(
                _metrics.LLM_ERRORS_TOTAL,
                1,
                labels={**self.labels, "error": type(error).__name__},
            )
        except Exception:  # pragma: no cover - telemetry is never load-bearing
            return


@contextmanager
def llm_span(*, agent: str, client: Any, operation_name: str = "generate") -> Iterator[LlmCall]:
    """Wrap one model call in an ``llm.generate`` span. Never changes the call.

    ``client`` is read for its :class:`~trading_system.agents.base.ModelIdentity`
    only — provider, model name, agent version. A client that cannot report one
    yields ``unknown`` rather than raising: an agent must not fail because
    telemetry could not describe it.
    """
    provider, model, version = _identity(client)
    labels = {"agent": agent, "model": model, "provider": provider}

    with operation(
        "llm.generate",
        attributes={
            AGENT_NAME: agent,
            AGENT_VERSION: version,
            LLM_PROVIDER: provider,
            LLM_MODEL: model,
            LLM_OPERATION: operation_name,
        },
    ) as span:
        call = LlmCall(span=span, agent=agent, provider=provider, model=model, labels=labels)
        _count(_metrics.LLM_REQUESTS_TOTAL, labels)
        try:
            yield call
        except BaseException as error:
            call.record_error(error)
            raise


def _identity(client: Any) -> tuple[str, str, str]:
    """Provider, model and agent version, or ``unknown`` for each."""
    try:
        identity = client.identity
        return (
            str(getattr(identity, "provider", "unknown")),
            str(getattr(identity, "model_name", "unknown")),
            str(getattr(identity, "agent_version", "unknown")),
        )
    except Exception:  # pragma: no cover - telemetry is never load-bearing
        return "unknown", "unknown", "unknown"


def _count(instrument: str, labels: dict[str, str]) -> None:
    try:
        _metrics.record_count(instrument, 1, labels=labels)
    except Exception:  # pragma: no cover - telemetry is never load-bearing
        return
