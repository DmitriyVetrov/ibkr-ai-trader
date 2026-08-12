"""The ``execution`` command group.

Every test here monkeypatches the service factory, so no command reaches the
repository's own ``data/`` directory and none constructs a real broker. In this
suite that matters more than anywhere else: a CLI test that reached a gateway
would not merely be slow, it would place an order.

The commands under test are the only ones in the system that can mutate broker
state, so what is asserted is mostly what they *refuse* to do: submit without
authorisation, submit while disabled, or say anything that reads as a fill when
the broker only acknowledged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_system import cli
from trading_system.execution.service import ExecutionService
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings

from .conftest import NOW

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def wired(tmp_path: Path, system_config, stub_repositories, monkeypatch, fake_broker):
    """Point the CLI at temporary stores and a controlled broker.

    Returns the broker so a test can assert on the count of orders it was
    actually asked to send — read off the object, never off the output.
    """
    broker = fake_broker()
    enabled = system_config.model_copy(
        update={"execution": system_config.execution.model_copy(update={"enabled": True})}
    )

    def _service() -> ExecutionService:
        return ExecutionService(
            settings=Settings(_env_file=None, trading_mode="PAPER"),
            config=enabled,
            clock=FixedClock(NOW),
            root=tmp_path,
            broker_factory=lambda *args, **kwargs: broker,
            **stub_repositories,
        )

    monkeypatch.setattr(cli, "_execution_service", _service)
    return broker


@pytest.fixture
def wired_disabled(tmp_path: Path, system_config, stub_repositories, monkeypatch, fake_broker):
    """The shipped configuration, which ships execution OFF."""
    broker = fake_broker()

    def _service() -> ExecutionService:
        return ExecutionService(
            settings=Settings(_env_file=None, trading_mode="PAPER"),
            config=system_config,
            clock=FixedClock(NOW),
            root=tmp_path,
            broker_factory=lambda *args, **kwargs: broker,
            **stub_repositories,
        )

    monkeypatch.setattr(cli, "_execution_service", _service)
    return broker


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def test_validate_prints_the_policy_and_submits_nothing(runner, wired_disabled) -> None:
    result = runner.invoke(cli.app, ["execution", "validate"])

    assert result.exit_code == 0, result.output
    assert "EXECUTION POLICY" in result.output
    assert wired_disabled.orders_submitted == 0


def test_validate_says_both_switches_are_needed(runner, wired_disabled) -> None:
    result = runner.invoke(cli.app, ["execution", "validate"])

    assert "execution.enabled" in result.output
    assert "--confirm" in result.output


def test_validate_states_that_no_model_is_involved(runner, wired_disabled) -> None:
    result = runner.invoke(cli.app, ["execution", "validate"])
    assert "none" in result.output and "deterministic" in result.output


# ---------------------------------------------------------------------------
# run: the two switches
# ---------------------------------------------------------------------------
def test_run_without_confirm_submits_nothing(runner, wired) -> None:
    """An allocation id on a command line is not permission to trade."""
    result = runner.invoke(cli.app, ["execution", "run"])

    assert wired.orders_submitted == 0
    assert "NOT_AUTHORIZED" in result.output or "--confirm" in result.output


def test_run_with_confirm_while_disabled_submits_nothing(runner, wired_disabled) -> None:
    result = runner.invoke(cli.app, ["execution", "run", "--confirm"])

    assert wired_disabled.orders_submitted == 0
    assert "EXECUTION_DISABLED" in result.output or "enabled" in result.output


def test_dry_run_and_confirm_together_are_refused(runner, wired) -> None:
    """They contradict each other, and guessing which was meant would be wrong."""
    result = runner.invoke(cli.app, ["execution", "run", "--dry-run", "--confirm"])

    assert result.exit_code != 0
    assert wired.orders_submitted == 0


# ---------------------------------------------------------------------------
# run: dry run
# ---------------------------------------------------------------------------
def test_dry_run_says_so_and_submits_nothing(runner, wired) -> None:
    result = runner.invoke(cli.app, ["execution", "run", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "EXECUTION DRY RUN" in result.output
    assert "NOT PERFORMED" in result.output
    assert wired.orders_submitted == 0


def test_dry_run_shows_what_would_be_submitted(runner, wired) -> None:
    result = runner.invoke(cli.app, ["execution", "run", "--dry-run"])

    assert "Would submit" in result.output
    assert "NVDA" in result.output


def test_dry_run_states_the_mode(runner, wired) -> None:
    result = runner.invoke(cli.app, ["execution", "run", "--dry-run"])
    assert "PAPER" in result.output


# ---------------------------------------------------------------------------
# run: submitting
# ---------------------------------------------------------------------------
def test_a_confirmed_run_submits_exactly_one_order(runner, wired) -> None:
    result = runner.invoke(cli.app, ["execution", "run", "--confirm"])

    assert result.exit_code == 0, result.output
    assert wired.orders_submitted == 1
    assert "EXECUTION — PAPER" in result.output


def test_the_output_never_claims_a_fill_for_an_acknowledgement(runner, wired) -> None:
    """The line an operator would misread as "we own it"."""
    result = runner.invoke(cli.app, ["execution", "run", "--confirm"])

    assert "SUBMITTED" in result.output
    assert "NOT a fill" in result.output


def test_the_order_count_is_read_off_the_broker(runner, wired) -> None:
    result = runner.invoke(cli.app, ["execution", "run", "--confirm"])

    assert "Orders submitted (read off the broker)" in result.output
    assert wired.orders_submitted == 1


def test_a_second_confirmed_run_submits_nothing_more(runner, wired) -> None:
    runner.invoke(cli.app, ["execution", "run", "--confirm"])
    assert wired.orders_submitted == 1

    result = runner.invoke(cli.app, ["execution", "run", "--confirm"])

    assert wired.orders_submitted == 1, "one authorisation, one order"
    assert "ALREADY_SUBMITTED" in result.output


# ---------------------------------------------------------------------------
# show, history, explain
# ---------------------------------------------------------------------------
def test_show_without_a_run_reports_honestly(runner, wired) -> None:
    result = runner.invoke(cli.app, ["execution", "show"])

    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.output
    assert wired.orders_submitted == 0


def test_show_renders_the_latest_run(runner, wired) -> None:
    runner.invoke(cli.app, ["execution", "run", "--confirm"])

    result = runner.invoke(cli.app, ["execution", "show"])

    assert result.exit_code == 0, result.output
    assert "Execution" in result.output


def test_history_lists_recorded_executions(runner, wired) -> None:
    runner.invoke(cli.app, ["execution", "run", "--confirm"])

    result = runner.invoke(cli.app, ["execution", "history"])

    assert result.exit_code == 0, result.output
    assert "Execution history" in result.output


def test_history_is_empty_before_anything_runs(runner, wired) -> None:
    result = runner.invoke(cli.app, ["execution", "history"])

    assert "No executions recorded" in result.output


def test_explain_shows_the_append_only_event_history(runner, wired, stub_repositories) -> None:
    run = runner.invoke(cli.app, ["execution", "run", "--confirm"])
    assert run.exit_code == 0, run.output
    # Read the id from the store rather than from the rendered table: Rich
    # truncates columns to the terminal width, and a test that parsed the
    # output would be testing the formatting.
    [entry] = stub_repositories["execution_repository"].history()

    result = runner.invoke(cli.app, ["execution", "explain", "--execution-id", entry.execution_id])

    assert result.exit_code == 0, result.output
    assert "Event history (append-only)" in result.output
    assert "appends, it does not edit" in result.output


def test_explain_of_an_unknown_execution_reports_honestly(runner, wired) -> None:
    result = runner.invoke(cli.app, ["execution", "explain", "--execution-id", "execution-nope"])

    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.output


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------
def test_cancel_requires_confirmation(runner, wired, stub_repositories) -> None:
    runner.invoke(cli.app, ["execution", "run", "--confirm"])
    [entry] = stub_repositories["execution_repository"].history()

    result = runner.invoke(cli.app, ["execution", "cancel", "--execution-id", entry.execution_id])

    assert result.exit_code != 0
    assert wired.cancelled == []


def test_cancel_with_confirmation_cancels(runner, wired, stub_repositories) -> None:
    runner.invoke(cli.app, ["execution", "run", "--confirm"])
    [entry] = stub_repositories["execution_repository"].history()

    result = runner.invoke(
        cli.app, ["execution", "cancel", "--execution-id", entry.execution_id, "--confirm"]
    )

    assert result.exit_code == 0, result.output
    assert wired.cancelled == ["fake-order-1"]
    assert wired.orders_submitted == 1, "cancelling must never submit"


def test_cancelling_something_not_live_is_refused(runner, wired) -> None:
    result = runner.invoke(
        cli.app, ["execution", "cancel", "--execution-id", "execution-nope", "--confirm"]
    )

    assert "UNAVAILABLE" in result.output


# ---------------------------------------------------------------------------
# Help text conventions
# ---------------------------------------------------------------------------
def test_the_group_help_marks_it_as_mutating() -> None:
    result = CliRunner().invoke(cli.app, ["execution", "--help"])

    assert result.exit_code == 0
    # Rich wraps the help text, so the phrase is matched on collapsed
    # whitespace rather than as it happens to be laid out.
    flattened = " ".join(result.output.split())
    assert "mutates broker state" in flattened
    assert "ONLY command group in this system that can place an order" in flattened


def test_the_run_command_help_is_explicit_about_what_it_does() -> None:
    result = CliRunner().invoke(cli.app, ["execution", "run", "--help"])

    assert "MUTATES BROKER STATE" in result.output


def test_no_generic_place_order_command_exists() -> None:
    """Brief section 40: the only order path is through an approved allocation."""
    result = CliRunner().invoke(cli.app, ["--help"])

    assert "place-order" not in result.output
    broker_help = CliRunner().invoke(cli.app, ["test", "--help"])
    assert "place-order" not in broker_help.output


def test_no_stray_execution_history_is_written_into_the_repository(repo_root: Path) -> None:
    """A CLI test that wrote into the checkout would leave a phantom order on file."""
    assert not (repo_root / "data" / "execution" / "history.jsonl").exists()
