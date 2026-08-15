"""Structured logs reach the telemetry side channel (Milestone 12 correction).

Two defects Milestone 11 left, both found by the Milestone 12 acceptance gate
asking a log backend for a specific trace id and getting nothing back:

1. **No OTLP log exporter existed.** ``deploy/otel/collector.yaml`` shipped a
   ``logs`` pipeline wired to Loki, and ``observability/otel.py`` built only a
   tracer and a meter. The pipeline was correct and permanently unfed.

2. **Correlation never reached module-level loggers.** ``get_logger`` returned
   an eagerly-bound structlog logger, and ``BoundLoggerLazyProxy.bind()``
   snapshots the processor chain as it stands at *import* time. The trace
   correlation processor is installed at *start-up*, after imports — so every
   ``_logger = get_logger(__name__)`` in the system emitted lines with no
   ``trace_id``. The lines still appeared and still looked right, which is
   exactly why nobody noticed.

Both are regressions worth pinning: the failure mode of each is a log that
reads perfectly and cannot be joined to its trace.
"""

from __future__ import annotations

import pytest

from trading_system.infrastructure.logging import configure_logging, get_logger
from trading_system.observability import logging as correlation
from trading_system.observability import tracing
from trading_system.observability.provider import NullTelemetry, RecordingTelemetry

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_telemetry():
    """Restore the global ``structlog`` configuration after every test.

    Not optional hygiene. ``configure_logging`` installs a
    ``PrintLoggerFactory`` bound to whatever ``sys.stderr`` is at that instant,
    and the tests below reconfigure logging while pytest's capture fixture owns
    that stream. Without this restore, the global configuration keeps a handle
    on a capture buffer that is closed at teardown, and *every later test in
    the whole suite* dies with ``ValueError: I/O operation on closed file`` —
    which is how 67 unrelated universe tests failed the first time this file
    ran as part of the full suite.
    """
    import structlog

    saved = structlog.get_config()
    configure_logging()
    yield
    tracing.reset_provider()
    correlation.reset_correlation()
    structlog.configure(**saved)


# ---------------------------------------------------------------------------
# The processor reaches a logger bound before it was installed
# ---------------------------------------------------------------------------
def test_a_logger_bound_before_startup_still_gets_its_trace_id() -> None:
    """The defect: a module-level logger froze the chain at import time."""
    early = get_logger("bound.early")  # as every module does, at import

    telemetry = RecordingTelemetry(trace_id="a" * 32, span_id="b" * 16)
    tracing.set_provider(telemetry)
    correlation.install_correlation(export_otlp=True)

    with tracing.operation("some.operation"):
        early.info("an.event")

    assert telemetry.logs, "no log reached the telemetry side channel"
    assert telemetry.logs[0].attributes.get("trace_id") == "a" * 32
    assert telemetry.logs[0].attributes.get("span_id") == "b" * 16


def test_a_logger_created_after_startup_also_works() -> None:
    telemetry = RecordingTelemetry(trace_id="c" * 32, span_id="d" * 16)
    tracing.set_provider(telemetry)
    correlation.install_correlation(export_otlp=True)

    with tracing.operation("some.operation"):
        get_logger("bound.late").info("an.event")

    assert telemetry.logs
    assert telemetry.logs[0].attributes.get("trace_id") == "c" * 32


# ---------------------------------------------------------------------------
# Export is opt-in
# ---------------------------------------------------------------------------
def test_no_log_is_exported_when_the_switch_is_off() -> None:
    """``export_otlp`` ships false: stdout gets the line either way."""
    telemetry = RecordingTelemetry()
    tracing.set_provider(telemetry)
    correlation.install_correlation(export_otlp=False)

    with tracing.operation("some.operation"):
        get_logger("quiet").info("an.event")

    assert telemetry.logs == []


def test_the_export_flag_defaults_to_off() -> None:
    correlation.reset_correlation()
    assert correlation.otlp_export_enabled() is False


def test_the_shipped_configuration_ships_log_export_off(system_config=None) -> None:
    """A machine with no collector should not try to reach one."""
    from pathlib import Path

    from trading_system.infrastructure.settings import load_config

    config = load_config(Path(__file__).resolve().parents[2] / "config")
    assert config.observability.logging.export_otlp is False


# ---------------------------------------------------------------------------
# It never becomes load bearing
# ---------------------------------------------------------------------------
def test_a_provider_without_emit_log_is_simply_skipped() -> None:
    """``emit_log`` is an optional capability, reached through ``getattr``.

    A provider that predates it exports no logs, which is what every provider
    did before — never an ``AttributeError`` inside a logging statement.
    """

    class LegacyProvider(NullTelemetry):
        emit_log = None  # type: ignore[assignment]

        @property
        def enabled(self) -> bool:
            return True

    tracing.set_provider(LegacyProvider())
    correlation.install_correlation(export_otlp=True)
    get_logger("legacy").info("an.event")  # must not raise


def test_a_provider_that_raises_cannot_break_a_log_statement() -> None:
    """A logging call must never fail because telemetry is misbehaving."""

    class HostileProvider(NullTelemetry):
        @property
        def enabled(self) -> bool:
            return True

        def emit_log(self, level, message, *, attributes=None):
            raise RuntimeError("the collector exploded")

    tracing.set_provider(HostileProvider())
    correlation.install_correlation(export_otlp=True)
    get_logger("hostile").info("an.event")  # must not raise


def test_the_log_line_still_reaches_stdout_when_export_is_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deliberately *as well as*, never instead of.

    stdout is where an operator with no observability stack reads what
    happened; a log path that depended on a collector would fail exactly when
    somebody needed it most.
    """
    # Reconfigured inside the test so ``PrintLoggerFactory`` binds the stream
    # ``capsys`` has already replaced. It captures ``sys.stderr`` once, at
    # configure time, so a chain configured before the fixture writes to the
    # real terminal and the assertion sees nothing.
    configure_logging()
    tracing.set_provider(RecordingTelemetry())
    correlation.install_correlation(export_otlp=True)
    get_logger("visible").info("an.event")
    assert "an.event" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The privacy filter still applies
# ---------------------------------------------------------------------------
def test_a_forbidden_attribute_never_reaches_an_exported_log() -> None:
    """A log line is not an audit archive either."""
    telemetry = RecordingTelemetry()
    tracing.set_provider(telemetry)
    correlation.install_correlation(export_otlp=True)

    get_logger("secretive").info("an.event", api_key="super-secret", password="hunter2")

    assert telemetry.logs
    attributes = telemetry.logs[0].attributes
    assert "api_key" not in attributes
    assert "password" not in attributes
    assert "super-secret" not in str(attributes)
