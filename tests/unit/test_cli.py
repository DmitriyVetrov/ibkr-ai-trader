"""CLI surface: it starts, it shows help, and it never fakes work it cannot do."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from trading_system.cli import EXIT_NOT_IMPLEMENTED, EXIT_OK, app

runner = CliRunner()

#: Command groups and commands the specification requires to be discoverable.
REQUIRED_TOP_LEVEL = (
    "run",
    "test",
    "data",
    "portfolio",
    "positions",
    "research",
    "opportunities",
    "reconcile",
    "reports",
    "health",
)

#: Jobs that must each be independently runnable (specification section 23).
REQUIRED_RUN_JOBS = (
    "universe",
    "research",
    "opportunities",
    "position-monitor",
    "thesis-monitor",
    "reconciliation",
    "data-collection",
    "end-of-day-report",
)

#: Diagnostics the specification names explicitly (sections 15, 30, 32, 33).
REQUIRED_TEST_COMMANDS = (
    "ibkr-connection",
    "ibkr-portfolio",
    "ibkr-market-data",
    "ibkr-option-chain",
    "ibkr-order-simulation",
    "e2e-dry-run",
    "e2e-paper",
    "allocation",
    "risk",
    "reconciliation",
    "strategy-selection",
    "contract-selection",
    "workflow",
)


def _text(result: object) -> str:
    """Combined output, tolerating Click versions that split stderr."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except ValueError:  # stderr not separately captured
        stderr = ""
    return stdout + stderr


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_help_starts_and_exits_cleanly() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == EXIT_OK
    assert "Usage" in _text(result)


@pytest.mark.unit
@pytest.mark.parametrize("command", REQUIRED_TOP_LEVEL)
def test_required_command_is_discoverable(command: str) -> None:
    result = runner.invoke(app, ["--help"])
    assert command in _text(result)


@pytest.mark.unit
@pytest.mark.parametrize("job", REQUIRED_RUN_JOBS)
def test_each_scheduled_job_is_runnable_from_the_cli(job: str) -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == EXIT_OK
    assert job in _text(result)


@pytest.mark.unit
@pytest.mark.parametrize("command", REQUIRED_TEST_COMMANDS)
def test_each_diagnostic_is_discoverable(command: str) -> None:
    result = runner.invoke(app, ["test", "--help"])
    assert result.exit_code == EXIT_OK
    assert command in _text(result)


@pytest.mark.unit
@pytest.mark.parametrize("group", ["run", "test", "data", "reports"])
def test_group_help_exits_cleanly(group: str) -> None:
    assert runner.invoke(app, [group, "--help"]).exit_code == EXIT_OK


@pytest.mark.unit
def test_commands_declare_their_blast_radius() -> None:
    """Every command says whether it can change state."""
    for args in (["--help"], ["run", "--help"], ["test", "--help"]):
        text = _text(runner.invoke(app, args))
        assert "read-only" in text or "mutates state" in text


# ---------------------------------------------------------------------------
# Commands that work today
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_version_reports_the_package_version() -> None:
    from trading_system import __version__

    result = runner.invoke(app, ["version"])
    assert result.exit_code == EXIT_OK
    assert __version__ in _text(result)


@pytest.mark.unit
def test_health_passes_without_touching_a_broker() -> None:
    result = runner.invoke(app, ["health"])
    assert result.exit_code == EXIT_OK
    text = _text(result)
    assert "PASS" in text
    assert "PAPER" in text


@pytest.mark.unit
def test_config_validates_and_prints() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == EXIT_OK
    assert "valid" in _text(result).lower()


# ---------------------------------------------------------------------------
# Commands that are honest about not existing yet
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "args",
    [
        ["portfolio"],
        ["opportunities"],
        ["run", "opportunities"],
        # `run thesis-monitor` is the one scheduled job Milestone 11 did NOT
        # build. Milestone 10 evaluates a stored thesis deterministically as
        # part of the exit policy and never returns WEAKENING; deciding that a
        # thesis has weakened without being falsified is a judgement no
        # milestone has made, and this command fabricates none.
        ["run", "thesis-monitor"],
        # `run position-monitor`, `run reconciliation`, `run pnl-settlement`,
        # `run operational-health`, `run exit-management` and
        # `run end-of-day-report` became real in Milestone 11: each invokes one
        # registered scheduler job, through the scheduler's own guards. They
        # are covered by tests/operations/, which roots every store at tmp_path
        # so no command touches the repository's own data/.
        # The IBKR diagnostics and `test reconciliation` landed in Milestone 2;
        # they are covered by tests/broker and tests/integration.
        ["test", "ibkr-order-simulation"],
        ["test", "e2e-dry-run"],
        ["test", "e2e-paper"],
        ["test", "workflow", "research"],
        # The `data` command group and `run data-collection` landed in
        # Milestone 3; they are covered by tests/data/test_data_cli.py.
        # The `universe` group and `run universe` landed in Milestone 4; they
        # are covered by tests/universe/test_cli.py, which repoints the service
        # at tmp_path so no command touches the repository's own data/.
        # The `research` group and `run research` landed in Milestone 5; they
        # are covered by tests/research/test_cli.py, which does the same. They
        # must NOT be invoked here: they now reach the real service, and a
        # non-dry run would write into the repository's own data/research/.
        # The `strategy` and `contract` groups landed in Milestone 6 and are
        # covered by tests/strategy/test_cli.py, for the same reason.
        # The `risk` and `allocation` groups, and `test risk` / `test
        # allocation`, landed in Milestone 7 and are covered by
        # tests/allocation/test_cli.py, which repoints the service at tmp_path
        # so no command touches the repository's own data/.
        # The `positions`, `reservations` and `reconciliation` groups, and the
        # top-level `reconcile` alias, landed in Milestone 9. They are covered
        # by tests/positions/test_cli.py, tests/reservations/test_cli.py and
        # tests/reconciliation/test_cli.py, each of which repoints the service
        # at tmp_path so no command touches the repository's own data/ — and
        # each of which asserts zero submitted and zero corrective orders.
        ["reports", "daily"],
        ["reports", "performance"],
    ],
    ids=lambda args: " ".join(args),
)
def test_unimplemented_commands_fail_loudly(args: list[str]) -> None:
    """A stub that prints plausible output is worse than no command at all."""
    result = runner.invoke(app, args)
    assert result.exit_code == EXIT_NOT_IMPLEMENTED, f"{args} -> {result.exit_code}"
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.unit
def test_unimplemented_command_names_its_milestone() -> None:
    result = runner.invoke(app, ["run", "opportunities"])
    text = _text(result)
    assert "NOT IMPLEMENTED" in text
    assert "Milestone 8" in text
    assert "No broker connection was attempted" in text


@pytest.mark.unit
def test_milestone_7_diagnostics_are_implemented() -> None:
    """``test risk`` and ``test allocation`` exited 3 before Milestone 7.

    They now inspect stored decisions. With no allocation run recorded they
    report UNAVAILABLE and exit 0 — honest about having nothing to show rather
    than pretending the command does not exist. Producing a run is covered by
    tests/allocation/test_cli.py, which points the service at a temporary
    store so nothing is written into the repository's own data/.
    """
    for args in (["test", "risk"], ["test", "allocation"]):
        result = runner.invoke(app, args)
        assert result.exit_code == EXIT_OK, f"{args} -> {result.output}"
        assert "NOT IMPLEMENTED" not in _text(result)


@pytest.mark.unit
def test_milestone_2_diagnostics_are_implemented() -> None:
    """These exited 3 in Milestone 1 and must not regress to a stub."""
    for args in (
        ["test", "ibkr-connection", "--simulated"],
        ["test", "ibkr-portfolio", "--simulated"],
        ["test", "ibkr-market-data", "--symbol", "SPY", "--simulated"],
        ["test", "ibkr-option-chain", "--symbol", "SPY", "--simulated"],
        ["test", "reconciliation", "--simulated"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == EXIT_OK, f"{args} -> {result.output}"
        assert "NOT IMPLEMENTED" not in _text(result)


@pytest.mark.unit
def test_milestone_4_universe_commands_are_implemented() -> None:
    """These exited 3 before Milestone 4 and must not regress to a stub.

    Only read-only commands are invoked here. `universe run` writes a run
    record, so it is exercised in tests/universe/test_cli.py against a
    temporary store — a unit test that wrote into the repository's own data/
    would be a bug in the test.
    """
    for args in (["universe", "--help"], ["universe", "validate"]):
        result = runner.invoke(app, args)
        assert result.exit_code == EXIT_OK, f"{args} -> {result.output}"
        assert "NOT IMPLEMENTED" not in _text(result)


@pytest.mark.unit
def test_milestone_6_strategy_commands_are_implemented() -> None:
    """These exited 3 before Milestone 6 and must not regress to a stub.

    Only read-only commands are invoked here. `strategy run` and
    `contract select` write run records, so they are exercised in
    tests/strategy/test_cli.py against temporary stores — a unit test that
    wrote into the repository's own data/ would be a bug in the test.
    """
    for args in (
        ["strategy", "--help"],
        ["contract", "--help"],
        ["contract", "validate"],
        ["test", "strategy-selection", "--ticker", "NVDA"],
        ["test", "contract-selection", "--ticker", "NVDA"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == EXIT_OK, f"{args} -> {result.output}"
        assert "NOT IMPLEMENTED" not in _text(result)


@pytest.mark.unit
def test_no_cli_test_leaves_a_universe_run_in_the_repository(repo_root) -> None:
    """A stray artifact means some test invoked a state-writing command for real."""
    assert not (repo_root / "data" / "universe" / "history.jsonl").exists()
    assert not (repo_root / "data" / "strategy" / "history.jsonl").exists()
    assert not (repo_root / "data" / "contracts" / "history.jsonl").exists()


@pytest.mark.unit
def test_unknown_command_is_a_usage_error() -> None:
    assert runner.invoke(app, ["definitely-not-a-command"]).exit_code == 2
