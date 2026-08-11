"""The boundary between an agent and whatever model answers it.

Agents in this system propose and explain. They never move money, never reach a
broker, and never retrieve their own data — an agent is handed a validated
input contract and returns a structured object that a deterministic layer then
checks. This module defines the narrow surface that lets that hold:

* :class:`LLMClient` — a protocol, not a vendor. The agent depends on "something
  that can answer a structured request", so swapping providers, replaying a
  recorded response, or running the whole suite with no API key at all are the
  same operation.
* :class:`StructuredRequest` / :class:`LLMResponse` — a JSON-schema-constrained
  request and the raw text that came back, plus the metadata needed to say
  *which model produced this* months later.
* The error hierarchy — because "the model is unreachable" and "the model
  answered something unusable" demand different responses, and collapsing them
  into one exception would hide which one happened.

There is deliberately no retry, no fallback model and no default client here.
A failure surfaces to the caller, which decides — and in this system the
caller's default is to fail closed rather than to trade on a degraded answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AgentError",
    "AgentInvalidOutputError",
    "AgentTimeoutError",
    "AgentUnavailableError",
    "LLMClient",
    "LLMResponse",
    "ModelIdentity",
    "StructuredRequest",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AgentError(RuntimeError):
    """Base class for every agent failure."""


class AgentUnavailableError(AgentError):
    """The model could not be reached, or is not configured.

    Distinct from a bad answer on purpose: an unreachable model is an
    operational problem, and a system that treated it as "the agent declined"
    would silently produce an empty universe every time its API key expired.
    """


class AgentTimeoutError(AgentUnavailableError):
    """The model did not answer within its configured bound.

    A subclass of unavailable rather than a sibling: from the caller's
    perspective a request that never returns and one that cannot be sent are
    the same problem — no answer.
    """


class AgentInvalidOutputError(AgentError):
    """The model answered, but the answer cannot be used.

    Malformed JSON, a missing field, a value outside its enum, or a claim the
    supplied evidence contradicts. Never repaired — see
    :mod:`trading_system.universe.validation`.
    """


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Which model answered, precisely enough to reproduce the call.

    ``prompt_version`` and ``prompt_fingerprint`` are both recorded because they
    fail differently: the version is what an operator declares, the fingerprint
    is what the file actually contained. A prompt edited without a version bump
    is exactly the change that would otherwise be invisible in the audit trail.
    """

    provider: str
    model_name: str
    prompt_version: str
    prompt_fingerprint: str
    model_version: str | None = None

    @property
    def agent_version(self) -> str:
        return f"{self.prompt_version}+{self.prompt_fingerprint[:12]}"


@dataclass(frozen=True, slots=True)
class StructuredRequest:
    """A request whose answer must satisfy a JSON schema.

    The schema is part of the request rather than a post-hoc check so the model
    is constrained at generation time; the response is validated again on
    arrival regardless, because a constraint enforced elsewhere is not a
    guarantee we can audit.
    """

    system_prompt: str
    user_content: str
    output_schema: Mapping[str, Any]
    max_output_tokens: int = 8000
    timeout_seconds: float = 120.0
    #: Reasoning depth hint. Ranking pre-validated candidates on supplied
    #: evidence is not a deep-reasoning task; the caller sets this from config.
    effort: str = "medium"


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """What came back, plus what it cost and who produced it."""

    text: str
    identity: ModelIdentity
    generated_at: datetime
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: Provider-reported stop reason. A refusal is a real outcome and must not
    #: be parsed as though it were content.
    stop_reason: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class LLMClient(Protocol):
    """Anything that can answer a :class:`StructuredRequest`.

    Implementations must raise :class:`AgentUnavailableError` rather than
    returning an empty or partial answer: a client that invented a response on
    failure would be indistinguishable from a model that produced one.
    """

    @property
    def identity(self) -> ModelIdentity:
        """Who this client speaks to, for the audit record."""
        ...

    def complete(self, request: StructuredRequest) -> LLMResponse:
        """Send ``request`` and return the model's answer."""
        ...
