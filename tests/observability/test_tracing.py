"""Spans: the business operations, their attributes, and what carries them.

The brief asks for stable spans for named business operations, domain
identifiers on them, and a distinction between a trace id and a domain
artifact id. These check all three, plus the property that makes it acceptable
to put a span call inside a trading service: with telemetry off it costs
nothing and changes nothing.
"""

from __future__ import annotations

import pytest

from trading_system.observability.attributes import (
    OPERATION_NAMES,
    TRADING_EXECUTION_ID,
    TRADING_POSITION_ID,
    TRADING_REASON_CODE,
    TRADING_STATUS,
)
from trading_system.observability.instrument import traced
from trading_system.observability.provider import NULL_SPAN, NullTelemetry, RecordingTelemetry
from trading_system.observability.tracing import (
    annotate,
    operation,
    reset_provider,
    set_provider,
    telemetry_enabled,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def telemetry():
    recorder = RecordingTelemetry()
    set_provider(recorder)
    yield recorder
    reset_provider()


# ---------------------------------------------------------------------------
# Span creation
# ---------------------------------------------------------------------------
def test_an_operation_creates_a_span(telemetry) -> None:
    with operation("exit.evaluate"):
        pass

    assert telemetry.span_named("exit.evaluate") is not None


def test_a_span_is_ended_exactly_once(telemetry) -> None:
    with operation("exit.evaluate"):
        pass

    assert telemetry.span_named("exit.evaluate").ended is True


def test_a_successful_operation_is_marked_ok(telemetry) -> None:
    with operation("exit.evaluate"):
        pass

    assert telemetry.span_named("exit.evaluate").ok is True


def test_a_failing_operation_records_the_error_and_re_raises(telemetry) -> None:
    with pytest.raises(ValueError, match="something went wrong"), operation("exit.evaluate"):
        raise ValueError("something went wrong")

    span = telemetry.span_named("exit.evaluate")
    assert span.errors == ["ValueError"]
    assert span.ended is True
    assert span.ok is False


def test_nested_operations_produce_nested_spans(telemetry) -> None:
    """The trace hierarchy the brief asks for: a workflow with the stages
    beneath it."""
    with (
        operation("trading.workflow"),
        operation("research.run"),
        operation("llm.generate"),
    ):
        pass

    names = [span.name for span in telemetry.spans]
    assert names == ["trading.workflow", "research.run", "llm.generate"]


# ---------------------------------------------------------------------------
# Domain identifiers
# ---------------------------------------------------------------------------
def test_a_span_carries_the_domain_identifiers_it_was_given(telemetry) -> None:
    with operation(
        "exit.evaluate",
        attributes={TRADING_POSITION_ID: "position-1", TRADING_STATUS: "WAIT"},
    ):
        pass

    span = telemetry.span_named("exit.evaluate")
    assert span.attributes[TRADING_POSITION_ID] == "position-1"
    assert span.attributes[TRADING_STATUS] == "WAIT"


def test_an_attribute_can_be_added_once_the_value_is_known(telemetry) -> None:
    """The execution id after the record is minted, the reason code after the
    engine has decided."""
    with operation("execution.open") as span:
        annotate(span, {TRADING_EXECUTION_ID: "execution-1"})

    assert telemetry.span_named("execution.open").attributes[TRADING_EXECUTION_ID] == (
        "execution-1"
    )


def test_a_trace_id_is_not_a_domain_identifier(telemetry) -> None:
    """Both levels exist and neither substitutes for the other: a trace id says
    which execution of the software, an execution id says which trade."""
    with operation("execution.open", attributes={TRADING_EXECUTION_ID: "execution-1"}):
        trace_id, _ = telemetry.current_trace_context()

    span = telemetry.span_named("execution.open")
    assert span is not None
    assert trace_id is not None
    assert trace_id != "execution-1"
    assert span.attributes[TRADING_EXECUTION_ID] == "execution-1"


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------
def test_every_span_name_used_in_the_source_is_in_the_vocabulary(repo_root) -> None:
    """A typo would silently create a second operation that no dashboard
    queries and no alert rule matches."""
    import ast

    used: set[str] = set()
    for path in (repo_root / "src" / "trading_system").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in ("operation", "traced"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    used.add(value)

    unknown = sorted(used - set(OPERATION_NAMES))
    assert unknown == [], f"span names outside the vocabulary: {unknown}"


@pytest.mark.parametrize(
    "name",
    [
        "trading.workflow",
        "universe.selection",
        "research.run",
        "strategy.selection",
        "contract.selection",
        "risk.evaluate",
        "allocation.calculate",
        "execution.open",
        "execution.close",
        "position.monitor",
        "exit.evaluate",
        "exit.decision",
        "exit.execute",
        "exit.confirm",
        "reconciliation.run",
        "llm.generate",
    ],
)
def test_every_operation_the_brief_names_is_in_the_vocabulary(name: str) -> None:
    assert name in OPERATION_NAMES


# ---------------------------------------------------------------------------
# Telemetry off
# ---------------------------------------------------------------------------
def test_an_operation_with_telemetry_off_yields_the_shared_null_span() -> None:
    """No allocation, no attribute filtering, no cost beyond a global read.

    An operation must not pay for a feature that is switched off, and the
    shipped default is off.
    """
    set_provider(NullTelemetry())
    try:
        with operation("exit.evaluate", attributes={TRADING_STATUS: "WAIT"}) as span:
            assert span is NULL_SPAN
        assert telemetry_enabled() is False
    finally:
        reset_provider()


def test_the_decorator_calls_straight_through_when_telemetry_is_off() -> None:
    calls: list[int] = []

    @traced("exit.evaluate")
    def work(value: int) -> int:
        calls.append(value)
        return value * 2

    set_provider(NullTelemetry())
    try:
        assert work(21) == 42
    finally:
        reset_provider()

    assert calls == [21]


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------
def test_the_decorator_records_a_span_and_a_metric(telemetry) -> None:
    from trading_system.observability import metrics

    @traced(
        "exit.evaluate",
        count=metrics.EXIT_EVALUATIONS_TOTAL,
        duration=metrics.EXIT_EVALUATION_DURATION,
        labels=lambda result: {"decision": result},
    )
    def work() -> str:
        return "WAIT"

    assert work() == "WAIT"
    assert telemetry.span_named("exit.evaluate") is not None
    assert telemetry.counts_for(metrics.EXIT_EVALUATIONS_TOTAL)[0].labels == {"decision": "WAIT"}
    assert telemetry.durations[0].instrument == metrics.EXIT_EVALUATION_DURATION


def test_the_decorator_reads_attributes_from_the_call(telemetry) -> None:
    @traced(
        "exit.evaluate",
        attributes=lambda position_id, **kwargs: {TRADING_POSITION_ID: position_id},
    )
    def work(position_id: str) -> str:
        return "WAIT"

    work("position-1")

    assert telemetry.span_named("exit.evaluate").attributes[TRADING_POSITION_ID] == ("position-1")


def test_the_decorator_reads_attributes_from_the_result(telemetry) -> None:
    @traced(
        "exit.decision",
        result_attributes=lambda result: {TRADING_REASON_CODE: result},
    )
    def work() -> str:
        return "TAKE_PROFIT"

    work()

    assert telemetry.span_named("exit.decision").attributes[TRADING_REASON_CODE] == ("TAKE_PROFIT")


def test_the_decorator_never_changes_the_return_value(telemetry) -> None:
    sentinel = object()

    @traced("exit.evaluate")
    def work() -> object:
        return sentinel

    assert work() is sentinel


def test_the_decorator_re_raises_and_counts_the_failure(telemetry) -> None:
    from trading_system.observability import metrics

    @traced("execution.open", failure_count=metrics.EXECUTION_REJECTIONS_TOTAL)
    def work() -> None:
        raise RuntimeError("the broker refused")

    with pytest.raises(RuntimeError, match="the broker refused"):
        work()

    assert telemetry.counts_for(metrics.EXECUTION_REJECTIONS_TOTAL)


def test_an_attribute_extractor_that_raises_is_ignored(telemetry) -> None:
    """Telemetry is never load bearing, including its own helpers."""

    def explode(*args: object, **kwargs: object) -> dict[str, str]:
        raise RuntimeError("bad extractor")

    @traced("exit.evaluate", attributes=explode, result_attributes=explode, labels=explode)
    def work() -> str:
        return "WAIT"

    assert work() == "WAIT"
    assert telemetry.span_named("exit.evaluate") is not None


def test_the_decorator_preserves_the_wrapped_function_identity() -> None:
    @traced("exit.evaluate")
    def work() -> None:
        """The original docstring."""

    assert work.__name__ == "work"
    assert work.__doc__ == "The original docstring."
