"""What telemetry may never carry, enforced (Milestone 11).

Telemetry is **not an audit archive**. The archive is the immutable domain
artifact — a research report, a strategy decision, an execution record — and a
span carries the *id* of one, never its contents. That is not a stylistic
preference: telemetry leaves this process, is retained by systems with
different access controls, and is frequently the least-locked-down thing in a
deployment.

This module is the last line of defence, and it is written to be the boring
one. Every attribute that reaches a span or a metric goes through
:func:`sanitize`, which:

* **drops** any attribute whose name matches a forbidden substring — password,
  secret, token, api_key, credential, prompt, completion, portfolio, balance,
  account_number — at any nesting depth;
* **masks** anything that looks like an account identifier, so the shape of the
  value cannot leak it even under a name nobody thought to forbid;
* **truncates** long strings, because a long value in a span is a payload
  trying to become an archive;
* **drops monetary values** unless configuration explicitly permits them, since
  counts, durations, statuses and references answer operational questions and a
  money figure in a trace is financial truth in the wrong system;
* **refuses nothing loudly.** A dropped attribute is dropped. Raising here
  would let a privacy rule take down a trading operation, which is the one
  thing this whole package is forbidden to do.

The first line of defence is that :mod:`trading_system.observability.attributes`
defines no name for any of these things. This module exists because the first
line depends on everybody using the vocabulary, and the second does not.

Imports nothing but the standard library, deliberately: it is reachable from
every instrumented package, several of which have boundary tests forbidding
sockets and brokers in their import graphs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "ALLOWED_EXACT_NAMES",
    "DEFAULT_FORBIDDEN_SUBSTRINGS",
    "PrivacyPolicy",
    "mask_account",
    "sanitize",
    "sanitize_value",
]

#: Substrings that disqualify an attribute name, whatever else it says. The
#: shipped configuration repeats these; this constant is the fallback when a
#: caller has no configuration to hand, so the guard is never simply absent.
DEFAULT_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "prompt",
    "completion",
    "response_text",
    "portfolio",
    "balance",
    "account_number",
)

#: Attribute names whose *value* is money. Dropped unless configuration
#: explicitly permits monetary attributes.
_MONETARY_HINTS: tuple[str, ...] = (
    "amount",
    "capital",
    "cash",
    "cost",
    "equity",
    "pnl",
    "premium",
    "price",
    "proceeds",
    "profit",
    "value",
)

#: An IBKR account looks like ``DU1234567`` / ``U1234567``. Matched on shape so
#: a value under an unexpected name is masked anyway.
_ACCOUNT_SHAPE = re.compile(r"\b(D[UF]|[UF])\d{5,10}\b")

#: Exact attribute names whose *name* collides with a forbidden substring but
#: whose *value* is a count or an identifier.
#:
#: The substring rules are deliberately blunt — that is what makes them hold
#: against names nobody anticipated — and bluntness produces exactly three
#: false positives in the shipped vocabulary:
#:
#: ``llm.input_tokens`` / ``llm.output_tokens``
#:     contain ``token``. They are integers describing what a model call cost,
#:     and they are the whole point of AI telemetry.
#: ``trading.pnl.id``
#:     contains ``pnl``, which is a monetary hint. It is an identifier — the
#:     one that leads an operator from a span to the immutable result record.
#:
#: An allow-list of *exact* names rather than a loosened pattern, so adding one
#: is a deliberate, reviewable act. A test asserts every name in
#: :mod:`trading_system.observability.attributes` survives the filter, which is
#: what keeps this list honest as the vocabulary grows.
ALLOWED_EXACT_NAMES: frozenset[str] = frozenset(
    {
        "llm.input_tokens",
        "llm.output_tokens",
        "trading.pnl.id",
    }
)


@dataclass(frozen=True, slots=True)
class PrivacyPolicy:
    """The redaction rules in force. Built from configuration, or defaulted."""

    forbidden_substrings: tuple[str, ...] = DEFAULT_FORBIDDEN_SUBSTRINGS
    mask_account_identifiers: bool = True
    max_attribute_length: int = 256
    allow_monetary_attributes: bool = False

    @classmethod
    def of(cls, config: Any) -> PrivacyPolicy:
        """Build from an ``ObservabilityPrivacyConfig``, tolerating ``None``.

        Tolerant on purpose. A telemetry configuration that failed to load must
        leave the *strictest* policy in force rather than none at all — the
        failure mode of the alternative is a deployment that emits everything
        because a YAML key was misspelled.
        """
        if config is None:
            return cls()
        return cls(
            forbidden_substrings=tuple(
                getattr(config, "forbidden_attribute_substrings", DEFAULT_FORBIDDEN_SUBSTRINGS)
            )
            or DEFAULT_FORBIDDEN_SUBSTRINGS,
            mask_account_identifiers=bool(getattr(config, "mask_account_identifiers", True)),
            max_attribute_length=int(getattr(config, "max_attribute_length", 256)),
            allow_monetary_attributes=bool(getattr(config, "allow_monetary_attributes", False)),
        )

    def forbids(self, name: str) -> bool:
        lowered = name.lower()
        if lowered in ALLOWED_EXACT_NAMES:
            return False
        return any(substring in lowered for substring in self.forbidden_substrings)

    def is_monetary(self, name: str) -> bool:
        lowered = name.lower()
        if lowered in ALLOWED_EXACT_NAMES:
            return False
        return any(hint in lowered for hint in _MONETARY_HINTS)


def mask_account(value: str, *, visible: int = 4) -> str:
    """Mask an account identifier, keeping the last few characters.

    The same shape Milestone 9 stores on every artifact: ``*****4567``. Enough
    to tell two accounts apart in an operational context, not enough to be one.
    """
    text = value.strip()
    if len(text) <= visible:
        return "*" * len(text)
    return "*" * (len(text) - visible) + text[-visible:]


def sanitize_value(value: Any, policy: PrivacyPolicy) -> Any:
    """One value, made safe to emit. Never raises."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    text = str(value)
    if policy.mask_account_identifiers:
        text = _ACCOUNT_SHAPE.sub(lambda match: mask_account(match.group(0)), text)
    if len(text) > policy.max_attribute_length:
        # Truncated rather than dropped: an operator usually wants the first
        # part of a long identifier, and the marker makes the truncation
        # visible rather than leaving a value that looks complete.
        return text[: policy.max_attribute_length - 1] + "…"
    return text


def sanitize(
    attributes: Mapping[str, Any] | None, policy: PrivacyPolicy | None = None
) -> dict[str, Any]:
    """Every attribute that may safely be emitted, with the rest dropped.

    Never raises, whatever it is handed — including an object whose ``str``
    itself raises. A privacy filter that could throw would let a redaction bug
    take down the operation it was supposed to be quietly observing, and this
    package's whole contract is that it cannot affect trading.
    """
    if not attributes:
        return {}
    resolved = policy or PrivacyPolicy()
    safe: dict[str, Any] = {}
    for name, value in attributes.items():
        try:
            if resolved.forbids(name):
                continue
            if value is None:
                continue
            if resolved.is_monetary(name) and not resolved.allow_monetary_attributes:
                continue
            safe[name] = sanitize_value(value, resolved)
        except Exception:  # pragma: no cover - a filter must never raise
            continue
    return safe


def would_emit(name: str, policy: PrivacyPolicy | None = None) -> bool:
    """Whether an attribute with this name would survive. For tests and audits."""
    resolved = policy or PrivacyPolicy()
    if resolved.forbids(name):
        return False
    return not (resolved.is_monetary(name) and not resolved.allow_monetary_attributes)
