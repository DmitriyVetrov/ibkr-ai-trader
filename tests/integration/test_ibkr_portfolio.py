"""Portfolio read diagnostic, end to end.

The portfolio command must report real broker state or fail. It must never
substitute fabricated balances or positions when the broker is unavailable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from typer.testing import CliRunner

from trading_system.broker.factory import build_broker
from trading_system.cli import app
from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.settings import Settings

runner = CliRunner()


# ---------------------------------------------------------------------------
# Simulator-backed (always run)
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_portfolio_reads_account_positions_orders_and_fills() -> None:
    result = runner.invoke(app, ["test", "ibkr-portfolio", "--simulated"])

    assert result.exit_code == 0, result.output
    for expected in ("Account", "Net liquidation", "Positions", "Open orders", "Executions"):
        assert expected in result.output
    assert "Orders submitted: 0" in result.output
    assert "PASS" in result.output


@pytest.mark.integration
def test_portfolio_shows_option_and_stock_positions() -> None:
    result = runner.invoke(app, ["test", "ibkr-portfolio", "--simulated"])
    assert "OPTION" in result.output
    assert "STOCK" in result.output


@pytest.mark.integration
def test_portfolio_distinguishes_unreported_from_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A balance the broker did not send must not print as 0."""
    from trading_system import cli as cli_module
    from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState

    state = SimulatedBrokerState()
    state.buying_power = None  # type: ignore[assignment]
    broker = SimulatedBroker(state)
    monkeypatch.setattr(cli_module, "build_broker", lambda *a, **k: broker)

    result = runner.invoke(app, ["test", "ibkr-portfolio"])

    assert "(not reported)" in result.output
    assert result.exit_code == 0


@pytest.mark.integration
def test_portfolio_fails_when_the_broker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_system import cli as cli_module
    from trading_system.broker.simulator import SimulatedBroker

    monkeypatch.setattr(
        cli_module,
        "build_broker",
        lambda *a, **k: SimulatedBroker(fail_to_connect=True),
    )
    result = runner.invoke(app, ["test", "ibkr-portfolio"])

    assert result.exit_code == 1
    assert "PASS" not in result.output
    # No fabricated balances anywhere in the output.
    assert "Net liquidation" not in result.output


@pytest.mark.integration
def test_portfolio_money_is_decimal_not_float() -> None:
    settings = Settings(_env_file=None, trading_mode=TradingMode.DRY_RUN)
    broker = build_broker(settings)
    broker.connect()
    try:
        account = broker.get_account()
        for value in (account.cash, account.net_liquidation, account.buying_power):
            assert isinstance(value, Decimal)
        for position in broker.get_positions():
            assert isinstance(position.quantity, Decimal)
    finally:
        broker.disconnect()

    assert broker.orders_submitted == 0


# ---------------------------------------------------------------------------
# Gateway-backed (skipped unless explicitly unlocked)
# ---------------------------------------------------------------------------
@pytest.mark.ibkr
@pytest.mark.integration
def test_real_portfolio_read_places_no_orders() -> None:
    """Requires a running IB Gateway. Reads only."""
    settings = Settings()
    assert settings.trading_mode is not TradingMode.LIVE, "refusing to run against LIVE"

    broker = build_broker(settings)
    try:
        broker.connect()
        account = broker.get_account()
        positions = broker.get_positions()
        orders = broker.get_open_orders()

        assert account.account_id
        assert isinstance(positions, list)
        assert isinstance(orders, list)
        for position in positions:
            assert position.source == "IBKR"
            assert isinstance(position.quantity, Decimal)
    finally:
        broker.disconnect()

    assert broker.orders_submitted == 0
