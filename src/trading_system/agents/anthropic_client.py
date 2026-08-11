"""Anthropic-backed :class:`~trading_system.agents.base.LLMClient`.

The only module in the system that talks to a model provider, and the only one
that imports the ``anthropic`` SDK — imported lazily inside the constructor so
that the package, the whole test suite and every offline command run without it
installed. That mirrors how ``ib_async`` is treated: an optional dependency of
one adapter, never a prerequisite for the domain.

Three properties matter here, all of them for the same reason — an autonomous
system must never mistake a failure for an answer:

* **Structured output.** The response is constrained at generation time by the
  caller's JSON schema, so the agent parses a schema-shaped object rather than
  scraping prose.
* **Bounded.** Every request carries an explicit timeout. An unbounded call in
  a scheduled job is a hung process.
* **Honest about failure.** Unreachable, unauthenticated, rate-limited, timed
  out and *declined* are all raised, never smoothed into an empty ranking. A
  refusal in particular arrives as a successful HTTP response, so it is checked
  explicitly instead of being read as content.

No credential is read here. The key comes from :class:`Settings`, which reads
the environment; nothing about it is ever logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from trading_system.agents.base import (
    AgentTimeoutError,
    AgentUnavailableError,
    LLMResponse,
    ModelIdentity,
    StructuredRequest,
)

__all__ = ["ANTHROPIC_PROVIDER", "AnthropicLLMClient", "anthropic_available"]

ANTHROPIC_PROVIDER = "ANTHROPIC"


def anthropic_available() -> bool:
    """Whether the SDK is installed. Cheap, and performs no network call."""
    from importlib.util import find_spec

    return find_spec("anthropic") is not None


class AnthropicLLMClient:
    """Speaks to the Anthropic Messages API with a schema-constrained response."""

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        prompt_version: str,
        prompt_fingerprint: str,
        max_retries: int = 2,
    ) -> None:
        if not api_key.strip():
            raise AgentUnavailableError(
                "ANTHROPIC_API_KEY is not set; the ranking agent cannot be reached. "
                "Set it in the environment, or disable AI ranking in config/universe.yaml "
                "— the system will not substitute an ordering of its own."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise AgentUnavailableError(
                "the 'anthropic' package is not installed; install the optional "
                "'llm' extra (uv pip install -e '.[llm]') to enable AI ranking"
            ) from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
        self._identity = ModelIdentity(
            provider=ANTHROPIC_PROVIDER,
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_fingerprint=prompt_fingerprint,
        )

    @property
    def identity(self) -> ModelIdentity:
        return self._identity

    def complete(self, request: StructuredRequest) -> LLMResponse:
        """Send one structured request. Raises rather than degrading."""
        started = datetime.now(UTC)
        client = self._client.with_options(timeout=request.timeout_seconds)

        try:
            message = client.messages.create(
                model=self._identity.model_name,
                max_tokens=request.max_output_tokens,
                system=request.system_prompt,
                messages=[{"role": "user", "content": request.user_content}],
                output_config={
                    "effort": request.effort,
                    "format": {
                        "type": "json_schema",
                        "schema": dict(request.output_schema),
                    },
                },
            )
        except Exception as exc:
            raise self._translate(exc) from exc

        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "refusal":
            # A refusal is a successful HTTP response with no content. Reading
            # it as an answer would turn a declined request into an empty
            # universe that looks like a considered decision.
            detail = _refusal_detail(message)
            raise AgentUnavailableError(
                f"the model declined this request ({detail}); no ranking was produced"
            )

        finished = datetime.now(UTC)
        usage = getattr(message, "usage", None)
        return LLMResponse(
            text=_first_text(message),
            identity=ModelIdentity(
                provider=self._identity.provider,
                model_name=self._identity.model_name,
                prompt_version=self._identity.prompt_version,
                prompt_fingerprint=self._identity.prompt_fingerprint,
                model_version=getattr(message, "model", None),
            ),
            generated_at=finished,
            latency_ms=(finished - started).total_seconds() * 1000,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            stop_reason=stop_reason,
        )

    def _translate(self, exc: Exception) -> Exception:
        """Map SDK exceptions onto the agent error hierarchy.

        Matched on the SDK's typed classes rather than on message text: a
        string match would silently stop working the moment a message is
        reworded, and the timeout case is the one that must never be missed.
        """
        anthropic = self._anthropic
        if isinstance(exc, anthropic.APITimeoutError):
            return AgentTimeoutError(f"the model did not answer within its timeout: {exc}")
        if isinstance(exc, anthropic.AuthenticationError):
            return AgentUnavailableError("the Anthropic API rejected the configured credentials")
        if isinstance(exc, anthropic.RateLimitError):
            return AgentUnavailableError(f"the Anthropic API is rate limiting this key: {exc}")
        if isinstance(exc, anthropic.APIConnectionError):
            return AgentUnavailableError(f"the Anthropic API is unreachable: {exc}")
        if isinstance(exc, anthropic.APIStatusError):
            return AgentUnavailableError(
                f"the Anthropic API returned {exc.status_code}: {getattr(exc, 'message', exc)}"
            )
        return AgentUnavailableError(f"the ranking request failed: {type(exc).__name__}: {exc}")


def _first_text(message: Any) -> str:
    """The response's text blocks, concatenated.

    Blocks are filtered by type rather than indexed: a thinking block at
    position zero would otherwise be read as the answer.
    """
    parts = [
        block.text
        for block in getattr(message, "content", []) or []
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    return "\n".join(parts)


def _refusal_detail(message: Any) -> str:
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None) if details is not None else None
    return f"category={category}" if category else "no category reported"
