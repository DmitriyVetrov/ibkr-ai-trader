"""The ``exit`` command group, and ``positions monitor``.

The output requirement is explicit: it must be impossible to mistake an
*evaluation* for an *execution*. Every command that only judges reports zero
submitted orders, and the one command that can place an order needs both of
Milestone 8's switches.

Every test here points the CLI at a temporary store. A test that wrote into the
repository's own ``data/`` — or reached a real gateway — is a bug in the test.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.execution.store import FilesystemExecutionRepository
from trading_system.exit.service import ExitService
from trading_system.exit.store import FilesystemExitRepository
from trading_system.exit.valuation import ExitQuoteReader
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.positions.service import PositionService
from trading_system.positions.store import FilesystemPositionRepository

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    system_config: SystemConfig,
    market_research_run,
) -> Callable[..., ExitService]:
    """Point the CLI's exit service at a temporary store.

    Monkeypatched rather than configured, exactly as the universe and
    reconciliation CLI suites do it: the alternative is a test that writes into
    the repository's own ``data/`` and leaves a trail behind.
    """
    from trading_system import cli
    from trading_system.research.store import FilesystemResearchRepository

    def install(
        *,
        quotes: bool = True,
        held: bool = True,
        bid: Decimal = Decimal("6.50"),
        config: SystemConfig | None = None,
    ) -> ExitService:
        resolved = config or system_config
        clock = FixedClock(NOW)
        data_root = tmp_path / "data"

        repository = factories.data_repository(data_root, clock=clock)
        if quotes:
            factories.store_quotes(
                repository,
                [factories.option_quote(bid=bid, ask=bid + Decimal("0.20"))],
            )
        FilesystemResearchRepository(data_root / "research").save(market_research_run)

        executions = FilesystemExecutionRepository(data_root / "execution")
        executions.save(
            factories.entry_execution(research_report_id=market_research_run.reports[0].report_id)
        )
        positions_store = FilesystemPositionRepository(data_root / "positions")
        positions_store.save_snapshot(factories.position_snapshot([] if not held else None))

        settings = Settings(_env_file=None, trading_mode="PAPER")
        service = ExitService(
            settings=settings,
            config=resolved,
            clock=clock,
            exit_repository=FilesystemExitRepository(data_root / "exit"),
            position_service=PositionService(
                settings=settings,
                config=resolved,
                clock=clock,
                position_repository=positions_store,
                execution_repository=executions,
                root=tmp_path,
            ),
            quote_reader=ExitQuoteReader(repository),
            root=tmp_path,
        )
        monkeypatch.setattr(cli, "_exit_service", lambda *args, **kwargs: service)
        return service

    return install


def _invoke(*args: str):
    from trading_system.cli import app

    return runner.invoke(app, list(args))


# ---------------------------------------------------------------------------
# Every command has help, and says what it does to the world
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    ["evaluate", "run", "show", "history", "validate", "explain"],
)
def test_every_exit_command_has_help(command: str) -> None:
    result = _invoke("exit", command, "--help")

    assert result.exit_code == 0
    assert command in result.stdout or "Usage" in result.stdout


def test_the_group_help_states_that_evaluation_never_submits() -> None:
    result = _invoke("exit", "--help")

    assert result.exit_code == 0
    assert "NEVER submits" in result.stdout


def test_positions_monitor_and_test_exit_have_help() -> None:
    for args in (("positions", "monitor", "--help"), ("test", "exit", "--help")):
        result = _invoke(*args)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Evaluation never submits
# ---------------------------------------------------------------------------
def test_exit_evaluate_submits_nothing(wired) -> None:
    wired()

    result = _invoke("exit", "evaluate")

    assert result.exit_code == 0
    assert "Orders submitted  : 0" in result.stdout


def test_exit_evaluate_records_the_verdict(wired) -> None:
    service = wired()

    result = _invoke("exit", "evaluate")

    assert result.exit_code == 0
    assert service.repository.history()


def test_a_dry_run_writes_nothing(wired) -> None:
    service = wired()

    result = _invoke("exit", "evaluate", "--dry-run")

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert service.repository.history() == []


def test_positions_monitor_submits_nothing(wired) -> None:
    wired()

    result = _invoke("positions", "monitor")

    assert result.exit_code == 0
    assert "Orders submitted  : 0" in result.stdout


def test_a_triggered_exit_is_reported_without_being_sent(wired) -> None:
    """An EXIT verdict from ``evaluate`` is a decision, not an order."""
    wired(bid=Decimal("2.00"))

    result = _invoke("exit", "evaluate")

    assert result.exit_code == 0
    assert "EXIT" in result.stdout
    assert "NOTHING was submitted" in result.stdout
    assert "Orders submitted  : 0" in result.stdout


# ---------------------------------------------------------------------------
# The one command that can place an order
# ---------------------------------------------------------------------------
def test_exit_run_without_confirm_sends_nothing(wired) -> None:
    wired(bid=Decimal("2.00"))

    result = _invoke("exit", "run")

    assert result.exit_code == 0
    assert "NOT AUTHORISED" in result.stdout
    assert "Orders submitted  : 0" in result.stdout


def test_confirm_and_dry_run_are_mutually_exclusive(wired) -> None:
    """One submits and the other structurally cannot; a command that accepted
    both would have to silently pick a winner."""
    wired()

    result = _invoke("exit", "run", "--confirm", "--dry-run")

    assert result.exit_code != 0
    assert "mutually exclusive" in (result.stdout + result.stderr)


def test_exit_run_dry_run_opens_no_broker(wired) -> None:
    wired(bid=Decimal("2.00"))

    result = _invoke("exit", "run", "--dry-run")

    assert result.exit_code == 0
    assert "Orders submitted  : 0" in result.stdout


def test_a_confirmed_run_is_still_refused_while_execution_is_disabled(
    wired,
) -> None:
    """Two switches, and neither implies the other. The shipped
    ``execution.enabled`` is false."""
    wired(bid=Decimal("2.00"))

    result = _invoke("exit", "run", "--confirm")

    assert result.exit_code == 0
    assert "EXECUTION_DISABLED" in result.stdout
    assert "Orders submitted  : 0" in result.stdout


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------
def test_exit_show_before_any_evaluation_says_so(wired) -> None:
    wired()

    result = _invoke("exit", "show")

    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.stdout


def test_exit_show_renders_the_latest_run(wired) -> None:
    wired()
    _invoke("exit", "evaluate")

    result = _invoke("exit", "show")

    assert result.exit_code == 0
    assert "EXIT RUN" in result.stdout


def test_exit_show_for_one_position_renders_its_decision(wired) -> None:
    service = wired()
    _invoke("exit", "evaluate")
    position_id = service.open_positions()[0].position_id

    result = _invoke("exit", "show", "--position-id", position_id, "--evaluation")

    assert result.exit_code == 0
    assert "EXIT DECISION" in result.stdout
    assert "POLICY OUTCOMES" in result.stdout


def test_an_unknown_position_is_reported_rather_than_invented(wired) -> None:
    wired()

    result = _invoke("exit", "show", "--position-id", "strategypos-does-not-exist")

    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.stdout


def test_exit_history_lists_recorded_judgements(wired) -> None:
    wired()
    _invoke("exit", "evaluate")

    result = _invoke("exit", "history")

    assert result.exit_code == 0
    assert "Exit evaluations" in result.stdout


def test_exit_explain_renders_the_lifecycle_and_the_trail(wired) -> None:
    service = wired()
    _invoke("exit", "evaluate")
    position_id = service.open_positions()[0].position_id

    result = _invoke("exit", "explain", "--position-id", position_id)

    assert result.exit_code == 0
    assert "POSITION LIFECYCLE" in result.stdout
    assert "TRAILING STOP" in result.stdout
    assert "LIFECYCLE HISTORY" in result.stdout


def test_exit_explain_for_an_unknown_position_says_so(wired) -> None:
    wired()

    result = _invoke("exit", "explain", "--position-id", "strategypos-nothing")

    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.stdout


# ---------------------------------------------------------------------------
# The policy in force
# ---------------------------------------------------------------------------
def test_exit_validate_prints_the_precedence_and_the_narrowing(wired) -> None:
    wired()

    result = _invoke("exit", "validate")

    assert result.exit_code == 0
    assert "Policy precedence" in result.stdout
    assert "Per-strategy narrowing" in result.stdout
    assert "PASS" in result.stdout


def test_exit_validate_states_that_no_model_is_involved(wired) -> None:
    wired()

    result = _invoke("exit", "validate")

    assert "none" in result.stdout
    assert "deterministic" in result.stdout


def test_exit_validate_states_that_an_unknown_exit_is_never_re_sent(
    wired,
) -> None:
    wired()

    result = _invoke("exit", "validate")

    assert "never re-sent" in result.stdout


# ---------------------------------------------------------------------------
# The diagnostic
# ---------------------------------------------------------------------------
def test_test_exit_evaluates_and_submits_nothing(wired) -> None:
    wired()

    result = _invoke("test", "exit")

    assert result.exit_code == 0
    assert "Orders submitted: 0" in result.stdout
    assert "PASS" in result.stdout
    assert "reached no broker" in result.stdout


def test_test_exit_lists_the_open_positions(wired) -> None:
    wired()

    result = _invoke("test", "exit")

    assert "Open positions     : 1" in result.stdout


# ---------------------------------------------------------------------------
# Nothing is written into the repository's own data directory
# ---------------------------------------------------------------------------
def test_no_command_writes_into_the_repositorys_own_data_directory(wired, repo_root: Path) -> None:
    """Asserts "this added nothing", not "this file does not exist": a
    developer who has actually run the CLI has a legitimate one."""
    history = repo_root / "data" / "exit" / "history.jsonl"
    before = history.read_bytes() if history.exists() else None
    wired()

    _invoke("exit", "evaluate")
    _invoke("positions", "monitor")
    _invoke("test", "exit")

    after = history.read_bytes() if history.exists() else None
    assert after == before
