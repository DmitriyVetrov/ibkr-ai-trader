"""Zero-order safety invariant (specification section 30, brief section 22).

Every read-only diagnostic must submit no orders. These tests run each command
against a broker that is deliberately *writable* and that records every
mutation attempt — so an empty record proves the command never even tried,
which a read-only broker could not distinguish from "tried and was blocked".

This is a security invariant, not a nicety. If one of these fails, a command
that claims to be read-only is capable of trading.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from trading_system import cli as cli_module
from trading_system.broker.ibkr.reconciliation import Reconciler
from trading_system.infrastructure.clock import FixedClock

from .conftest import RecordingBroker

runner = CliRunner()

#: Every command that claims to be read-only.
READ_ONLY_COMMANDS = [
    ["health", "--broker"],
    ["test", "ibkr-connection"],
    ["test", "ibkr-portfolio"],
    ["test", "ibkr-market-data", "--symbol", "SPY"],
    ["test", "ibkr-option-chain", "--symbol", "SPY"],
    ["test", "reconciliation"],
]


@pytest.fixture
def cli_recording_broker(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> RecordingBroker:
    """Force every CLI command to use a mutation-recording broker."""
    broker = RecordingBroker(clock=broker_clock)
    monkeypatch.setattr(cli_module, "build_broker", lambda *a, **k: broker)
    return broker


@pytest.mark.unit
@pytest.mark.parametrize("command", READ_ONLY_COMMANDS, ids=lambda c: " ".join(c))
def test_read_only_command_submits_no_orders(
    command: list[str], cli_recording_broker: RecordingBroker
) -> None:
    result = runner.invoke(cli_module.app, command)

    assert cli_recording_broker.order_submission_count == 0, (
        f"{' '.join(command)} attempted to place an order"
    )
    assert cli_recording_broker.mutation_attempts == [], (
        f"{' '.join(command)} attempted to mutate broker state: "
        f"{cli_recording_broker.mutation_attempts}"
    )
    assert cli_recording_broker.orders_submitted == 0
    assert result.exit_code == 0, f"{' '.join(command)} failed: {result.output}"


@pytest.mark.unit
@pytest.mark.parametrize("command", READ_ONLY_COMMANDS, ids=lambda c: " ".join(c))
def test_read_only_command_reports_the_zero_count(
    command: list[str], cli_recording_broker: RecordingBroker
) -> None:
    """The count must be printed, so a human can see the guarantee held."""
    result = runner.invoke(cli_module.app, command)
    assert "Orders submitted: 0" in result.output


@pytest.mark.unit
def test_health_without_broker_flag_never_touches_a_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain `health` must not open a connection at all."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("health must not build a broker without --broker")

    monkeypatch.setattr(cli_module, "build_broker", explode)
    assert runner.invoke(cli_module.app, ["health"]).exit_code == 0


@pytest.mark.unit
def test_reconciliation_submits_no_orders_directly(
    recording_broker: RecordingBroker, broker_clock: FixedClock
) -> None:
    """The reconciler reports discrepancies; it never trades to fix them."""
    report = Reconciler(broker_clock).reconcile(recording_broker)

    assert recording_broker.order_submission_count == 0
    assert recording_broker.mutation_attempts == []
    assert report.discrepancies, "expected unexplained broker state to be flagged"
    assert report.blocks_new_executions is True


@pytest.mark.unit
def test_every_read_only_command_is_covered() -> None:
    """Guard against a new diagnostic being added without a safety test.

    Compares the declared read-only commands against the `test` command group,
    so adding a diagnostic without listing it here fails loudly.
    """
    documented = {" ".join(command[1:]) for command in READ_ONLY_COMMANDS if command[0] == "test"}
    registered = {
        info.name
        for info in cli_module.test_app.registered_commands
        if info.name and info.name.startswith(("ibkr-", "reconcil"))
    }
    # Commands taking arguments appear here with their flags stripped.
    covered = {name.split(" ")[0] for name in documented}
    uncovered = registered - covered - {"ibkr-order-simulation"}
    assert not uncovered, f"read-only diagnostics without a zero-order test: {uncovered}"
