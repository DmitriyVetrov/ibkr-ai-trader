"""Business-operation spans, safe to call from anywhere (Milestone 11).

The one function the trading packages call:

.. code-block:: python

    with operation("exit.evaluate", attributes={TRADING_POSITION_ID: position_id}):
        ...

Four properties, and every one of them is what makes it acceptable to put this
call inside a trading service:

* **It is a no-op when telemetry is off**, which is the shipped default. No
  allocation, no attribute filtering, no cost beyond a module-global lookup.
* **It never raises.** Every telemetry operation is wrapped. If the provider
  throws, the exporter's queue is full, the collector is unreachable or the SDK
  is half-installed, the ``with`` block runs exactly as it would have.
* **It never swallows the caller's exception.** An error inside the block is
  recorded on the span and then re-raised, unchanged. Telemetry observes; it
  does not handle.
* **It imports no vendor.** This module reaches
  :mod:`trading_system.observability.provider` and
  :mod:`~trading_system.observability.privacy`, both of which import nothing
  but the standard library. That is what lets the exit engine, the risk engine
  and the research agent call it without breaking the boundary tests that
  forbid sockets in their import graphs.

The provider is a module global set once at process start by
:func:`trading_system.observability.runtime.configure_telemetry`. A global
rather than an injected dependency, deliberately: threading a telemetry handle
through twelve service constructors would put an observability concern into
every trading signature, and the moment a service *has* a telemetry object, the
temptation exists to make a decision from it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from trading_system.observability.privacy import PrivacyPolicy, sanitize
from trading_system.observability.provider import (
    NULL_SPAN,
    NullTelemetry,
    SpanHandle,
    TelemetryProvider,
)

__all__ = [
    "current_trace_context",
    "emit_log",
    "get_provider",
    "operation",
    "reset_provider",
    "set_privacy_policy",
    "set_provider",
    "telemetry_enabled",
]

#: The active provider. ``NullTelemetry`` until something sets otherwise, which
#: is what makes every trading package safe to instrument before the
#: observability stack exists — and safe to run for ever without one.
_provider: TelemetryProvider = NullTelemetry()

#: The redaction rules applied to every attribute. Strict by default, so a
#: telemetry configuration that failed to load leaves the *strictest* policy in
#: force rather than none.
_privacy: PrivacyPolicy = PrivacyPolicy()


def set_provider(provider: TelemetryProvider) -> None:
    """Install the active provider. Called once, at process start."""
    global _provider
    _provider = provider


def reset_provider() -> None:
    """Return to doing nothing. Used by tests, and by shutdown."""
    global _provider
    _provider = NullTelemetry()


def set_privacy_policy(policy: PrivacyPolicy) -> None:
    """Install the redaction rules. Called alongside :func:`set_provider`."""
    global _privacy
    _privacy = policy


def get_provider() -> TelemetryProvider:
    return _provider


def telemetry_enabled() -> bool:
    """Whether anything is actually being recorded.

    Read by nothing in the trading path. It exists for the health report and
    for tests — a trading decision that branched on this would be a trading
    decision that depends on telemetry.
    """
    try:
        return bool(_provider.enabled)
    except Exception:  # pragma: no cover - a provider must never matter
        return False


@contextmanager
def operation(name: str, *, attributes: Mapping[str, Any] | None = None) -> Iterator[SpanHandle]:
    """One business operation, as a span. Never changes what the caller does.

    ``name`` should come from
    :data:`trading_system.observability.attributes.OPERATION_NAMES`; a test
    asserts every name used in the source is in that list, so a typo cannot
    silently create a second operation nobody is querying.

    An exception inside the block is recorded on the span and **re-raised**.
    Recording an error is observation; handling it would be a decision, and
    this package makes none.
    """
    provider = _provider
    if not _is_enabled(provider):
        yield NULL_SPAN
        return

    span = _start(provider, name, attributes)
    try:
        yield span
    except BaseException as exc:
        _record_error(span, exc)
        _safely(span.end)
        raise
    else:
        _safely(span.set_status_ok)
        _safely(span.end)


def annotate(span: SpanHandle, attributes: Mapping[str, Any]) -> None:
    """Add attributes to a span that is already open. Never raises.

    Used where a value is only known part-way through an operation — the
    execution id after the record is minted, the reason code after the engine
    has decided.
    """
    safe = sanitize(attributes, _privacy)
    if safe:
        _safely(lambda: span.set_attributes(safe))


def current_trace_context() -> tuple[str | None, str | None]:
    """``(trace_id, span_id)`` of the active span, or ``(None, None)``.

    What the structured-logging processor injects into every record emitted
    inside a span, and therefore what makes trace-to-log navigation work in
    Grafana. Returns ``(None, None)`` rather than raising under every failure,
    because a logging call must not be able to fail on account of telemetry.
    """
    try:
        return _provider.current_trace_context()
    except Exception:  # pragma: no cover - a provider must never matter
        return None, None


def record_count(
    instrument: str, value: int = 1, *, labels: Mapping[str, str] | None = None
) -> None:
    """Increment a counter. Never raises; a no-op when telemetry is off."""
    provider = _provider
    if not _is_enabled(provider):
        return
    _safely(lambda: provider.record_count(instrument, value, labels=labels))


def record_duration(
    instrument: str, seconds: float, *, labels: Mapping[str, str] | None = None
) -> None:
    """Record a duration in seconds. Never raises; a no-op when telemetry is off."""
    provider = _provider
    if not _is_enabled(provider):
        return
    _safely(lambda: provider.record_duration(instrument, seconds, labels=labels))


def emit_log(level: str, message: str, *, attributes: Mapping[str, Any] | None = None) -> None:
    """Offer one log line to the telemetry side channel. Never raises.

    Reached through ``getattr`` because ``emit_log`` is an *optional* provider
    capability: a provider that predates it simply does not export logs, which
    is what every provider did before Milestone 12 added the OTLP log path.

    Attributes go through the same privacy filter spans do. A log line is not
    an audit archive either — the immutable domain artifact is, and this
    carries its id.
    """
    provider = _provider
    if not _is_enabled(provider):
        return
    sink = getattr(provider, "emit_log", None)
    if sink is None:
        return
    _safely(lambda: sink(level, message, attributes=sanitize(attributes, _privacy)))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _is_enabled(provider: TelemetryProvider) -> bool:
    try:
        return bool(provider.enabled)
    except Exception:  # pragma: no cover - a provider must never matter
        return False


def _start(
    provider: TelemetryProvider, name: str, attributes: Mapping[str, Any] | None
) -> SpanHandle:
    """Open a span, or hand back the null one if anything goes wrong."""
    try:
        return provider.start_span(name, attributes=sanitize(attributes, _privacy))
    except Exception:  # pragma: no cover - a provider must never matter
        return NULL_SPAN


def _record_error(span: SpanHandle, error: BaseException) -> None:
    """Note an error on a span, swallowing anything the provider does about it.

    A named function rather than a lambda inside the ``except`` clause: Python
    unbinds the exception name when the clause ends, and a closure over it is a
    latent ``NameError`` waiting for the day somebody makes this deferred.
    """
    try:
        span.record_error(error)
    except Exception:  # pragma: no cover - telemetry is never load-bearing
        return


def _safely(action: Any) -> None:
    """Run a telemetry action, swallowing anything it raises.

    The single most important five lines in this package. Everything else is a
    convenience; this is the guarantee that an unreachable collector cannot
    approve a trade, reject one, release capital or stop an exit from being
    evaluated.
    """
    try:
        action()
    except Exception:  # pragma: no cover - telemetry is never load-bearing
        return
