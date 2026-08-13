"""The ``reservations`` command group (brief section 69).

What the output must make unmistakable:

* **committed** capital is not **available** capital;
* capital held because an execution is ``UNKNOWN`` is called out separately,
  with the reason, every time;
* ``release`` refuses an unresolved execution and there is no force-release.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.positions.factories import NOW, execution_record
from tests.reservations.conftest import StubAllocationRepository, StubExecutionRepository
from trading_system.domain.enums import ExecutionState
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.reservations.service import ReservationService

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    system_config: SystemConfig,
    allocation_run,
) -> Callable[..., ReservationService]:
    """Point the CLI's reservation service at a temporary store."""
    from trading_system import cli

    def install() -> ReservationService:
        service = ReservationService(
            settings=Settings(_env_file=None, trading_mode="PAPER"),
            config=system_config,
            clock=FixedClock(NOW),
            allocation_repository=StubAllocationRepository([allocation_run]),
            execution_repository=StubExecutionRepository(tmp_path / "data" / "execution"),
            root=tmp_path,
        )
        monkeypatch.setattr(cli, "_reservation_service", lambda: service)
        return service

    return install


def _app():
    from trading_system.cli import app

    return app


def test_show_reports_committed_and_available_separately(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["reservations", "show"])
    assert result.exit_code == 0, result.output
    assert "CAMPAIGN CAPITAL" in result.output
    assert "Committed" in result.output
    assert "Available" in result.output


def test_show_states_that_a_reservation_is_committed_not_invested(wired) -> None:
    wired()
    result = runner.invoke(_app(), ["reservations", "show"])
    assert "cannot be spent again" in result.output


def test_show_lists_each_reservation(wired) -> None:
    service = wired()
    result = runner.invoke(_app(), ["reservations", "show"])
    [held] = service.all()
    assert held.reservation_id in result.output


def test_show_one_reservation_in_full(wired) -> None:
    service = wired()
    service.sync()
    [held] = service.all()
    result = runner.invoke(
        _app(), ["reservations", "show", "--reservation-id", held.reservation_id]
    )
    assert result.exit_code == 0, result.output
    assert "Authorised" in result.output
    assert "Consumed" in result.output
    assert "Remaining" in result.output


def test_validate_shows_what_would_move_without_moving_it(wired) -> None:
    service = wired()
    service.sync()
    [held] = service.all()
    service._execution_repository.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.FAILED, filled_quantity=0
        )
    )

    result = runner.invoke(_app(), ["reservations", "validate"])

    assert result.exit_code == 0, result.output
    assert "Nothing was written" in result.output
    assert service.get(held.reservation_id).released_amount == 0


def test_release_requires_confirmation(wired) -> None:
    service = wired()
    service.sync()
    [held] = service.all()
    result = runner.invoke(
        _app(), ["reservations", "release", "--reservation-id", held.reservation_id]
    )
    assert result.exit_code == 1
    assert "--confirm" in result.output


def test_release_refuses_an_unknown_execution(wired) -> None:
    """The refusal that stops the campaign funding the same trade twice."""
    service = wired()
    service.sync()
    [held] = service.all()
    service._execution_repository.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.UNKNOWN, filled_quantity=0
        )
    )

    result = runner.invoke(
        _app(),
        ["reservations", "release", "--reservation-id", held.reservation_id, "--confirm"],
    )

    assert result.exit_code == 1
    assert "RELEASE_REFUSED_UNKNOWN" in result.output
    assert service.get(held.reservation_id).committed_amount == held.authorized_amount


def test_release_returns_capital_when_the_execution_provably_failed(wired) -> None:
    service = wired()
    service.sync()
    [held] = service.all()
    service._execution_repository.seed(
        execution_record(
            allocation_id=held.allocation_id, state=ExecutionState.FAILED, filled_quantity=0
        )
    )

    result = runner.invoke(
        _app(),
        ["reservations", "release", "--reservation-id", held.reservation_id, "--confirm"],
    )

    assert result.exit_code == 0, result.output
    assert service.get(held.reservation_id).released_amount == held.authorized_amount


def test_history_lists_reservations_and_their_events(wired) -> None:
    service = wired()
    service.sync()
    [held] = service.all()

    listed = runner.invoke(_app(), ["reservations", "history"])
    assert listed.exit_code == 0
    # Rich truncates long ids inside a table, so the assertion is on what a
    # reader would actually see rather than on the whole identifier.
    assert "Reservations" in listed.output
    assert held.symbol in listed.output

    events = runner.invoke(
        _app(), ["reservations", "history", "--reservation-id", held.reservation_id]
    )
    assert "No events recorded" in events.output


def test_the_cli_exposes_no_force_release() -> None:
    result = runner.invoke(_app(), ["reservations", "--help"])
    assert "force" not in result.output.lower()


def test_no_command_writes_into_the_repository_data_directory(wired, repo_root: Path) -> None:
    before = _tree(repo_root / "data")
    wired()
    for command in (
        ["reservations", "show"],
        ["reservations", "validate"],
        ["reservations", "history"],
    ):
        runner.invoke(_app(), command)
    assert _tree(repo_root / "data") == before


def _tree(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}
