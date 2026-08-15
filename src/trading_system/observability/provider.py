"""The telemetry interface, and the null implementation (Milestone 11).

The seam between the trading system and OpenTelemetry. Everything in the
trading packages talks to :class:`TelemetryProvider`; only
:mod:`trading_system.observability.otel` knows the SDK exists, and nothing
imports that except :mod:`trading_system.observability.runtime`.

That layout is not tidiness — it is what keeps the boundary tests passing. The
research agent, the exit engine, the risk engine and the strategy selector all
have tests that walk their transitive import graph and fail on ``socket``,
``urllib``, ``http`` or ``requests``. The OTLP exporter imports every one of
those. So the *interface* has to be importable without the SDK, and the SDK has
to be reachable only from a module the instrumented code never names.

Two implementations ship:

:class:`NullTelemetry`
    Does nothing, allocates nothing, and is the default. This is what runs when
    ``observability.enabled`` is false, when the SDK is not installed, and when
    the configuration failed to load — three different situations that must all
    produce the same trading behaviour.
:class:`RecordingTelemetry`
    Keeps spans and measurements in memory. For tests: it is how
    ``tests/observability/`` asserts that a span carried the right domain ids
    without standing up a collector, and how the privacy tests prove that no
    forbidden attribute reaches an exporter.

The real one lives in :mod:`trading_system.observability.otel`.

**Nothing here may raise into a caller.** Every method is best effort. A
provider that could throw would let a telemetry fault change what the trading
system does, which is the one property this package exists to guarantee.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "LogRecord",
    "NullTelemetry",
    "RecordingSpan",
    "RecordingTelemetry",
    "SpanHandle",
    "TelemetryProvider",
]


@runtime_checkable
class SpanHandle(Protocol):
    """A span in progress. Every method is best effort and never raises."""

    def set_attribute(self, key: str, value: Any) -> None: ...

    def set_attributes(self, attributes: Mapping[str, Any]) -> None: ...

    def record_error(self, error: BaseException) -> None: ...

    def set_status_ok(self) -> None: ...

    def end(self) -> None: ...

    @property
    def trace_id(self) -> str | None:
        """Hex trace id, when there is a recording span. ``None`` otherwise."""

    @property
    def span_id(self) -> str | None: ...


@runtime_checkable
class TelemetryProvider(Protocol):
    """Somewhere spans and measurements can go.

    Deliberately narrow. A wider interface would tempt callers to reach for
    telemetry features inside business logic, and the moment a trading decision
    can *read* anything from telemetry, telemetry has stopped being a side
    channel.
    """

    @property
    def enabled(self) -> bool: ...

    def start_span(
        self, name: str, *, attributes: Mapping[str, Any] | None = None
    ) -> SpanHandle: ...

    def record_count(
        self, instrument: str, value: int = 1, *, labels: Mapping[str, str] | None = None
    ) -> None: ...

    def record_duration(
        self, instrument: str, seconds: float, *, labels: Mapping[str, str] | None = None
    ) -> None: ...

    def current_trace_context(self) -> tuple[str | None, str | None]:
        """``(trace_id, span_id)`` of the active span, or ``(None, None)``."""

    def shutdown(self) -> None: ...


#: ``emit_log`` is an **optional** provider capability, reached through
#: ``getattr`` in :func:`trading_system.observability.tracing.emit_log` rather
#: than declared on the protocol above.
#:
#: Optional rather than required because adding a method to a protocol every
#: existing implementation would have to grow is a change to a completed
#: milestone's contract, and Milestone 12 exists to *find* gaps in Milestone 11
#: rather than to redesign it. A provider that does not implement it simply
#: emits no logs over OTLP, which is what every provider did before.
OPTIONAL_PROVIDER_CAPABILITIES: tuple[str, ...] = ("emit_log",)


# ---------------------------------------------------------------------------
# The default: nothing at all
# ---------------------------------------------------------------------------
class _NullSpan:
    """A span that records nothing. Allocated once and shared."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        return None

    def record_error(self, error: BaseException) -> None:
        return None

    def set_status_ok(self) -> None:
        return None

    def end(self) -> None:
        return None

    @property
    def trace_id(self) -> str | None:
        return None

    @property
    def span_id(self) -> str | None:
        return None


#: One shared instance. Telemetry-off must cost nothing per operation, and a
#: monitoring cycle that allocated an object per span for the privilege of
#: discarding it would be paying for a feature it switched off.
NULL_SPAN = _NullSpan()


class NullTelemetry:
    """The default provider: does nothing, and does it identically every time.

    What runs when telemetry is disabled, when the OpenTelemetry SDK is not
    installed, and when the observability configuration failed to load. All
    three produce the same trading behaviour, which is the property
    ``tests/observability/test_failure_isolation.py`` asserts by running the
    same operations under each and comparing the stored artifacts.
    """

    @property
    def enabled(self) -> bool:
        return False

    def start_span(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> SpanHandle:
        return NULL_SPAN

    def record_count(
        self, instrument: str, value: int = 1, *, labels: Mapping[str, str] | None = None
    ) -> None:
        return None

    def record_duration(
        self, instrument: str, seconds: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        return None

    def current_trace_context(self) -> tuple[str | None, str | None]:
        return None, None

    def emit_log(
        self, level: str, message: str, *, attributes: Mapping[str, Any] | None = None
    ) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def __repr__(self) -> str:
        return "NullTelemetry()"


# ---------------------------------------------------------------------------
# The test double
# ---------------------------------------------------------------------------
@dataclass
class RecordingSpan:
    """A span kept in memory, with everything that was put on it."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    ended: bool = False
    ok: bool = False
    started_at: datetime | None = None
    _trace_id: str | None = None
    _span_id: str | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        self.attributes.update(attributes)

    def record_error(self, error: BaseException) -> None:
        self.errors.append(type(error).__name__)

    def set_status_ok(self) -> None:
        self.ok = True

    def end(self) -> None:
        self.ended = True

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    @property
    def span_id(self) -> str | None:
        return self._span_id


@dataclass
class Measurement:
    """One recorded metric point, with the labels it carried."""

    instrument: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class LogRecord:
    """One log line offered to the telemetry side channel.

    Recorded so a test can assert that a line carrying a trace id was actually
    emitted, without standing up a collector — the same reason
    :class:`RecordingSpan` exists.
    """

    level: str
    message: str
    attributes: dict[str, Any] = field(default_factory=dict)


class RecordingTelemetry:
    """Keeps everything in memory. For tests, never for a deployment.

    Exists so the assertions this milestone needs can be made without a
    collector: that a span carried ``trading.execution.id``, that no metric
    carried it as a *label*, that a forbidden attribute never reached an
    exporter, and that turning telemetry on changed no stored artifact.
    """

    def __init__(self, *, trace_id: str = "0" * 32, span_id: str = "0" * 16) -> None:
        self.spans: list[RecordingSpan] = []
        self.counts: list[Measurement] = []
        self.durations: list[Measurement] = []
        self.logs: list[LogRecord] = []
        self.shutdown_calls = 0
        self._trace_id = trace_id
        self._span_id = span_id
        self._stack: list[RecordingSpan] = []

    @property
    def enabled(self) -> bool:
        return True

    def start_span(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> SpanHandle:
        span = RecordingSpan(
            name=name,
            attributes=dict(attributes or {}),
            _trace_id=self._trace_id,
            _span_id=self._span_id,
        )
        self.spans.append(span)
        self._stack.append(span)
        return span

    def record_count(
        self, instrument: str, value: int = 1, *, labels: Mapping[str, str] | None = None
    ) -> None:
        self.counts.append(
            Measurement(instrument=instrument, value=value, labels=dict(labels or {}))
        )

    def record_duration(
        self, instrument: str, seconds: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        self.durations.append(
            Measurement(instrument=instrument, value=seconds, labels=dict(labels or {}))
        )

    def current_trace_context(self) -> tuple[str | None, str | None]:
        if not self._stack:
            return None, None
        return self._trace_id, self._span_id

    def emit_log(
        self, level: str, message: str, *, attributes: Mapping[str, Any] | None = None
    ) -> None:
        self.logs.append(LogRecord(level=level, message=message, attributes=dict(attributes or {})))

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    # --- test conveniences -------------------------------------------------
    def span_named(self, name: str) -> RecordingSpan | None:
        return next((span for span in self.spans if span.name == name), None)

    def spans_named(self, name: str) -> list[RecordingSpan]:
        return [span for span in self.spans if span.name == name]

    def all_attribute_names(self) -> set[str]:
        return {name for span in self.spans for name in span.attributes}

    def all_label_names(self) -> set[str]:
        return {
            name for measurement in (*self.counts, *self.durations) for name in measurement.labels
        }

    def counts_for(self, instrument: str) -> list[Measurement]:
        return [point for point in self.counts if point.instrument == instrument]

    def __repr__(self) -> str:
        return (
            f"RecordingTelemetry(spans={len(self.spans)}, counts={len(self.counts)}, "
            f"durations={len(self.durations)})"
        )
