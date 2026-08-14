"""Monitoring suites — what Milestone 10 delivered, and what is still pending.

Milestone 10 delivered the exit engine, the position lifecycle, the trailing
stop, the expiration policy and the thesis check. Those live in ``tests/exit``
and in ``tests/integration/test_exit_to_execution_to_reconciliation.py``, not
here — the suites are named for the packages they test, and the package is
``trading_system.exit``.

What is still absent is the **scheduler**: nothing yet runs
``positions monitor`` or ``reconciliation run`` on a recurring cadence, and
``config/schedules.yaml`` describes jobs no process executes. Milestone 10
deliberately built the callable operation rather than the loop that calls it —
:meth:`~trading_system.exit.service.ExitService.monitor` is safe to run
repeatedly, holds nothing in process memory, and is what a scheduler will
invoke.

Also absent, and deliberately: the specification's separate **Thesis Monitor**,
which returns ``VALID / WEAKENING / INVALIDATED / UNKNOWN``. Milestone 10
evaluates a stored thesis deterministically and never returns ``WEAKENING`` —
deciding a thesis has weakened without being falsified is a judgement, and this
milestone makes none.

This placeholder keeps the gap visible in the test report instead of silently
absent.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="The scheduler and the thesis monitor are delivered after Milestone 10")
def test_suite_pending() -> None:
    raise AssertionError("placeholder; never executed")


@pytest.mark.unit
def test_the_exit_operations_a_scheduler_will_call_exist() -> None:
    """The seam a scheduler needs, asserted now so it cannot be removed.

    Milestone 10's contribution to automation is a *callable operation* that is
    safe to run repeatedly: no process-local memory, an idempotent store, and a
    re-run over unchanged state that re-observes rather than deciding again.
    """
    from trading_system.exit.service import ExitService

    for operation in ("monitor", "evaluate", "confirm", "open_positions"):
        assert callable(getattr(ExitService, operation)), operation
