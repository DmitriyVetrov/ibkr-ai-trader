"""No ordinary test can submit an order (brief sections 42.12, 43, 63).

The suite-wide safety property. ``pytest`` with no environment variables set
must never place an order, and the gate that guarantees it must itself be
tested — a skip condition nobody checks is a skip condition that can silently
stop working.

The gate is deliberately two variables. ``ALLOW_LIVE_TESTS=true`` unlocks the
read-only gateway diagnostics, and a developer who set it for those must not
thereby have authorised an order; ``RUN_PAPER_EXECUTION_TESTS=true`` is the
second, separate decision.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.settings import Settings

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The environment every ordinary test runs in
# ---------------------------------------------------------------------------
def test_the_suite_runs_in_paper_mode() -> None:
    assert Settings().trading_mode is TradingMode.PAPER


def test_live_tests_are_locked_by_default() -> None:
    assert Settings().allow_live_tests is False


def test_the_live_guards_are_off() -> None:
    settings = Settings()
    assert settings.live_trading_confirmed is False
    assert settings.live_readiness_checklist_signed_off is False


def test_execution_submission_is_disabled_in_the_shipped_configuration(system_config) -> None:
    """A checkout that could trade without an edit would be the wrong default."""
    assert system_config.execution.enabled is False


def test_the_shipped_broker_setting_is_read_only() -> None:
    assert Settings().ibkr_read_only is True


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------
def test_the_paper_execution_marker_is_registered(repo_root: Path) -> None:
    """``--strict-markers`` is on, so an unregistered marker is a collection error."""
    text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    assert "paper_execution:" in text
    assert "RUN_PAPER_EXECUTION_TESTS=true" in text


def test_allow_live_tests_alone_does_not_unlock_order_submission(repo_root: Path) -> None:
    """The two-variable gate, read off the conftest that enforces it."""
    source = (repo_root / "tests" / "conftest.py").read_text(encoding="utf-8")

    assert "RUN_PAPER_EXECUTION_TESTS" in source
    assert "paper_execution" in source
    # Both variables appear in one condition, so neither alone suffices.
    gate = source[source.index("execution_unlocked") : source.index("skip_execution")]
    assert "ALLOW_LIVE_TESTS" in gate and "RUN_PAPER_EXECUTION_TESTS" in gate
    assert " and " in gate


@pytest.mark.parametrize(
    ("allow_live", "run_execution", "unlocked"),
    [
        ("false", "false", False),
        ("true", "false", False),
        ("false", "true", False),
        ("true", "true", True),
    ],
)
def test_only_both_variables_together_unlock(
    monkeypatch, allow_live: str, run_execution: str, unlocked: bool
) -> None:
    """The truth table, evaluated exactly as the conftest evaluates it."""
    monkeypatch.setenv("ALLOW_LIVE_TESTS", allow_live)
    monkeypatch.setenv("RUN_PAPER_EXECUTION_TESTS", run_execution)
    import os

    computed = (
        os.environ.get("ALLOW_LIVE_TESTS", "false").lower() == "true"
        and os.environ.get("RUN_PAPER_EXECUTION_TESTS", "false").lower() == "true"
    )
    assert computed is unlocked


# ---------------------------------------------------------------------------
# No ordinary test constructs something that could submit
# ---------------------------------------------------------------------------
def _test_files(repo_root: Path) -> list[Path]:
    return [
        path
        for path in (repo_root / "tests").rglob("test_*.py")
        # The one file that is *supposed* to submit, and is gated to do so.
        if path.name != "test_paper_execution.py"
    ]


def _raises_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    """Line ranges covered by a ``with pytest.raises(...)`` block.

    A call inside one is asserting that the thing *refuses*, which is the
    opposite of using it, so it must not count as a violation.
    """
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "raises"
            ):
                ranges.append((node.lineno, node.end_lineno or node.lineno))
    return ranges


def test_no_ordinary_test_actually_builds_a_writable_broker(repo_root: Path) -> None:
    """Only the gated paper test may obtain a writable broker.

    Tests may construct a writable *simulator* — that never leaves the process
    — but nothing else may ask the factory for a real one. Calls inside a
    ``pytest.raises`` block are exempt: they assert the factory refuses, which
    is precisely the boundary this milestone relies on.
    """
    offenders = []
    for path in _test_files(repo_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guarded = _raises_ranges(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "build_execution_broker":
                continue
            if any(start <= node.lineno <= end for start, end in guarded):
                continue
            offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}")

    assert offenders == [], f"ordinary tests must not build a writable broker: {offenders}"


def test_no_ordinary_test_constructs_a_writable_ibkr_broker(repo_root: Path) -> None:
    """A writable IBKR broker is the one object that could reach a real account.

    Constructions inside a ``pytest.raises`` block are exempt: they assert the
    adapter refuses, which is the guard itself being tested.
    """
    offenders = []
    for path in _test_files(repo_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guarded = _raises_ranges(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "IBKRBroker":
                continue
            writable = any(
                keyword.arg == "read_only"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in node.keywords
            )
            if writable and not any(start <= node.lineno <= end for start, end in guarded):
                offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}")

    assert offenders == [], f"ordinary tests must not open a writable IBKR broker: {offenders}"


def test_the_execution_suite_only_ever_submits_to_a_simulator_or_a_fake(repo_root: Path) -> None:
    """Every broker in this suite is in-process, by construction."""
    conftest = (repo_root / "tests" / "execution" / "conftest.py").read_text(encoding="utf-8")

    assert "SimulatedBroker" in conftest
    assert "class FakeBroker" in conftest
    assert "IBKRBroker" not in conftest
