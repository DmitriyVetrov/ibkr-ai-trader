"""Connection diagnostic, end to end through the CLI.

Two layers:

* **Simulator-backed** tests run always. They exercise the real command path —
  factory, connect, health check, disconnect — with no network.
* **``ibkr``-marked** tests need a running IB Gateway and are skipped unless
  ``ALLOW_LIVE_TESTS=true``. They never place an order.

Run the gateway-backed ones with::

    ALLOW_LIVE_TESTS=true pytest -m ibkr
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from trading_system.broker.factory import build_broker
from trading_system.domain.enums import BrokerConnectionState, TradingMode
from trading_system.infrastructure.settings import BrokerBackend, Settings

from trading_system.cli import app  # isort: skip

runner = CliRunner()


# ---------------------------------------------------------------------------
# Simulator-backed (always run)
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_connection_diagnostic_passes_against_the_simulator() -> None:
    result = runner.invoke(app, ["test", "ibkr-connection", "--simulated"])

    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "Orders submitted: 0" in result.output
    assert "CONNECTED" in result.output


@pytest.mark.integration
def test_connection_diagnostic_labels_its_access_level() -> None:
    result = runner.invoke(app, ["test", "ibkr-connection", "--simulated"])
    assert "READ-ONLY / SIMULATED" in result.output


@pytest.mark.integration
def test_connection_diagnostic_masks_the_account_number() -> None:
    result = runner.invoke(app, ["test", "ibkr-connection", "--simulated"])
    assert "DU0000000" not in result.output
    assert "*" in result.output


@pytest.mark.integration
def test_diagnostic_fails_when_the_broker_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable broker must FAIL, never fall back to fabricated output."""
    from trading_system import cli as cli_module
    from trading_system.broker.simulator import SimulatedBroker

    monkeypatch.setattr(
        cli_module,
        "build_broker",
        lambda *a, **k: SimulatedBroker(fail_to_connect=True),
    )
    result = runner.invoke(app, ["test", "ibkr-connection"])

    assert result.exit_code == 1
    assert "PASS" not in result.output


@pytest.mark.integration
def test_dry_run_mode_selects_the_simulator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "DRY_RUN")
    settings = Settings(_env_file=None)

    assert settings.resolved_broker_backend is BrokerBackend.SIMULATOR
    assert build_broker(settings).name == "SIMULATOR"


@pytest.mark.integration
def test_paper_mode_selects_ibkr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    settings = Settings(_env_file=None)

    assert settings.resolved_broker_backend is BrokerBackend.IBKR
    broker = build_broker(settings)
    assert broker.name == "IBKR"
    assert broker.read_only is True


@pytest.mark.integration
def test_factory_refuses_live_mode() -> None:
    from trading_system.broker.base import BrokerConfigurationError

    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.LIVE,
        live_trading_confirmed=True,
        live_readiness_checklist_signed_off=True,
    )
    with pytest.raises(BrokerConfigurationError, match="LIVE"):
        build_broker(settings)


@pytest.mark.integration
def test_factory_refuses_a_writable_ibkr_connection() -> None:
    from trading_system.broker.base import BrokerConfigurationError

    settings = Settings(_env_file=None, trading_mode=TradingMode.PAPER, ibkr_read_only=False)
    with pytest.raises(BrokerConfigurationError, match="Milestone 8"):
        build_broker(settings)


# ---------------------------------------------------------------------------
# Gateway-backed (skipped unless explicitly unlocked)
# ---------------------------------------------------------------------------
@pytest.mark.ibkr
@pytest.mark.integration
def test_real_gateway_connection_is_read_only_and_places_no_orders() -> None:
    """Requires a running IB Gateway on the configured paper port."""
    settings = Settings()
    assert settings.trading_mode is not TradingMode.LIVE, "refusing to run against LIVE"

    broker = build_broker(settings)
    try:
        health = broker.connect()

        assert health.state is BrokerConnectionState.CONNECTED
        assert health.read_only is True
        assert health.account_id, "account identity must be established"
        assert broker.get_account_summary()
    finally:
        broker.disconnect()

    assert broker.orders_submitted == 0
