"""The ``reconciliation cleanup-orphans`` command.

Every test monkeypatches the service factory, so no command reaches the
repository's own ``data/`` directory and none constructs a real broker. In this
suite that matters more than anywhere else: a CLI test that reached a gateway
would not merely be slow, it would sell something.

What is asserted is mostly what the command *refuses* to do — run without
authorisation, run outside PAPER, or send a second order for a holding it has
already sold once.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from tests.cleanup.conftest import (
    ORPHAN_CALL_ID,
    ORPHAN_CALL_KEY,
    WiredCleanup,
    orphan_position,
)
from trading_system import cli
from trading_system.infrastructure.settings import SystemConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def flat(output: str) -> str:
    """One line with single spaces.

    Rich wraps to the terminal width, so a phrase this system prints as one
    sentence arrives split across lines. Asserting on the wrapped form would
    make these tests depend on a column count.
    """
    return " ".join(output.split())


@pytest.fixture
def wire(
    make_service: Callable[..., WiredCleanup], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., WiredCleanup]:
    """Point the CLI at a temporary store and an in-process broker.

    Returns the service so a test can assert on the count of orders the broker
    was actually asked to send — read off the object, never off the output.
    """

    def build(config: SystemConfig, positions=None, **kwargs: object) -> WiredCleanup:
        service = make_service(
            positions if positions is not None else [orphan_position()],
            config=config,
            **kwargs,
        )
        monkeypatch.setattr(cli, "_cleanup_service", lambda simulated: service)
        return service

    return build


# ---------------------------------------------------------------------------
# The review path
# ---------------------------------------------------------------------------
def test_without_confirm_the_command_is_a_review(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    service = wire(cleanup_enabled_config)

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans"])

    assert result.exit_code == 0, result.output
    assert "REVIEW" in result.output
    assert service.broker.orders_submitted == 0


def test_the_review_shows_the_targets_quantities_and_proposed_orders(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    wire(cleanup_enabled_config)

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans"])

    assert ORPHAN_CALL_KEY in result.output
    assert "TARGETS" in result.output
    assert "SAFETY GATES" in result.output
    assert "PROPOSED ORDERS" in result.output
    assert "SELL 1" in result.output
    assert "held (broker)" in result.output


def test_the_review_states_that_nothing_was_adopted(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    wire(cleanup_enabled_config)

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans"])

    assert "this system did not open this holding" in flat(result.output)
    assert "CLEANUP" in result.output


def test_the_review_reports_zero_orders_submitted(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    service = wire(cleanup_enabled_config)

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans"])

    assert "Orders submitted : 0" in flat(result.output)
    assert service.broker.orders_submitted == 0


def test_the_review_points_at_confirm_without_doing_it(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    wire(cleanup_enabled_config)

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans"])

    assert "--confirm" in result.output


def test_a_review_runs_even_while_the_switches_are_off(
    runner, wire, cleanup_disabled_config: SystemConfig
) -> None:
    """It shows what it would do, and says which gate is shut."""
    service = wire(cleanup_disabled_config)

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans"])

    assert result.exit_code == 0, result.output
    assert "FAIL CLEANUP_ENABLED" in flat(result.output)
    assert service.broker.orders_submitted == 0


# ---------------------------------------------------------------------------
# The authorised path
# ---------------------------------------------------------------------------
def test_confirm_prints_the_summary_before_submitting(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    service = wire(cleanup_enabled_config)
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans", "--confirm"])

    assert "ABOUT TO SUBMIT REAL PAPER ORDERS" in flat(result.output)
    assert "MODE : PAPER" in flat(result.output)
    assert "TARGET COUNT : 1" in flat(result.output)
    assert "LIVE : BLOCKED" in flat(result.output)
    assert "ACTION : CLOSE" in flat(result.output)


def test_confirm_closes_the_holding_and_says_so(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    service = wire(cleanup_enabled_config)
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans", "--confirm"])

    assert result.exit_code == 0, result.output
    assert service.broker.orders_submitted == 1
    assert "Orders submitted (read off the broker): 1" in flat(result.output)
    assert "CLOSED" in result.output


def test_a_disabled_configuration_refuses_even_with_confirm(
    runner, wire, cleanup_disabled_config: SystemConfig
) -> None:
    service = wire(cleanup_disabled_config)

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans", "--confirm"])

    assert service.broker.orders_submitted == 0
    assert result.exit_code != 0
    assert "FAIL CLEANUP_ENABLED" in flat(result.output)


def test_a_non_paper_mode_refuses(
    runner,
    make_service: Callable[..., WiredCleanup],
    monkeypatch: pytest.MonkeyPatch,
    cleanup_enabled_config: SystemConfig,
) -> None:
    from trading_system.infrastructure.settings import Settings

    service = make_service([orphan_position()], config=cleanup_enabled_config)
    service._settings = Settings(_env_file=None, trading_mode="DRY_RUN", ibkr_account="DU1234567")
    monkeypatch.setattr(cli, "_cleanup_service", lambda simulated: service)

    result = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans", "--confirm"])

    assert service.broker.orders_submitted == 0
    assert "FAIL TRADING_MODE_IS_PAPER" in flat(result.output)
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_a_second_confirmed_invocation_sends_no_second_order(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    service = wire(cleanup_enabled_config)
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    first = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans", "--confirm"])
    assert first.exit_code == 0, first.output
    assert service.broker.orders_submitted == 1

    second = runner.invoke(cli.app, ["reconciliation", "cleanup-orphans", "--confirm"])

    assert service.broker.orders_submitted == 1, "a second order was sent for a closed holding"
    assert "Nothing to close" in flat(second.output)
    assert second.exit_code == 0


# ---------------------------------------------------------------------------
# Narrowing
# ---------------------------------------------------------------------------
def test_contract_id_narrows_the_target_list(
    runner, wire, cleanup_enabled_config: SystemConfig
) -> None:
    from tests.cleanup.conftest import ORPHAN_PUT_ID
    from trading_system.domain.enums import OptionRight

    service = wire(
        cleanup_enabled_config,
        positions=[
            orphan_position(),
            orphan_position(
                contract_id=ORPHAN_PUT_ID, strike=Decimal("545.00"), right=OptionRight.PUT
            ),
        ],
    )
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    result = runner.invoke(
        cli.app,
        ["reconciliation", "cleanup-orphans", "--contract-id", str(ORPHAN_CALL_ID), "--confirm"],
    )

    assert result.exit_code == 0, result.output
    assert service.broker.orders_submitted == 1


# ---------------------------------------------------------------------------
# Ordinary reconciliation is untouched
# ---------------------------------------------------------------------------
def test_the_cleanup_command_is_not_part_of_reconciliation_run() -> None:
    """Normal reconciliation stays read-only and can never submit."""
    import inspect

    source = inspect.getsource(cli.reconciliation_run)
    assert "cleanup" not in source
    assert "confirm" not in source


def test_the_group_help_names_the_one_command_that_can_submit(runner) -> None:
    result = runner.invoke(cli.app, ["reconciliation", "--help"])

    assert "cleanup-orphans" in result.output
    assert "submits zero orders EXCEPT cleanup-orphans" in flat(result.output)
