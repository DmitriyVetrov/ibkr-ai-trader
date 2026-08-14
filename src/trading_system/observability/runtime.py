"""Starting and stopping telemetry (Milestone 11).

One function the process calls at start-up, one at exit, and a status anybody
can read. This is the only module that names
:mod:`trading_system.observability.otel`, which is the only module that names
the OpenTelemetry SDK — a chain that exists so the SDK's transitive imports
(``socket``, ``urllib``, ``http``) never appear in the import graph of a
trading package that has a boundary test forbidding them.

.. code-block:: text

    CLI / scheduler start
          |
    configure_telemetry(settings, config)
          |
    +-- disabled?           -> NullTelemetry, status DISABLED
    +-- SDK missing?        -> NullTelemetry, status SDK_UNAVAILABLE
    +-- exporter refused?   -> NullTelemetry, status MISCONFIGURED
    +-- otherwise           -> OpenTelemetryProvider, status ACTIVE

Every one of those four outcomes produces **identical trading behaviour**. That
is the milestone's central safety claim and
``tests/observability/test_failure_isolation.py`` asserts it by running the
same operations under each and comparing the stored artifacts.

:func:`configure_telemetry` never raises. A telemetry configuration problem is
reported and telemetry is switched off; it can never stop the process, and it
can never alter a trading policy — the observability configuration is loaded
into its own model and has no path to any risk, execution or exit setting.
"""

from __future__ import annotations

from contextlib import suppress

from trading_system.domain.enums import TelemetryExportStatus
from trading_system.infrastructure.logging import get_logger
from trading_system.observability import tracing
from trading_system.observability.privacy import PrivacyPolicy
from trading_system.observability.provider import NullTelemetry, TelemetryProvider

__all__ = [
    "configure_telemetry",
    "shutdown_telemetry",
    "telemetry_status",
]

_logger = get_logger(__name__)

#: What the last :func:`configure_telemetry` concluded. Read by ``ops health``
#: so an operator can tell "switched off" from "configured and failing" —
#: different facts calling for different responses, and neither of them a
#: trading problem.
_status: TelemetryExportStatus = TelemetryExportStatus.DISABLED


def telemetry_status() -> TelemetryExportStatus:
    """What the telemetry side channel is currently doing."""
    return _status


def configure_telemetry(
    *,
    config: object,
    service_version: str = "0.0.0",
    provider: TelemetryProvider | None = None,
) -> TelemetryProvider:
    """Install a telemetry provider. Never raises, whatever goes wrong.

    ``provider`` is an explicit override, used by tests to install a
    :class:`~trading_system.observability.provider.RecordingTelemetry` without
    an SDK or a collector.

    Returns the provider that was actually installed, which on any failure path
    is :class:`~trading_system.observability.provider.NullTelemetry`. Callers
    do not check it — the whole point is that they behave the same either way —
    but it is returned so a diagnostic can report what happened.
    """
    global _status

    if provider is not None:
        tracing.set_provider(provider)
        tracing.set_privacy_policy(_policy(config))
        _status = TelemetryExportStatus.ACTIVE
        return provider

    enabled = bool(getattr(config, "enabled", False))
    if not enabled:
        tracing.reset_provider()
        tracing.set_privacy_policy(_policy(config))
        _status = TelemetryExportStatus.DISABLED
        return tracing.get_provider()

    try:
        from trading_system.observability.otel import build_provider, sdk_available

        if not sdk_available():
            _status = TelemetryExportStatus.SDK_UNAVAILABLE
            _logger.info(
                "observability.disabled",
                reason="SDK_UNAVAILABLE",
                detail=(
                    "telemetry is enabled in configuration but the OpenTelemetry SDK is not "
                    "installed. Trading behaviour is unchanged; install the [observability] "
                    "extra to enable it"
                ),
            )
            tracing.reset_provider()
            tracing.set_privacy_policy(_policy(config))
            return tracing.get_provider()

        built = build_provider(config, service_version=service_version)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover - telemetry must never raise
        _status = TelemetryExportStatus.MISCONFIGURED
        _logger.warning(
            "observability.disabled",
            reason="MISCONFIGURED",
            error=str(exc),
            detail=(
                "telemetry could not be initialised. It is switched off and no trading "
                "policy is affected — the observability configuration has no path to a risk, "
                "execution or exit setting"
            ),
        )
        tracing.reset_provider()
        tracing.set_privacy_policy(_policy(config))
        return tracing.get_provider()

    if built is None:
        _status = TelemetryExportStatus.MISCONFIGURED
        tracing.reset_provider()
        tracing.set_privacy_policy(_policy(config))
        return tracing.get_provider()

    tracing.set_provider(built)
    tracing.set_privacy_policy(_policy(config))
    _status = TelemetryExportStatus.ACTIVE
    _logger.info(
        "observability.enabled",
        service=getattr(config, "service_name", "trading-system"),
        environment=getattr(config, "environment", "local"),
        endpoint=getattr(getattr(config, "exporter", None), "endpoint", None),
        detail=(
            "telemetry exports over OTLP to a collector. The application has no dependency "
            "on Tempo, Prometheus, Loki or Grafana"
        ),
    )
    return built


def shutdown_telemetry() -> None:
    """Flush and stop. Never raises, and never blocks the process from exiting."""
    global _status
    provider = tracing.get_provider()
    with suppress(Exception):  # a shutdown that raised would block process exit
        provider.shutdown()
    tracing.reset_provider()
    _status = TelemetryExportStatus.DISABLED


def _policy(config: object) -> PrivacyPolicy:
    """The redaction rules, defaulting to the strictest when unavailable.

    A configuration that failed to load leaves the *strict* policy in force
    rather than none at all. The failure mode of the alternative is a
    deployment that emits everything because a YAML key was misspelled.
    """
    return PrivacyPolicy.of(getattr(config, "privacy", None))


def is_active() -> bool:
    """Whether telemetry is currently exporting. For diagnostics only."""
    return (
        _status is TelemetryExportStatus.ACTIVE
        and isinstance(tracing.get_provider(), NullTelemetry) is False
    )
