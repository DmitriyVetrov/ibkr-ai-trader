"""The OpenTelemetry adapter. **The only module that imports the SDK.**

Everything else in this package — the attribute vocabulary, the privacy filter,
the provider protocol, the span helper, the metric guard — imports nothing but
the standard library. That is not fastidiousness: the research agent, the exit
engine, the risk engine and the strategy selector all have boundary tests that
walk their transitive import graph and fail on ``socket``, ``urllib``, ``http``
or ``requests``, and the OTLP exporter imports all of them. So the SDK is
confined here, and nothing in the trading packages names this module.

The import itself is **lazy and optional**, exactly as ``ib_async`` and
``anthropic`` are. Without the extra installed, :func:`build_provider` returns
``None`` and the system runs on :class:`~trading_system.observability.provider.NullTelemetry`
— unchanged, because that is what it runs on by default anyway.

Everything here is best effort:

* a failed exporter construction returns ``None`` rather than raising;
* a span operation that throws is swallowed by the wrapper in
  :mod:`trading_system.observability.tracing`;
* the exporter queue never blocks — ``block_on_full_queue`` fails to load, so a
  dead collector drops telemetry instead of applying back-pressure to a
  monitoring cycle.

Install with ``pip install -e '.[observability]'``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from trading_system.infrastructure.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.infrastructure.settings import ObservabilityConfig
    from trading_system.observability.provider import SpanHandle

__all__ = ["OpenTelemetryProvider", "build_provider", "sdk_available"]

_logger = get_logger(__name__)


def sdk_available() -> bool:
    """Whether the OpenTelemetry SDK is importable.

    Checked rather than assumed, so ``ops health`` can report
    ``SDK_UNAVAILABLE`` — a different fact from ``DISABLED`` and from
    ``EXPORT_FAILING``, and one an operator needs when telemetry is configured
    but nothing is arriving.
    """
    try:
        import opentelemetry.sdk.trace  # noqa: F401

        return True
    except Exception:
        return False


class _Span:
    """One OpenTelemetry span, wrapped so nothing it does can escape."""

    __slots__ = ("_context", "_span", "_token")

    def __init__(self, span: Any, context: Any = None, token: Any = None) -> None:
        self._span = span
        self._context = context
        self._token = token

    def set_attribute(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, value)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        self._span.set_attributes(dict(attributes))

    def record_error(self, error: BaseException) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._span.record_exception(error)
        self._span.set_status(Status(StatusCode.ERROR, type(error).__name__))

    def set_status_ok(self) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._span.set_status(Status(StatusCode.OK))

    def end(self) -> None:
        from opentelemetry import context as otel_context

        self._span.end()
        if self._token is not None:
            otel_context.detach(self._token)

    @property
    def trace_id(self) -> str | None:
        context = self._span.get_span_context()
        return format(context.trace_id, "032x") if context.trace_id else None

    @property
    def span_id(self) -> str | None:
        context = self._span.get_span_context()
        return format(context.span_id, "016x") if context.span_id else None


class OpenTelemetryProvider:
    """Emits spans and metrics over OTLP to a collector.

    To a **collector**, never to a backend. The application has no dependency
    on Tempo, Prometheus, Loki or Grafana; swapping any of them is a change to
    ``deploy/otel/collector.yaml``, not to this file.
    """

    def __init__(self, tracer: Any, meter: Any, providers: tuple[Any, ...]) -> None:
        self._tracer = tracer
        self._meter = meter
        self._providers = providers
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return True

    def start_span(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> SpanHandle:
        """Start a span and make it current, so children nest under it.

        Making it current is what produces the trace hierarchy the brief asks
        for — ``trading.workflow`` with ``research.run`` and ``llm.generate``
        beneath it — without any service having to pass a parent handle around.
        """
        from opentelemetry import context as otel_context
        from opentelemetry import trace

        span = self._tracer.start_span(name, attributes=dict(attributes or {}))
        token = otel_context.attach(trace.set_span_in_context(span))
        return _Span(span, token=token)

    def record_count(
        self, instrument: str, value: int = 1, *, labels: Mapping[str, str] | None = None
    ) -> None:
        counter = self._counters.get(instrument)
        if counter is None:
            counter = self._meter.create_counter(instrument)
            self._counters[instrument] = counter
        counter.add(value, dict(labels or {}))

    def record_duration(
        self, instrument: str, seconds: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        histogram = self._histograms.get(instrument)
        if histogram is None:
            histogram = self._meter.create_histogram(instrument, unit="s")
            self._histograms[instrument] = histogram
        histogram.record(seconds, dict(labels or {}))

    def current_trace_context(self) -> tuple[str | None, str | None]:
        """``(trace_id, span_id)`` of the active span, for log correlation."""
        from opentelemetry import trace

        span = trace.get_current_span()
        context = span.get_span_context()
        if not context.is_valid:
            return None, None
        return format(context.trace_id, "032x"), format(context.span_id, "016x")

    def shutdown(self) -> None:
        """Flush and stop. Bounded, and never raises.

        Called from the CLI at exit and from the scheduler on a clean stop. A
        flush that hangs on an unreachable collector would turn "telemetry is
        down" into "the process will not exit", which is precisely the class of
        failure this package promises not to cause.
        """
        for provider in self._providers:
            try:
                provider.shutdown()
            except Exception:  # pragma: no cover - shutdown must never raise
                continue

    def __repr__(self) -> str:
        return "OpenTelemetryProvider()"


def build_provider(
    config: ObservabilityConfig, *, service_version: str
) -> OpenTelemetryProvider | None:
    """Construct a real provider, or ``None`` if one cannot be built.

    ``None`` for every reason: the SDK is absent, an exporter refused to
    construct, an endpoint is malformed. The caller installs
    :class:`~trading_system.observability.provider.NullTelemetry` and the
    system runs exactly as it does with telemetry switched off — which is the
    shipped default, so that path is the well-tested one.
    """
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except Exception as exc:
        _logger.info(
            "observability.sdk_unavailable",
            error=str(exc),
            detail=(
                "the OpenTelemetry SDK is not installed. Telemetry is off and the trading "
                "system runs unchanged; install with pip install -e '.[observability]'"
            ),
        )
        return None

    try:
        resource = Resource.create(
            {
                "service.name": config.service_name,
                "service.version": config.service_version or service_version,
                "deployment.environment": config.environment,
            }
        )
        span_exporter, metric_exporter = _exporters(config)
        tracer_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(root=TraceIdRatioBased(config.sampling.ratio)),
        )
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                span_exporter,
                max_queue_size=config.exporter.max_queue_size,
                max_export_batch_size=config.exporter.max_export_batch_size,
                schedule_delay_millis=int(config.exporter.export_interval_seconds * 1000),
            )
        )

        providers: list[Any] = [tracer_provider]
        meter_provider = None
        if config.metrics.enabled and metric_exporter is not None:
            meter_provider = MeterProvider(
                resource=resource,
                metric_readers=[
                    PeriodicExportingMetricReader(
                        metric_exporter,
                        export_interval_millis=int(config.metrics.export_interval_seconds * 1000),
                    )
                ],
            )
            providers.append(meter_provider)

        trace.set_tracer_provider(tracer_provider)
        if meter_provider is not None:
            metrics.set_meter_provider(meter_provider)

        return OpenTelemetryProvider(
            tracer=tracer_provider.get_tracer("trading_system"),
            meter=(
                meter_provider.get_meter("trading_system")
                if meter_provider is not None
                else metrics.get_meter("trading_system")
            ),
            providers=tuple(providers),
        )
    except Exception as exc:
        _logger.warning(
            "observability.provider_unavailable",
            error=str(exc),
            detail=(
                "telemetry could not be initialised. The trading system continues with "
                "telemetry disabled; no trading behaviour changes"
            ),
        )
        return None


def _exporters(config: ObservabilityConfig) -> tuple[Any, Any]:
    """The span and metric exporters, OTLP or console.

    ``console`` is for local development and for seeing what *would* be
    exported without standing up a collector. It is not a fallback: a
    misconfigured OTLP endpoint does not quietly start printing spans to
    stderr, because that would look like telemetry working.
    """
    if config.exporter.console:
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter(), ConsoleMetricExporter()

    endpoint = config.exporter.endpoint.rstrip("/")
    if config.exporter.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return (
            OTLPSpanExporter(endpoint=endpoint, timeout=int(config.exporter.timeout_seconds)),
            OTLPMetricExporter(endpoint=endpoint, timeout=int(config.exporter.timeout_seconds)),
        )

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return (
        OTLPSpanExporter(
            endpoint=f"{endpoint}/v1/traces", timeout=int(config.exporter.timeout_seconds)
        ),
        OTLPMetricExporter(
            endpoint=f"{endpoint}/v1/metrics", timeout=int(config.exporter.timeout_seconds)
        ),
    )
