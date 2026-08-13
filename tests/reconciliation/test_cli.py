"""The ``reconciliation`` command group (brief sections 67, 95).

The output requirement is unusual and explicit: it must be **impossible to
mistake reconciliation for trading**. Every rendering states the submitted-order
count and the corrective-order count, both zero, and every recommendation says
``ACTION REQUIRED`` rather than naming a trade.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.positions.factories import (
    ACCOUNT,
    NOW,
    execution_record,
    option_position,
    stock_position,
)
from tests.reconciliation.conftest import WiredReconciliation
from tests.reservations.conftest import StubAllocationRepository, StubExecutionRepository
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import ExecutionState, TradingMode
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.positions.service import PositionService
from trading_system.reconciliation.service import ReconciliationService
from trading_system.reservations.service import ReservationService

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    system_config: SystemConfig,
    allocation_run,
) -> Callable[..., ReconciliationService]:
    """Point the CLI at a temporary store and the in-process simulator."""
    from trading_system import cli

    def install(state: SimulatedBrokerState | None = None) -> WiredReconciliation:
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
        executions = StubExecutionRepository(tmp_path / "data" / "execution")
        service = WiredReconciliation(
            settings=settings,
            config=system_config,
            clock=clock,
            position_service=PositionService(
                settings=settings,
                config=system_config,
                clock=clock,
                execution_repository=executions,
                broker_factory=lambda *a, **k: broker,
                root=tmp_path,
            ),
            reservation_service=ReservationService(
                settings=settings,
                config=system_config,
                clock=clock,
                allocation_repository=StubAllocationRepository([allocation_run]),
                execution_repository=executions,
                root=tmp_path,
            ),
            execution_repository=executions,
            root=tmp_path,
        )
        service.broker = broker
        service.executions = executions
        monkeypatch.setattr(cli, "_services", lambda simulated=False: (settings, service))
        return service

    return install


def _app():
    from trading_system.cli import app

    return app


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def test_run_reports_zero_submitted_and_zero_corrective_orders(wired) -> None:
    service = wired()
    result = runner.invoke(_app(), ["reconciliation", "run"])

    assert result.exit_code == 0, result.output
    assert "orders submitted  : 0" in result.output
    assert "corrective orders : 0" in result.output
    assert service.broker.orders_submitted == 0


def test_run_states_that_corrective_trading_is_not_possible(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["reconciliation", "run"])
    assert "not possible" in result.output
    assert "reports only" in result.output


def test_run_prints_the_shape_the_brief_asks_for(wired) -> None:
    """mode, account, both position counts, findings, and the two order counts."""
    wired(
        SimulatedBrokerState(
            account_id=ACCOUNT, currency="EUR", positions=[option_position(), stock_position()]
        )
    )
    result = runner.invoke(_app(), ["reconciliation", "run"])

    assert "RECONCILIATION" in result.output
    assert "mode    : PAPER" in result.output
    assert "broker positions" in result.output
    assert "internal expected positions" in result.output
    assert "ORPHAN_BROKER_POSITION" in result.output


def test_run_masks_the_account_number(wired) -> None:
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    result = runner.invoke(_app(), ["reconciliation", "run"])
    assert ACCOUNT not in result.output
    assert "4567" in result.output


def test_run_never_recommends_a_trade(wired) -> None:
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    result = runner.invoke(_app(), ["reconciliation", "run"])
    upper = result.output.upper()
    assert "AUTO-SELL" not in upper
    assert "AUTO-BUY" not in upper
    assert "ACTION REQUIRED" in upper


def test_run_fails_loudly_on_a_critical_finding(wired) -> None:
    service = wired(
        SimulatedBrokerState(
            account_id=ACCOUNT, currency="EUR", positions=[option_position()], open_orders=[]
        )
    )
    service.executions.seed(execution_record(state=ExecutionState.UNKNOWN, filled_quantity=0))

    result = runner.invoke(_app(), ["reconciliation", "run"])

    assert result.exit_code == 1
    assert "critical finding" in result.output
    assert "nothing was corrected automatically" in result.output.lower()


def test_a_dry_run_writes_nothing(wired) -> None:
    service = wired(
        SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()])
    )
    result = runner.invoke(_app(), ["reconciliation", "run", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert service.latest() is None


def test_the_reconcile_alias_runs_the_same_comparison(wired) -> None:
    """The specification names this command; it is the same code path."""
    service = wired()
    result = runner.invoke(_app(), ["reconcile"])

    assert result.exit_code == 0, result.output
    assert "RECONCILIATION" in result.output
    assert service.latest() is not None


# ---------------------------------------------------------------------------
# show / history / explain / validate
# ---------------------------------------------------------------------------
def test_show_without_a_run_says_so(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["reconciliation", "show"])
    assert result.exit_code == 0
    assert "UNAVAILABLE" in result.output


def test_show_renders_the_latest_comparison(wired) -> None:
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    runner.invoke(_app(), ["reconciliation", "run"])
    result = runner.invoke(_app(), ["reconciliation", "show"])
    assert "RECONCILIATION" in result.output
    assert "ORPHAN_BROKER_POSITION" in result.output


def test_history_lists_runs_with_their_order_counts(wired) -> None:
    wired()
    runner.invoke(_app(), ["reconciliation", "run"])
    result = runner.invoke(_app(), ["reconciliation", "history"])
    assert result.exit_code == 0
    assert "Reconciliation history" in result.output


def test_explain_shows_the_append_only_event_history(wired) -> None:
    wired()
    runner.invoke(_app(), ["reconciliation", "run"])
    result = runner.invoke(_app(), ["reconciliation", "explain"])

    assert result.exit_code == 0, result.output
    assert "Event history" in result.output
    assert "RECONCILIATION_STARTED" in result.output
    assert "Corrective orders" in result.output


def test_validate_prints_the_policy_and_its_refusals(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["reconciliation", "validate"])

    assert result.exit_code == 0, result.output
    assert "RECONCILIATION POLICY" in result.output
    assert "corrective orders permitted" in result.output
    assert "release on UNKNOWN" in result.output
    assert "No orders were submitted" in result.output


def test_validate_states_that_an_unknown_never_releases(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["reconciliation", "validate"])
    assert "no command that forces it" in result.output


def test_validate_can_recheck_a_stored_comparison(wired) -> None:
    service = wired()
    runner.invoke(_app(), ["reconciliation", "run"])
    stored = service.latest()
    assert stored is not None

    result = runner.invoke(
        _app(),
        ["reconciliation", "validate", "--reconciliation-id", stored.reconciliation_id],
    )
    assert result.exit_code == 0
    assert "RECONCILIATION" in result.output


# ---------------------------------------------------------------------------
# Nothing leaks into the repository's own data directory
# ---------------------------------------------------------------------------
def test_no_command_writes_into_the_repository_data_directory(wired, repo_root: Path) -> None:
    before = _tree(repo_root / "data")
    wired(SimulatedBrokerState(account_id=ACCOUNT, currency="EUR", positions=[option_position()]))
    for command in (
        ["reconciliation", "run"],
        ["reconciliation", "show"],
        ["reconciliation", "history"],
        ["reconciliation", "validate"],
        ["reconcile"],
    ):
        runner.invoke(_app(), command)
    assert _tree(repo_root / "data") == before


def _tree(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}
