"""The ``positions`` command group (brief section 68).

Two things are asserted throughout, and they are the reasons this group exists:

* every command distinguishes **BROKER OBSERVED** positions from **INTERNAL
  EXPECTED** positions, in the output, by name;
* every command reports zero submitted orders, read off the broker.

No test here reaches a real gateway: ``cli._services`` is replaced with one
wired to a temporary store and the in-process simulator. A test that reached a
gateway would be a bug in the test, and a stray write into the repository's own
``data/`` would be a bug in the suite.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.positions.factories import ACCOUNT, NOW, broker_execution, option_position
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    system_config: SystemConfig,
) -> Callable[..., SimulatedBroker]:
    """Point the CLI at a temporary store and an in-process broker."""
    from trading_system import cli
    from trading_system.reconciliation.service import ReconciliationService

    def install(state: SimulatedBrokerState | None = None) -> SimulatedBroker:
        clock = FixedClock(NOW)
        broker = SimulatedBroker(
            state
            if state is not None
            else SimulatedBrokerState(account_id=ACCOUNT, currency="EUR"),
            clock=clock,
            trading_mode=TradingMode.PAPER,
            read_only=True,
        )
        settings = Settings(_env_file=None, trading_mode="PAPER")
        service = ReconciliationService(
            settings=settings,
            config=system_config,
            clock=clock,
            broker_factory=lambda *a, **k: broker,
            root=tmp_path,
        )
        monkeypatch.setattr(cli, "_services", lambda simulated=False: (settings, service))
        return broker

    return install


def _app():
    from trading_system.cli import app

    return app


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------
def test_snapshot_reports_broker_observed_positions_and_zero_orders(wired) -> None:
    broker = wired(
        SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()])
    )
    result = runner.invoke(_app(), ["positions", "snapshot"])

    assert result.exit_code == 0, result.output
    assert "BROKER OBSERVED POSITIONS" in result.output
    assert "Orders submitted" in result.output
    assert broker.orders_submitted == 0


def test_snapshot_masks_the_account_number(wired) -> None:
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    result = runner.invoke(_app(), ["positions", "snapshot"])
    assert ACCOUNT not in result.output
    assert "4567" in result.output


def test_snapshot_of_an_empty_account_says_the_broker_answered(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["positions", "snapshot"])
    assert result.exit_code == 0, result.output
    assert "reported no positions" in result.output


def test_snapshot_fails_loudly_when_the_broker_cannot_be_read(wired, monkeypatch) -> None:
    """And says, in as many words, that this is not an empty account."""
    broker = wired()
    from trading_system.broker.base import BrokerConnectionError

    def refuse() -> None:
        raise BrokerConnectionError("gateway down")

    monkeypatch.setattr(broker, "get_positions", refuse)
    result = runner.invoke(_app(), ["positions", "snapshot"])

    assert result.exit_code == 1
    assert "not an empty account" in result.output.replace("\n", " ")


def test_a_dry_run_snapshot_stores_nothing(wired) -> None:
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    result = runner.invoke(_app(), ["positions", "snapshot", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output

    listed = runner.invoke(_app(), ["positions", "history"])
    assert "No position snapshots recorded." in listed.output


# ---------------------------------------------------------------------------
# show / history / explain
# ---------------------------------------------------------------------------
def test_show_without_a_snapshot_says_so_rather_than_inventing_one(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["positions", "show"])
    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.output


def test_show_labels_the_broker_observed_ledger(wired) -> None:
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    runner.invoke(_app(), ["positions", "snapshot"])
    result = runner.invoke(_app(), ["positions", "show"])
    assert "BROKER OBSERVED POSITIONS" in result.output


def test_show_expected_labels_the_internal_ledger(wired) -> None:
    """The two are never presented as one thing."""
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    runner.invoke(_app(), ["positions", "snapshot"])
    result = runner.invoke(_app(), ["positions", "show", "--expected"])
    assert "INTERNAL EXPECTED POSITIONS" in result.output
    assert "NOT broker reality" in result.output


def test_history_lists_captures_newest_first(wired) -> None:
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    runner.invoke(_app(), ["positions", "snapshot"])
    result = runner.invoke(_app(), ["positions", "history"])
    assert result.exit_code == 0
    assert "Broker position snapshots" in result.output


def test_explain_shows_both_ledgers_for_one_instrument(wired) -> None:
    wired(
        SimulatedBrokerState(
            account_id=ACCOUNT,
            currency="EUR",
            positions=[option_position()],
            executions=[broker_execution()],
        )
    )
    runner.invoke(_app(), ["positions", "snapshot"])
    result = runner.invoke(_app(), ["positions", "explain", "--contract-id", "100001"])

    assert result.exit_code == 0, result.output
    assert "INTERNAL EXPECTED" in result.output
    assert "BROKER OBSERVED" in result.output
    assert "RECORDED FILLS" in result.output


def test_explain_of_an_unheld_instrument_says_the_broker_holds_none(wired) -> None:
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    runner.invoke(_app(), ["positions", "snapshot"])
    result = runner.invoke(_app(), ["positions", "explain", "--contract-id", "999999"])
    assert "broker holds none" in result.output


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def test_validate_prints_the_policy_and_submits_nothing(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["positions", "validate"])
    assert result.exit_code == 0, result.output
    assert "POSITION LEDGER POLICY" in result.output
    assert "No orders were submitted" in result.output


def test_validate_states_that_only_a_confirmed_fill_makes_a_position(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["positions", "validate"])
    assert "CONFIRMED BROKER FILL" in result.output


# ---------------------------------------------------------------------------
# Nothing leaks into the repository's own data directory
# ---------------------------------------------------------------------------
def test_no_command_writes_into_the_repository_data_directory(wired, repo_root: Path) -> None:
    before = _tree(repo_root / "data")
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    for command in (
        ["positions", "snapshot"],
        ["positions", "show"],
        ["positions", "history"],
        ["positions", "validate"],
    ):
        runner.invoke(_app(), command)
    assert _tree(repo_root / "data") == before


def _tree(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}
