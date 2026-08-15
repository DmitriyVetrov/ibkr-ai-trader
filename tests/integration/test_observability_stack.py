"""Acceptance for the RUNNING observability stack (brief sections 11 and 31).

Milestone 11 shipped the collector, Tempo, Prometheus, Loki and Grafana with
every one recorded ``NOT_TESTED``. These tests are what closes that, and the
rule they follow is the one section 11 states outright:

    **A validated compose file is not proof that a stack is running.**

So every assertion here is an HTTP request to a live service. Nothing reads a
YAML file and concludes a backend is healthy, and nothing concludes a signal
arrived because the application sent one — arrival is established by asking the
backend for it, by id.

The suite **skips** when the stack is not up, and skipping is honest: the
readiness gate reports those criteria ``NOT_TESTED``, which is not a pass. Run
the stack first:

.. code-block:: bash

    make observability-up
    pytest tests/integration/test_observability_stack.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_system.infrastructure.settings import Settings, load_config
from trading_system.readiness.observability_probe import (
    ObservabilityProbe,
    http_get,
    probe_collector,
    probe_correlation,
    probe_grafana,
    probe_loki,
    probe_prometheus,
    probe_services,
    probe_tempo,
)
from trading_system.readiness.telemetry_emission import emit_acceptance_signals

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
NOW = __import__("datetime").datetime.now(__import__("datetime").UTC)


@pytest.fixture(scope="module")
def config():
    return load_config(REPO / "config")


@pytest.fixture(scope="module")
def probe(config) -> ObservabilityProbe:
    return ObservabilityProbe(config=config.readiness.observability_acceptance)


@pytest.fixture(scope="module", autouse=True)
def _require_a_running_stack(probe: ObservabilityProbe) -> None:
    """Skip unless every backend answers. Never pretend one did."""
    result = http_get(probe.config.grafana_url.rstrip("/") + "/api/health", timeout=2.0)
    if not result.ok:
        pytest.skip(
            "the observability stack is not running. Start it with `make observability-up`; "
            "readiness reports these criteria NOT_TESTED until it is."
        )


@pytest.fixture(scope="module")
def emission(config):
    """One real span, metric and log through the ordinary application path."""
    settings = Settings(_env_file=None, trading_mode="PAPER")
    return emit_acceptance_signals(settings=settings, config=config)


# ---------------------------------------------------------------------------
# The stack is genuinely up
# ---------------------------------------------------------------------------
def test_every_backend_answers_its_own_health_endpoint(probe: ObservabilityProbe) -> None:
    record = probe_services(probe, observed_at=NOW)
    services = record.detail["services"]
    unhealthy = sorted(name for name, ok in services.items() if not ok)
    assert not unhealthy, f"these services did not report healthy: {unhealthy}"
    assert len(services) == 5


# ---------------------------------------------------------------------------
# Telemetry actually flows
# ---------------------------------------------------------------------------
def test_the_application_emitted_a_real_trace(emission) -> None:
    assert emission.active, f"telemetry did not start: {emission.error}"
    assert emission.trace_id, "no trace id was produced"


def test_the_collector_accepted_spans_from_this_application(
    probe: ObservabilityProbe, emission
) -> None:
    """Being up is not the same as receiving. This is M11's exact gap."""
    record = probe_collector(probe, observed_at=NOW)
    assert record.detail["reachable"] is True
    accepted = record.detail["spans_accepted"]
    assert accepted is not None, "the collector reported no accepted-span counter"
    assert accepted > 0


def test_the_trace_reached_the_tracing_backend(probe: ObservabilityProbe, emission) -> None:
    """Looked up by id: a backend holding somebody else's traces proves nothing."""
    record = probe_tempo(probe, trace_id=emission.trace_id, observed_at=NOW)
    assert record.detail["trace_found"] is True, record.detail
    assert record.detail["spans"] >= 1


def test_the_metric_is_queryable_in_the_metrics_backend(
    probe: ObservabilityProbe, emission
) -> None:
    """The whole path: application, OTLP, collector, scrape, storage."""
    record = probe_prometheus(probe, metric=emission.metric, observed_at=NOW)
    assert record.detail["metric_found"] is True, record.detail


def test_the_log_line_reached_the_log_backend(probe: ObservabilityProbe, emission) -> None:
    record = probe_loki(
        probe,
        query='{service_name="trading-system"}',
        observed_at=NOW,
        expect_substring=emission.trace_id,
    )
    assert record.detail["log_found"] is True, record.detail


# ---------------------------------------------------------------------------
# Correlation (brief sections 12 and 13)
# ---------------------------------------------------------------------------
def test_one_trace_id_is_findable_in_both_backends(probe: ObservabilityProbe, emission) -> None:
    """The claim section 13 asks for, established by two independent lookups."""
    tempo = probe_tempo(probe, trace_id=emission.trace_id, observed_at=NOW)
    loki = probe_loki(
        probe,
        query='{service_name="trading-system"}',
        observed_at=NOW,
        expect_substring=emission.trace_id,
    )
    record = probe_correlation(trace_id=emission.trace_id, tempo=tempo, loki=loki, observed_at=NOW)
    assert record.detail["trace_found"] is True
    assert record.detail["log_found"] is True


# ---------------------------------------------------------------------------
# Grafana provisioning (brief section 11)
# ---------------------------------------------------------------------------
def test_grafana_is_healthy_and_provisioned(probe: ObservabilityProbe, config) -> None:
    """Asked over the API, not read off disk.

    A provisioning file Grafana rejected at start-up looks perfect on disk,
    which is exactly the failure mode section 11 calls out.
    """
    acceptance = config.readiness.observability_acceptance
    record = probe_grafana(
        probe,
        observed_at=NOW,
        required_datasources=acceptance.required_datasources,
        required_dashboards=acceptance.required_dashboards,
    )
    assert record.detail["grafana_healthy"] is True
    assert record.detail["missing_datasources"] == [], record.detail
    assert record.detail["missing_dashboards"] == [], record.detail


# ---------------------------------------------------------------------------
# Cardinality, against live exposition (brief section 14)
# ---------------------------------------------------------------------------
def test_no_domain_identifier_reached_a_metric_label(config, emission) -> None:
    """Checked against what the collector is actually exposing.

    Both spellings are searched — ``execution_id`` and ``trading.execution.id``
    — because a guard that catches one and not the other catches nothing.
    """
    from trading_system.readiness.collectors import collect_cardinality

    record = collect_cardinality(config=config, exposition=emission.exposition, observed_at=NOW)
    assert record.detail["exposition_checked"] is True, (
        "the collector's metric exposition could not be read, so this test would "
        "have asserted nothing"
    )
    assert record.detail["forbidden_labels_found"] == []


# ---------------------------------------------------------------------------
# Telemetry never changes trading behaviour
# ---------------------------------------------------------------------------
def test_the_emission_restores_the_previous_provider(config) -> None:
    """Turning telemetry on for a gate must not leave it on."""
    from trading_system.observability import tracing

    before = tracing.get_provider()
    emit_acceptance_signals(settings=Settings(_env_file=None, trading_mode="PAPER"), config=config)
    assert tracing.get_provider() is before
