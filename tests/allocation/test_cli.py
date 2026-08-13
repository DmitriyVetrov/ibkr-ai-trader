"""The ``risk`` and ``allocation`` command groups.

Every test here monkeypatches the service factory so no command reaches the
repository's own ``data/`` directory — a test that wrote allocation history
into the checkout would leave the next run reading capital nobody committed.
The one command that *does* touch a broker, ``risk capture-account``, is
exercised against the simulator and asserted to submit zero orders.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_system import cli
from trading_system.allocation.service import AllocationService
from trading_system.allocation.store import FilesystemAllocationRepository
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings
from trading_system.risk.store import FilesystemAccountSnapshotRepository
from trading_system.strategies.store import (
    FilesystemContractSelectionRepository,
    FilesystemStrategyRepository,
)

from .test_service import _account, _contract_run, _selection, _strategy_run

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
EXPIRATION = date(2026, 8, 31)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def wired(tmp_path: Path, system_config, monkeypatch):
    """Point the CLI at temporary stores, seeded with one priced candidate."""
    contracts = FilesystemContractSelectionRepository(tmp_path / "contracts")
    strategies = FilesystemStrategyRepository(tmp_path / "strategy")
    accounts = FilesystemAccountSnapshotRepository(tmp_path / "accounts")
    contracts.save(_contract_run(_selection("NVDA")))
    strategies.save(_strategy_run("NVDA"))
    accounts.save(_account())

    def _service() -> AllocationService:
        return AllocationService(
            settings=Settings(_env_file=None),
            config=system_config,
            clock=FixedClock(NOW),
            strategy_repository=strategies,
            contract_repository=contracts,
            allocation_repository=FilesystemAllocationRepository(tmp_path / "allocation"),
            account_repository=accounts,
            root=tmp_path,
        )

    monkeypatch.setattr(cli, "_allocation_service", _service)
    return tmp_path


# ---------------------------------------------------------------------------
# risk
# ---------------------------------------------------------------------------
def test_risk_validate_prints_the_limits_in_force(runner, wired):
    result = runner.invoke(cli.app, ["risk", "validate"])

    assert result.exit_code == 0
    assert "RISK LIMITS IN FORCE" in result.output
    assert "campaign_budget" in result.output
    assert "No orders were submitted" in result.output


def test_risk_validate_names_the_layer_that_owns_each_limit(runner, wired):
    result = runner.invoke(cli.app, ["risk", "validate"])

    assert "CAMPAIGN" in result.output
    assert "GLOBAL" in result.output


def test_risk_evaluate_persists_nothing(runner, wired, tmp_path):
    result = runner.invoke(cli.app, ["risk", "evaluate"])

    assert result.exit_code == 0
    assert "NVDA" in result.output
    assert FilesystemAllocationRepository(tmp_path / "allocation").history() == []


def test_risk_show_without_a_run_is_not_an_error(runner, wired):
    result = runner.invoke(cli.app, ["risk", "show"])

    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.output


def test_risk_explain_shows_every_check(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["risk", "explain", "--symbol", "NVDA"])

    assert result.exit_code == 0
    assert "Checks:" in result.output
    assert "No model wrote" in result.output


# ---------------------------------------------------------------------------
# allocation
# ---------------------------------------------------------------------------
def test_allocation_validate_reports_the_campaign(runner, wired):
    result = runner.invoke(cli.app, ["allocation", "validate"])

    assert result.exit_code == 0
    assert "CAMPAIGN AND ALLOCATION POLICY" in result.output
    assert "independent of the broker account balance" in " ".join(result.output.split())


def test_allocation_run_authorises_and_stores(runner, wired, tmp_path):
    result = runner.invoke(cli.app, ["allocation", "run"])

    assert result.exit_code == 0, result.output
    assert "APPROVED" in result.output
    assert "Orders submitted      : 0" in result.output
    assert len(FilesystemAllocationRepository(tmp_path / "allocation").history()) == 1


def test_allocation_run_dry_run_persists_nothing(runner, wired, tmp_path):
    result = runner.invoke(cli.app, ["allocation", "run", "--dry-run"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "no capital was reserved" in " ".join(result.output.split())
    assert FilesystemAllocationRepository(tmp_path / "allocation").history() == []


def test_allocation_run_reports_a_failure_honestly(runner, wired):
    """A second run authorises nothing, and says so rather than claiming success."""
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["allocation", "run"])

    assert result.exit_code == cli.EXIT_ERROR
    assert "NO_ALLOCATION" in result.output
    assert "not an entitlement" in " ".join(result.output.split())


def test_allocation_show_renders_the_stored_run(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["allocation", "show"])

    assert result.exit_code == 0
    assert "Allocation Run" in result.output
    assert "Campaign budget" in result.output


def test_allocation_explain_shows_the_quantity_arithmetic(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["allocation", "explain", "--symbol", "NVDA"])

    assert result.exit_code == 0
    assert "Units each ceiling permitted" in " ".join(result.output.split())
    assert "No model determined" in result.output


def test_allocation_history_lists_runs(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["allocation", "history"])

    assert result.exit_code == 0
    assert "Allocation runs" in result.output
    assert "ever rewritten" in result.output


def test_allocation_history_for_one_symbol(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["allocation", "history", "--symbol", "NVDA"])

    assert result.exit_code == 0
    assert "Allocation history — NVDA" in result.output


def test_allocation_validate_rechecks_a_stored_run(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])
    run_id = FilesystemAllocationRepository(wired / "allocation").history()[0].run_id

    result = runner.invoke(cli.app, ["allocation", "validate", "--run-id", run_id])

    assert result.exit_code == 0
    assert "accounting balances" in result.output


def test_an_unknown_run_id_is_not_an_error(runner, wired):
    result = runner.invoke(cli.app, ["allocation", "show", "--run-id", "nope"])

    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.output


def test_a_symbol_the_contract_run_did_not_cover_is_refused(runner, wired):
    result = runner.invoke(cli.app, ["allocation", "run", "--symbol", "TSLA"])

    assert result.exit_code == cli.EXIT_ERROR
    assert "CONFIGURATION_ERROR" in result.output


# ---------------------------------------------------------------------------
# the diagnostics
# ---------------------------------------------------------------------------
def test_test_allocation_inspects_rather_than_allocating(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["test", "allocation"])

    assert result.exit_code == 0
    assert "Orders submitted: 0" in result.output


def test_test_risk_inspects_rather_than_evaluating(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["test", "risk"])

    assert result.exit_code == 0
    assert "no order path" in result.output


# ---------------------------------------------------------------------------
# the one command that touches a broker
# ---------------------------------------------------------------------------
def test_capture_account_reads_the_simulator_and_submits_no_orders(runner, wired, tmp_path):
    result = runner.invoke(cli.app, ["risk", "capture-account", "--simulated"])

    assert result.exit_code == 0, result.output
    assert "Orders submitted: 0" in result.output
    assert "not the campaign budget" in " ".join(result.output.split())

    snapshots = FilesystemAccountSnapshotRepository(tmp_path / "accounts").history()
    assert len(snapshots) == 2, "the seeded snapshot plus the captured one"
    assert any(entry.simulated for entry in snapshots)


def test_capture_account_masks_the_account_number(runner, wired):
    result = runner.invoke(cli.app, ["risk", "capture-account", "--simulated"])

    assert "*" in result.output
    assert "DU0000000" not in result.output


def test_no_cli_test_leaves_history_in_the_repository(runner, wired, repo_root: Path):
    """The stray-file check every stage's CLI suite carries.

    A test that wrote into the checkout's own ``data/`` would leave the next
    run reading capital nobody committed.

    Stated as "these commands added nothing" rather than "this file does not
    exist": from Milestone 9 onwards a developer who has actually run
    ``risk capture-account`` or ``reconciliation run`` against their paper
    gateway has a legitimate ``data/accounts/history.jsonl``, and a test that
    failed because the CLI had been *used* would be measuring the wrong thing.
    """
    watched = ("allocation", "accounts")
    before = {name: _lines(repo_root / "data" / name / "history.jsonl") for name in watched}

    runner.invoke(cli.app, ["allocation", "run"])
    runner.invoke(cli.app, ["allocation", "show"])
    runner.invoke(cli.app, ["risk", "capture-account", "--simulated"])

    after = {name: _lines(repo_root / "data" / name / "history.jsonl") for name in watched}
    assert after == before


def _lines(path: Path) -> int:
    """How many history entries a store holds, or none at all."""
    if not path.exists():
        return -1
    return len(path.read_text(encoding="utf-8").splitlines())


def test_the_allocated_capital_is_decimal_exact_in_the_output(runner, wired):
    runner.invoke(cli.app, ["allocation", "run"])

    result = runner.invoke(cli.app, ["allocation", "show"])

    assert "1210.00" in result.output
    assert Decimal("1210.00") == Decimal("605.00") * 2
