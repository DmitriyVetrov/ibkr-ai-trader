"""Trace correlation for structured logs (Milestone 11).

One ``structlog`` processor. It adds ``trace_id`` and ``span_id`` to every
record emitted inside an active span, plus ``service`` and ``trading_mode``, so
that a Grafana operator can go:

.. code-block:: text

    open a trace in Tempo
        -> trace_id
        -> Loki query {trace_id="..."} shows every log line from that operation

and the reverse: a log line carries the trace id that leads back to the span.

Three properties:

* **It never raises.** A logging call must not be able to fail because
  telemetry is misbehaving. Every lookup is wrapped, and a failure leaves the
  record exactly as it was.
* **It adds nothing when there is no span**, which is the case for every log
  line when telemetry is disabled. No key, no ``None`` value, no noise — a
  ``trace_id: null`` on every line of a system that has telemetry switched off
  is a field people learn to ignore.
* **It knows nothing about Grafana or Loki.** The domain emits structured logs
  with a trace id in them; correlating those is the collector's and the
  dashboard's job. That is the whole reason the domain can stay ignorant of the
  backends.

Registered by :func:`install_correlation`, which is called from the CLI after
:func:`~trading_system.observability.runtime.configure_telemetry`. Logging works
identically without it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["install_correlation", "trace_context_processor"]

#: Bound onto every record once :func:`install_correlation` has run. Kept as
#: module state rather than closed over, so re-installing (a test, a reload)
#: replaces rather than stacks.
_service_context: dict[str, str] = {}


def trace_context_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add trace correlation and service context to one log record.

    Never raises. A ``structlog`` processor that threw would turn every logging
    statement in the system into a crash — which is exactly the failure
    ``infrastructure/logging.py`` already documents from
    ``stdlib.add_logger_name``, and worth not repeating.
    """
    try:
        for key, value in _service_context.items():
            event_dict.setdefault(key, value)

        from trading_system.observability.tracing import current_trace_context

        trace_id, span_id = current_trace_context()
        if trace_id:
            event_dict["trace_id"] = trace_id
        if span_id:
            event_dict["span_id"] = span_id
    except Exception:  # pragma: no cover - a log call must never fail
        return event_dict
    return event_dict


def install_correlation(
    *,
    service_name: str = "trading-system",
    trading_mode: str = "PAPER",
    include_service_context: bool = True,
) -> None:
    """Register the processor with ``structlog``. Idempotent, and never raises.

    Inserted *before* the renderer so the correlation keys appear in both the
    console and the JSON output. Called once, after telemetry is configured;
    calling it twice replaces the service context rather than adding a second
    processor.
    """
    global _service_context
    _service_context = (
        {"service": service_name, "trading_mode": trading_mode} if include_service_context else {}
    )

    try:
        import structlog

        configuration = structlog.get_config()
        processors = list(configuration.get("processors", []))
        processors = [
            processor
            for processor in processors
            if getattr(processor, "__name__", "") != "trace_context_processor"
        ]
        if not processors:
            # Logging has not been configured yet. The next configure_logging()
            # call installs the standard chain, and the caller re-installs this.
            return
        processors.insert(max(len(processors) - 1, 0), trace_context_processor)
        structlog.configure(
            processors=processors,
            wrapper_class=configuration.get("wrapper_class"),
            logger_factory=configuration.get("logger_factory"),
            cache_logger_on_first_use=False,
        )
    except Exception:  # pragma: no cover - correlation is never load-bearing
        return


def service_context() -> dict[str, str]:
    """What is currently bound onto every record. For tests and diagnostics."""
    return dict(_service_context)


def reset_correlation() -> None:
    """Forget the service context. Used by tests between cases."""
    global _service_context
    _service_context = {}
