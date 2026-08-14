"""Telemetry: an operational side channel that can never change a decision.

.. code-block:: text

    trading operation
          |
    operation("exit.evaluate", {trading.position.id: ...})    <- no vendor here
          |
    TelemetryProvider          NullTelemetry by default
          |
    OpenTelemetry SDK          the ONLY place the SDK is imported
          |
    OTLP  ->  Collector  ->  Tempo / Prometheus / Loki  ->  Grafana

**The one rule.** If the collector, Tempo, Prometheus, Loki and Grafana are all
down, the trading system behaves *identically*. Telemetry cannot approve a
trade, reject one, change a quantity, release capital, submit an order or stop
an exit from being evaluated. Every call in this package is wrapped so that a
provider which throws is indistinguishable from one that is switched off, and
``tests/observability/test_failure_isolation.py`` asserts it by running the same
operations against a broken exporter and comparing the stored artifacts.

**The layering, and why it is shaped this way.** ``attributes``, ``privacy``,
``provider``, ``tracing``, ``metrics`` and ``logging`` import nothing but the
standard library. Only ``otel`` imports the SDK, and only ``runtime`` imports
``otel``. That is not tidiness: the research agent, the exit engine, the risk
engine and the strategy selector all have boundary tests that walk their
transitive import graph and fail on ``socket``, ``urllib``, ``http`` or
``requests`` — and the OTLP exporter imports every one of them. Confining the
SDK is what lets those packages be instrumented at all.

**What never leaves.** No account number, no credential, no balance, no
portfolio, no market-data payload, no prompt, no model response. A span carries
the *id* of an immutable domain artifact; the artifact stays where it can be
audited. ``privacy.py`` enforces this, and ``tests/observability/test_privacy.py``
proves it against real spans rather than by inspection.

Everything that touches the SDK is deferred through ``__getattr__``, for the
same reason every other package here defers its service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "NullTelemetry",
    "PrivacyPolicy",
    "RecordingTelemetry",
    "TelemetryProvider",
    "configure_telemetry",
    "current_trace_context",
    "install_correlation",
    "operation",
    "sanitize",
    "shutdown_telemetry",
    "telemetry_enabled",
    "telemetry_status",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.observability.logging import install_correlation
    from trading_system.observability.privacy import PrivacyPolicy, sanitize
    from trading_system.observability.provider import (
        NullTelemetry,
        RecordingTelemetry,
        TelemetryProvider,
    )
    from trading_system.observability.runtime import (
        configure_telemetry,
        shutdown_telemetry,
        telemetry_status,
    )
    from trading_system.observability.tracing import (
        current_trace_context,
        operation,
        telemetry_enabled,
    )

_LAZY: dict[str, str] = {
    "PrivacyPolicy": "trading_system.observability.privacy",
    "sanitize": "trading_system.observability.privacy",
    "NullTelemetry": "trading_system.observability.provider",
    "RecordingTelemetry": "trading_system.observability.provider",
    "TelemetryProvider": "trading_system.observability.provider",
    "operation": "trading_system.observability.tracing",
    "current_trace_context": "trading_system.observability.tracing",
    "telemetry_enabled": "trading_system.observability.tracing",
    "configure_telemetry": "trading_system.observability.runtime",
    "shutdown_telemetry": "trading_system.observability.runtime",
    "telemetry_status": "trading_system.observability.runtime",
    "install_correlation": "trading_system.observability.logging",
}


def __getattr__(name: str) -> Any:
    """Resolve a public name on first use, never at import.

    ``configure_telemetry`` reaches the OpenTelemetry SDK, whose exporter
    imports ``socket``, ``urllib`` and ``http``. An eager re-export here would
    put all three in the import graph of anything that merely called
    ``operation()`` — including the research agent, whose boundary test exists
    precisely to stop it being able to open a connection of its own.
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)
