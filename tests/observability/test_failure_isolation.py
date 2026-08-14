"""Telemetry cannot change what the trading system does.

The milestone's central safety claim, asserted rather than documented. If the
collector, Tempo, Prometheus, Loki and Grafana are all down — or the SDK is
half-installed, or the configuration is nonsense, or the exporter throws on
every call — the trading system behaves *identically*.

The strongest form of that claim is the last file in this module: the same
operation is run with telemetry off, with a recording provider, and with a
provider that raises on every method, and the **stored artifacts are compared
byte for byte**.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from tests.pnl import factories
from tests.pnl.test_calculator import compute
from trading_system.domain.enums import PnLStatus, TelemetryExportStatus
from trading_system.observability import metrics
from trading_system.observability.provider import NullTelemetry, RecordingTelemetry
from trading_system.observability.runtime import (
    configure_telemetry,
    shutdown_telemetry,
    telemetry_status,
)
from trading_system.observability.tracing import (
    current_trace_context,
    get_provider,
    operation,
    reset_provider,
    set_provider,
    telemetry_enabled,
)

pytestmark = pytest.mark.unit


class HostileTelemetry:
    """A provider that fails at every opportunity.

    Not a contrived case: an exporter with a full queue, a collector refusing
    connections and a half-installed SDK all surface as exceptions from these
    methods, at arbitrary points in an operation.
    """

    def __init__(self) -> None:
        self.attempts = 0

    @property
    def enabled(self) -> bool:
        self.attempts += 1
        return True

    def start_span(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> Any:
        raise RuntimeError("the collector is unreachable")

    def record_count(self, instrument: str, value: int = 1, **kwargs: Any) -> None:
        raise RuntimeError("the exporter queue is full")

    def record_duration(self, instrument: str, seconds: float, **kwargs: Any) -> None:
        raise RuntimeError("the exporter queue is full")

    def current_trace_context(self) -> tuple[str | None, str | None]:
        raise RuntimeError("no context")

    def shutdown(self) -> None:
        raise RuntimeError("shutdown failed too")


class PartiallyHostileTelemetry(RecordingTelemetry):
    """A provider whose spans throw once they are open.

    The nastier case: the operation starts, telemetry looks healthy, and then
    every attribute, status and end call fails part-way through.
    """

    def start_span(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> Any:
        class _Exploding:
            def set_attribute(self, key: str, value: Any) -> None:
                raise RuntimeError("boom")

            def set_attributes(self, attributes: Mapping[str, Any]) -> None:
                raise RuntimeError("boom")

            def record_error(self, error: BaseException) -> None:
                raise RuntimeError("boom")

            def set_status_ok(self) -> None:
                raise RuntimeError("boom")

            def end(self) -> None:
                raise RuntimeError("boom")

            trace_id = None
            span_id = None

        return _Exploding()


@pytest.fixture(autouse=True)
def _clean_provider():
    yield
    reset_provider()


# ---------------------------------------------------------------------------
# A broken provider changes nothing
# ---------------------------------------------------------------------------
def test_an_operation_completes_when_the_provider_raises() -> None:
    set_provider(HostileTelemetry())

    with operation("exit.evaluate", attributes={"trading.status": "WAIT"}):
        result = 2 + 2

    assert result == 4


def test_an_operation_completes_when_the_span_raises() -> None:
    set_provider(PartiallyHostileTelemetry())

    with operation("exit.evaluate", attributes={"trading.status": "WAIT"}):
        result = 2 + 2

    assert result == 4


def test_a_caller_exception_still_propagates_through_a_broken_provider() -> None:
    """Telemetry observes; it does not handle. Swallowing the caller's error
    would be the most dangerous possible 'helpful' behaviour."""
    set_provider(HostileTelemetry())

    # Nested deliberately: the point is that the span is opened *around* the
    # failing work, and combining the two contexts would change what is nested
    # inside what.
    with pytest.raises(ValueError, match="the real problem"), operation("exit.evaluate"):
        raise ValueError("the real problem")


def test_a_caller_exception_propagates_unchanged_when_the_span_raises() -> None:
    set_provider(PartiallyHostileTelemetry())

    # Nested deliberately: the point is that the span is opened *around* the
    # failing work, and combining the two contexts would change what is nested
    # inside what.
    with pytest.raises(ValueError, match="the real problem"), operation("exit.evaluate"):
        raise ValueError("the real problem")


def test_recording_a_metric_never_raises() -> None:
    set_provider(HostileTelemetry())

    metrics.record_count(metrics.EXECUTION_SUBMISSIONS_TOTAL, 1, labels={"status": "OK"})
    metrics.record_duration(metrics.EXECUTION_DURATION, 1.0, labels={"status": "OK"})


def test_reading_the_trace_context_never_raises() -> None:
    """A logging call must not be able to fail because telemetry is misbehaving."""
    set_provider(HostileTelemetry())

    assert current_trace_context() == (None, None)


def test_shutdown_never_raises() -> None:
    """A flush that hung on an unreachable collector would turn 'telemetry is
    down' into 'the process will not exit'."""
    set_provider(HostileTelemetry())

    shutdown_telemetry()

    assert isinstance(get_provider(), NullTelemetry)


# ---------------------------------------------------------------------------
# Configuration failures switch telemetry off, and nothing else
# ---------------------------------------------------------------------------
def test_telemetry_is_disabled_by_default(system_config) -> None:
    """The honest default for a machine with no collector on it."""
    assert system_config.observability.enabled is False


def test_disabled_telemetry_installs_the_null_provider(system_config) -> None:
    configure_telemetry(config=system_config.observability, service_version="0.1.0")

    assert telemetry_status() is TelemetryExportStatus.DISABLED
    assert telemetry_enabled() is False


def test_nonsense_configuration_never_raises_into_the_caller() -> None:
    """A telemetry configuration problem must not stop the process."""

    class Nonsense:
        enabled = True
        service_name = None
        exporter = None
        privacy = None

    provider = configure_telemetry(config=Nonsense(), service_version="0.1.0")

    assert isinstance(provider, NullTelemetry)
    assert telemetry_status() in (
        TelemetryExportStatus.MISCONFIGURED,
        TelemetryExportStatus.SDK_UNAVAILABLE,
    )


def test_a_missing_sdk_is_reported_distinctly_from_disabled(system_config) -> None:
    """'Switched off' and 'configured and unable to export' are different facts
    an operator needs to tell apart."""
    enabled = system_config.observability.model_copy(update={"enabled": True})

    configure_telemetry(config=enabled, service_version="0.1.0")

    assert telemetry_status() in (
        TelemetryExportStatus.ACTIVE,
        TelemetryExportStatus.SDK_UNAVAILABLE,
        TelemetryExportStatus.MISCONFIGURED,
    )


def test_telemetry_configuration_cannot_change_a_trading_policy(system_config) -> None:
    """Turning telemetry on changes what is *observed* and nothing decided.

    Asserted by comparing the whole trading policy either side of the change,
    rather than by comparing field names: ``enabled`` appears on the exit
    policy, on the execution policy and on the observability configuration, and
    those are three unrelated switches that happen to share a word.
    """
    before = {
        name: getattr(system_config, name).model_dump(mode="json")
        for name in ("risk", "execution", "exit", "campaign", "positions", "reconciliation")
    }

    noisiest = system_config.observability.model_copy(
        update={
            "enabled": True,
            "sampling": system_config.observability.sampling.model_copy(update={"ratio": 1.0}),
        }
    )
    changed = system_config.model_copy(update={"observability": noisiest})

    after = {
        name: getattr(changed, name).model_dump(mode="json")
        for name in ("risk", "execution", "exit", "campaign", "positions", "reconciliation")
    }
    assert before == after


def test_no_trading_policy_reads_the_observability_configuration(system_config) -> None:
    """There is no field through which a risk, execution or exit decision could
    consult telemetry."""
    for policy in (
        system_config.risk,
        system_config.execution,
        system_config.exit,
        system_config.campaign,
    ):
        payload = policy.model_dump(mode="json")
        assert "observability" not in payload
        assert "telemetry" not in payload
        assert "otlp" not in str(payload).lower()


# ---------------------------------------------------------------------------
# The strongest form: identical artifacts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "provider_factory",
    [
        pytest.param(NullTelemetry, id="telemetry-off"),
        pytest.param(RecordingTelemetry, id="telemetry-recording"),
        pytest.param(HostileTelemetry, id="telemetry-broken"),
        pytest.param(PartiallyHostileTelemetry, id="telemetry-half-broken"),
    ],
)
def test_the_same_operation_produces_the_same_artifact_under_any_telemetry(
    provider_factory,
) -> None:
    """The claim, in its strongest available form.

    A realised profit-and-loss result is a good subject: it is content
    addressed, it is money, and it is what the daily loss limit reads. If
    telemetry could perturb *anything*, this record would differ.
    """
    set_provider(provider_factory())

    result = compute(factories.entry_fills(), factories.exit_fills())

    assert result.status is PnLStatus.COMPLETE
    assert result.realized_gross_pnl == Decimal("400.00")
    assert result.realized_net_pnl == Decimal("397.00")
    assert result.pnl_id == "pnl-" + result.pnl_id.removeprefix("pnl-")

    reset_provider()
    without = compute(factories.entry_fills(), factories.exit_fills())
    assert result.model_dump(mode="json") == without.model_dump(mode="json")


def test_a_broken_provider_does_not_slow_a_decision_into_failure() -> None:
    """Every telemetry call is wrapped; none of them can turn into a retry, a
    backoff or a raise inside a trading operation."""
    hostile = HostileTelemetry()
    set_provider(hostile)

    for _ in range(50):
        with operation("exit.evaluate"):
            pass

    # It was asked every time and refused every time, and nothing propagated.
    assert hostile.attempts >= 50
