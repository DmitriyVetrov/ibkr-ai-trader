"""Emit real telemetry for the observability acceptance gate (Milestone 12).

Brief sections 11 and 12 ask for a *real* application trace, a real metric and
a real structured log — then for each to be found in its backend. This module
produces them, and it produces them the way the application does: through
:mod:`trading_system.observability.tracing`, against a provider built by
:mod:`trading_system.observability.runtime`.

That is the whole point. A bespoke OTLP client written for the gate would prove
that *a* program can reach the collector, which is not the claim being made.
The claim is that **this application's** telemetry path works end to end, so
the gate has to walk that path.

.. code-block:: text

    configure_telemetry(observability, enabled + otlp)
          |
    tracing.operation("readiness.acceptance")     -> a span, with a trace id
        record_count / record_duration            -> a metric
        logger.info(...)                          -> a structured log, carrying
                                                     the same trace id
          |
    flush (shutdown), so nothing is left in a batch queue when we go looking

Telemetry is **restored** afterwards. Turning it on to run a gate and leaving
it on would change what the process does after the gate finished, which is
precisely the kind of side effect a readiness check must not have.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig

__all__ = ["AcceptanceEmission", "emit_acceptance_signals"]

_logger = get_logger(__name__)

#: The metric the gate emits and then looks for in the metrics backend.
#:
#: A counter named for what it is, carrying only low-cardinality labels —
#: nothing here may become one time series per run, which is the failure the
#: cardinality guard exists to prevent and which this gate would otherwise be
#: the first to cause.
ACCEPTANCE_COUNTER = "trading_readiness_acceptance_total"

#: How the metric appears once the collector has exported it and Prometheus has
#: scraped it. The OTLP-to-Prometheus translation appends ``_total`` to a
#: counter, and the collector's ``resource_to_telemetry_conversion`` turns
#: resource attributes into labels — neither is something the application
#: controls, so the query name is stated here rather than derived.
ACCEPTANCE_METRIC_QUERY = "trading_readiness_acceptance_total"


@dataclass(frozen=True, slots=True)
class AcceptanceEmission:
    """What the gate emitted, so each backend can be asked for it."""

    #: The trace id to look up in the tracing backend, and to grep for in the
    #: log backend. ``None`` when no telemetry provider could be started, which
    #: is itself the finding.
    trace_id: str | None
    metric: str
    emitted_at: datetime
    #: Whether a provider was actually installed and exporting.
    active: bool = False
    #: The collector's metric exposition, when it was fetched, so the
    #: cardinality check can run against live output rather than against
    #: configuration alone.
    exposition: str | None = None
    error: str | None = None


def emit_acceptance_signals(*, settings: Settings, config: SystemConfig) -> AcceptanceEmission:
    """Emit one span, one metric and one correlated log line. Never raises.

    Forces telemetry **on** for the duration regardless of
    ``observability.enabled``, because the gate's question is "does the
    pipeline work", not "is the pipeline switched on for ordinary operation".
    Those are different questions and a deployment can legitimately answer no
    to the second while needing yes to the first.

    Restores the previous provider before returning, whatever happens.
    """
    from trading_system.observability import logging as correlation
    from trading_system.observability import tracing
    from trading_system.observability.runtime import configure_telemetry, shutdown_telemetry

    emitted_at = datetime.now(UTC)
    previous = tracing.get_provider()
    previous_export = correlation.otlp_export_enabled()

    # Telemetry on, logs exported, console off. Overriding the *committed*
    # policy for the duration of the gate rather than editing it: a
    # configuration file flipped to make an acceptance run pass would then be
    # the configuration the deployment runs under.
    acceptance = settings.resolved_observability(config.observability).model_copy(
        update={
            "enabled": True,
            "logging": config.observability.logging.model_copy(
                update={"export_otlp": True, "correlate_traces": True}
            ),
            "exporter": config.observability.exporter.model_copy(update={"console": False}),
        }
    )

    trace_id: str | None = None
    error: str | None = None
    active = False
    try:
        provider = configure_telemetry(config=acceptance, service_version="readiness-gate")
        active = bool(getattr(provider, "enabled", False))
        correlation.install_correlation(
            service_name=acceptance.service_name,
            trading_mode=settings.trading_mode.value,
            include_service_context=acceptance.logging.include_service_context,
            export_otlp=True,
        )

        with tracing.operation(
            "readiness.acceptance",
            attributes={
                # Deliberately no domain identifier and no monetary value. This
                # span exists to prove a pipeline works, and a gate that leaked
                # an execution id into telemetry would fail the criterion it was
                # emitted to satisfy.
                "readiness.gate": "observability",
                "trading.mode": settings.trading_mode.value,
            },
        ) as span:
            trace_id = span.trace_id
            if trace_id is None:
                trace_id, _ = tracing.current_trace_context()

            tracing.record_count(ACCEPTANCE_COUNTER, 1, labels={"gate": "observability"})
            tracing.record_duration(
                "trading_readiness_acceptance_duration", 0.001, labels={"gate": "observability"}
            )

            # A structured log emitted *inside* the span, so the correlation
            # probe can find one trace id in both backends. This is the line
            # the log backend is asked for by trace id.
            _logger.info(
                "readiness.acceptance.emitted",
                gate="observability",
                trace_id=trace_id,
                detail=(
                    "a real span, metric and log emitted through the ordinary application "
                    "telemetry path for the Milestone 12 observability acceptance gate"
                ),
            )
    except Exception as exc:  # pragma: no cover - telemetry must never raise
        error = str(exc)
    finally:
        # Flush before looking. A batch processor holds records for its
        # schedule delay, and a probe that queried a backend before the
        # exporter had sent anything would report a working pipeline broken.
        with suppress(Exception):  # a shutdown that raised would mask the result
            shutdown_telemetry()
        tracing.set_provider(previous)
        correlation.install_correlation(
            service_name=config.observability.service_name,
            trading_mode=settings.trading_mode.value,
            include_service_context=config.observability.logging.include_service_context,
            export_otlp=previous_export,
        )

    exposition = _collector_exposition(config)
    return AcceptanceEmission(
        trace_id=trace_id,
        metric=ACCEPTANCE_METRIC_QUERY,
        emitted_at=emitted_at,
        active=active,
        exposition=exposition,
        error=error,
    )


def _collector_exposition(config: SystemConfig) -> str | None:
    """The collector's exported application metrics, for the cardinality check.

    Fetched from the collector's Prometheus exporter rather than from
    Prometheus itself, because this is the point at which the *application's*
    labels are visible before any backend has relabelled anything. A forbidden
    label that Prometheus dropped is still a forbidden label the application
    emitted.
    """
    from trading_system.readiness.observability_probe import http_get

    acceptance = config.readiness.observability_acceptance
    result = http_get(acceptance.collector_exporter_url, timeout=acceptance.probe_timeout_seconds)
    return result.body if result.ok else None
