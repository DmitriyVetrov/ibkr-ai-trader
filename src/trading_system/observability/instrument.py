"""One decorator, so instrumenting a service is a single added line.

The alternative — wrapping each method body in a ``with`` block — means
re-indenting tested trading code to add an observability concern, and a diff
that touches every line of a risk or exit method is a diff nobody can review
for what actually changed. This keeps the change to one line above the
signature:

.. code-block:: python

    @traced("exit.evaluate", duration=metrics.EXIT_EVALUATION_DURATION)
    def evaluate(self, position, ...): ...

Four properties, and they are the same four the whole package rests on:

* **A no-op when telemetry is off**, which is the shipped default. The wrapper
  checks one module global and calls straight through — no span object, no
  attribute filtering, no timer.
* **It never raises.** Every telemetry call inside is wrapped. A provider that
  throws, an exporter with a full queue, a half-installed SDK: the decorated
  method runs and returns exactly as it would have.
* **It never changes the return value, and never swallows an exception.** An
  error is recorded on the span, the failure counter is incremented, and the
  exception is re-raised unchanged. Telemetry observes; it does not handle.
* **It carries domain ids without reading them.** ``attributes`` is a callable
  over the same arguments the method received, so an id reaches the span
  without the method itself knowing telemetry exists.

Metric labels go through the cardinality guard in
:mod:`trading_system.observability.metrics`, so an id passed as a label is
dropped rather than becoming one time series per trade.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from trading_system.observability import metrics as _metrics
from trading_system.observability import tracing

__all__ = ["traced"]

P = ParamSpec("P")
R = TypeVar("R")

#: Extracts span attributes from the call. Given the bound arguments, returns a
#: mapping of attribute name to value; anything it raises is ignored.
AttributeSource = Callable[..., Mapping[str, Any]]

#: Extracts span attributes from the *result*, once there is one. This is where
#: a newly minted execution id or a decided reason code comes from.
ResultSource = Callable[[Any], Mapping[str, Any]]


def traced(
    name: str,
    *,
    attributes: AttributeSource | None = None,
    result_attributes: ResultSource | None = None,
    duration: str | None = None,
    count: str | None = None,
    failure_count: str | None = None,
    labels: Callable[[Any], Mapping[str, str]] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap one business operation in a span, a duration and a counter.

    ``name`` must be in
    :data:`trading_system.observability.attributes.OPERATION_NAMES`; a test
    asserts it, so a typo cannot quietly create a second operation that no
    dashboard queries and no alert rule matches.

    ``duration``, ``count`` and ``failure_count`` name instruments from
    :mod:`trading_system.observability.metrics`. ``labels`` derives *low
    cardinality* labels from the result — status, strategy, mode. Anything
    high-cardinality it returns is dropped by the guard rather than emitted.
    """

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if not tracing.telemetry_enabled():
                # The common case, and the shipped default. One global read,
                # then straight through: an operation must not pay for a
                # feature that is switched off.
                return function(*args, **kwargs)

            span_attributes = _safe_attributes(attributes, args, kwargs)
            started = time.perf_counter()
            with tracing.operation(name, attributes=span_attributes) as span:
                try:
                    result = function(*args, **kwargs)
                except BaseException:
                    _record(failure_count, {})
                    raise
                elapsed = time.perf_counter() - started
                metric_labels = _safe_labels(labels, result)
                if result_attributes is not None:
                    tracing.annotate(span, _safe_result_attributes(result_attributes, result))
                if duration is not None:
                    _metrics.record_duration(duration, elapsed, labels=metric_labels)
                _record(count, metric_labels)
                return result

        return wrapper

    return decorate


def _record(instrument: str | None, labels: Mapping[str, str]) -> None:
    if instrument is not None:
        _metrics.record_count(instrument, 1, labels=labels)


def _safe_attributes(
    source: AttributeSource | None, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Attributes from the call, or none. An extractor that raises is ignored."""
    if source is None:
        return {}
    try:
        return dict(source(*args, **kwargs))
    except Exception:  # pragma: no cover - telemetry is never load-bearing
        return {}


def _safe_result_attributes(source: ResultSource, result: Any) -> dict[str, Any]:
    try:
        return dict(source(result))
    except Exception:  # pragma: no cover - telemetry is never load-bearing
        return {}


def _safe_labels(source: Callable[[Any], Mapping[str, str]] | None, result: Any) -> dict[str, str]:
    if source is None:
        return {}
    try:
        return dict(source(result))
    except Exception:  # pragma: no cover - telemetry is never load-bearing
        return {}
