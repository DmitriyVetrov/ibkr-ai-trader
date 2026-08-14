"""Metrics carry no domain identifiers.

A domain identifier as a metric label is one time series per trade, stored for
the retention period, on a system this one does not own. These identifiers
belong in traces and logs — which are built for exactly that, and which are
where the navigation path from a metric to a trade actually goes.

The guard is enforced twice: ``config/observability.yaml`` lists the forbidden
labels, and :mod:`trading_system.observability.metrics` refuses them at the
point of recording whatever the configuration says — because a metrics
explosion caused by a mis-edited YAML file is still a metrics explosion.
"""

from __future__ import annotations

import pytest

from trading_system.observability import metrics
from trading_system.observability.provider import RecordingTelemetry
from trading_system.observability.tracing import reset_provider, set_provider

pytestmark = pytest.mark.unit

#: The identifiers the brief names explicitly.
FORBIDDEN = (
    "trace_id",
    "span_id",
    "campaign_id",
    "universe_selection_id",
    "research_run_id",
    "strategy_decision_id",
    "contract_selection_id",
    "risk_evaluation_id",
    "allocation_id",
    "execution_id",
    "position_id",
    "exit_id",
    "broker_order_id",
)


@pytest.fixture
def telemetry():
    recorder = RecordingTelemetry()
    set_provider(recorder)
    yield recorder
    reset_provider()


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label", FORBIDDEN)
def test_a_domain_identifier_never_becomes_a_metric_label(telemetry, label: str) -> None:
    metrics.record_count(
        metrics.EXECUTION_SUBMISSIONS_TOTAL, 1, labels={label: "abc123", "status": "SUCCESS"}
    )

    measurement = telemetry.counts[0]
    assert label not in measurement.labels
    assert measurement.labels["status"] == "SUCCESS"


@pytest.mark.parametrize("label", FORBIDDEN)
def test_the_guard_covers_durations_too(telemetry, label: str) -> None:
    metrics.record_duration(
        metrics.EXECUTION_DURATION, 1.5, labels={label: "abc123", "strategy": "LONG_CALL"}
    )

    measurement = telemetry.durations[0]
    assert label not in measurement.labels
    assert measurement.labels["strategy"] == "LONG_CALL"


@pytest.mark.parametrize("label", FORBIDDEN)
def test_the_guard_is_in_code_not_only_in_configuration(label: str) -> None:
    """A mis-edited YAML file must not be able to remove it."""
    assert label in metrics.FORBIDDEN_LABELS


def test_the_dotted_attribute_names_are_refused_as_labels(telemetry) -> None:
    """An attribute constant used as a label by mistake is refused under
    either spelling."""
    from trading_system.observability.attributes import (
        TRADING_EXECUTION_ID,
        TRADING_POSITION_ID,
        TRADING_SYMBOL,
    )

    metrics.record_count(
        metrics.EXIT_EVALUATIONS_TOTAL,
        1,
        labels={
            TRADING_EXECUTION_ID: "execution-1",
            TRADING_POSITION_ID: "position-1",
            TRADING_SYMBOL: "NVDA",
            "decision": "WAIT",
        },
    )

    assert telemetry.counts[0].labels == {"decision": "WAIT"}


def test_a_symbol_is_not_a_metric_label(telemetry) -> None:
    """A universe of a few hundred underlyings is a few hundred time series
    per metric. The symbol is on the span."""
    metrics.record_count(metrics.RESEARCH_RUNS_TOTAL, 1, labels={"symbol": "NVDA"})

    assert telemetry.counts[0].labels == {}


def test_a_dropped_label_does_not_drop_the_measurement(telemetry) -> None:
    """The signal is worth more than the label. What must not happen is the
    label reaching the backend."""
    metrics.record_count(metrics.RISK_APPROVED_TOTAL, 1, labels={"execution_id": "x"})

    assert len(telemetry.counts) == 1
    assert telemetry.counts[0].value == 1


# ---------------------------------------------------------------------------
# What a label may be
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "label", ["status", "strategy", "trading_mode", "execution_type", "reason_category", "agent"]
)
def test_a_low_cardinality_label_survives(telemetry, label: str) -> None:
    metrics.record_count(metrics.ALLOCATIONS_TOTAL, 1, labels={label: "VALUE"})

    assert telemetry.counts[0].labels == {label: "VALUE"}


def test_the_shipped_configuration_lists_the_forbidden_labels(system_config) -> None:
    configured = set(system_config.observability.metrics.forbidden_labels)

    for label in FORBIDDEN:
        assert label in configured


def test_the_cardinality_guard_cannot_be_emptied() -> None:
    from trading_system.infrastructure.settings import ObservabilityMetricsConfig

    with pytest.raises(ValueError, match="cardinality guard"):
        ObservabilityMetricsConfig(forbidden_labels=[])


# ---------------------------------------------------------------------------
# Emitted spans DO carry the identifiers
# ---------------------------------------------------------------------------
def test_the_identifiers_belong_on_spans_instead(telemetry) -> None:
    """Both levels exist and neither substitutes for the other: a trace id says
    which execution of the software, an execution id says which trade."""
    from trading_system.observability.attributes import TRADING_EXECUTION_ID
    from trading_system.observability.tracing import operation

    with operation("execution.open", attributes={TRADING_EXECUTION_ID: "execution-1"}):
        pass

    span = telemetry.span_named("execution.open")
    assert span.attributes[TRADING_EXECUTION_ID] == "execution-1"


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------
def test_every_metric_the_brief_names_exists() -> None:
    """A dashboard query against a metric nobody emits is a permanently empty
    panel, which is worse than an absent one."""
    required = {
        "trading_workflows_total",
        "trading_workflows_failed_total",
        "universe_runs_total",
        "research_runs_total",
        "strategy_decisions_total",
        "contract_selections_total",
        "risk_approved_total",
        "risk_rejected_total",
        "allocations_total",
        "execution_submissions_total",
        "execution_rejections_total",
        "execution_fills_total",
        "execution_cancellations_total",
        "execution_unknown_total",
        "positions_opened_total",
        "positions_closed_total",
        "exit_evaluations_total",
        "exit_wait_total",
        "exit_block_total",
        "exit_triggered_total",
        "trailing_triggered_total",
        "expiration_exits_total",
        "thesis_invalidations_total",
        "take_profit_exits_total",
        "max_loss_exits_total",
        "broker_connection_errors_total",
        "broker_timeouts_total",
        "broker_requests_total",
        "broker_request_duration_seconds",
        "llm_requests_total",
        "llm_errors_total",
        "llm_latency_seconds",
        "llm_tokens_total",
        "trading_workflow_duration_seconds",
        "research_duration_seconds",
        "strategy_duration_seconds",
        "contract_selection_duration_seconds",
        "risk_duration_seconds",
        "allocation_duration_seconds",
        "execution_duration_seconds",
        "broker_submission_duration_seconds",
        "position_monitor_duration_seconds",
        "exit_evaluation_duration_seconds",
    }

    assert required <= set(metrics.METRIC_NAMES)


def test_the_metric_names_are_unique() -> None:
    assert len(metrics.METRIC_NAMES) == len(set(metrics.METRIC_NAMES))
