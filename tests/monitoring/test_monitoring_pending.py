"""Monitoring — what Milestone 11 delivered, and what is still pending.

Milestone 10 delivered the exit engine, the position lifecycle, the trailing
stop, the expiration policy and the thesis check. Milestone 11 delivered the
**scheduler** that runs them: ``config/schedules.yaml`` now describes jobs a
process actually executes, and those live in ``tests/operations`` — the suites
are named for the packages they test, and the package is
``trading_system.operations``.

What is still absent, and deliberately, is the specification's **separate
Thesis Monitor**, which returns ``VALID / WEAKENING / INVALIDATED / UNKNOWN``.
Milestone 10 evaluates a stored thesis deterministically inside the exit policy
and never returns ``WEAKENING``: deciding that a thesis has weakened without
being falsified is a judgement, and no milestone has made it. The job is
registered, disabled and honest — running it records ``SKIPPED`` with
``NOT_IMPLEMENTED`` rather than fabricating a verdict — and this file keeps
that gap visible in the test report instead of silently absent.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.skip(reason="The separate thesis monitor is delivered after Milestone 11")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")


def test_the_exit_operations_a_scheduler_calls_exist() -> None:
    """The seam Milestone 10 left, still asserted now that it is used.

    Milestone 10's contribution to automation was a *callable operation* that
    is safe to run repeatedly: no process-local memory, an idempotent store,
    and a re-run over unchanged state that re-observes rather than deciding
    again. Milestone 11's scheduler depends on exactly that.
    """
    from trading_system.exit.service import ExitService

    for operation in ("monitor", "evaluate", "confirm", "open_positions"):
        assert callable(getattr(ExitService, operation)), operation


def test_the_scheduler_calls_those_operations_rather_than_reimplementing_them() -> None:
    """The scheduler orchestrates; it contains no trading logic.

    Asserted by reading the job module: the position-monitor job's whole body
    is a call to ``ExitService.monitor``. A scheduler that re-derived whether a
    position should close would be a second, untested copy of a safety
    decision.
    """
    import inspect

    from trading_system.operations import jobs

    source = inspect.getsource(jobs._position_monitor)
    assert "service.monitor(" in source
    assert "ExitPolicyEngine" not in source
    assert "trailing" not in source.lower()


def test_the_thesis_monitor_is_registered_and_honest() -> None:
    """Registered so the gap is visible in ``ops jobs``; disabled so it never
    runs; and it fabricates no verdict when it is asked to."""
    from trading_system.domain.enums import JobSkipReason
    from trading_system.operations.jobs import JOB_BUILDERS, _thesis_monitor

    assert "thesis_monitor" in JOB_BUILDERS
    outcome = _thesis_monitor(None)  # type: ignore[arg-type]
    assert outcome.skipped is JobSkipReason.NOT_IMPLEMENTED
    assert "not built" in outcome.summary


def test_the_shipped_configuration_leaves_the_thesis_monitor_disabled(
    system_config,
) -> None:
    assert system_config.schedules.jobs["thesis_monitor"].enabled is False
