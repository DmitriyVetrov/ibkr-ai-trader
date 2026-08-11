"""Market data and option chain diagnostics, end to end.

The property under test in every case: the system either returns data whose
origin is honestly labelled, or it reports unavailability. It never silently
falls back from live broker data to a made-up value.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from trading_system.broker.factory import build_broker
from trading_system.cli import app
from trading_system.domain.enums import MarketDataOrigin, SecurityType, TradingMode
from trading_system.infrastructure.settings import Settings

runner = CliRunner()


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_market_data_diagnostic_passes_against_the_simulator() -> None:
    result = runner.invoke(app, ["test", "ibkr-market-data", "--symbol", "SPY", "--simulated"])

    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "Orders submitted: 0" in result.output


@pytest.mark.integration
def test_market_data_states_its_origin() -> None:
    """A reader must always be able to tell simulated data from broker data."""
    result = runner.invoke(app, ["test", "ibkr-market-data", "--symbol", "SPY", "--simulated"])
    assert "Data origin: SIMULATED" in result.output


@pytest.mark.integration
def test_unavailable_market_data_fails_without_inventing_a_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_system import cli as cli_module
    from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState

    broker = SimulatedBroker(SimulatedBrokerState(unquotable_symbols={"ZZZZ"}))
    monkeypatch.setattr(cli_module, "build_broker", lambda *a, **k: broker)

    result = runner.invoke(app, ["test", "ibkr-market-data", "--symbol", "ZZZZ"])

    assert result.exit_code == 1
    assert "MARKET_DATA_UNAVAILABLE" in result.output
    assert "PASS" not in result.output


@pytest.mark.integration
def test_simulated_quote_is_never_labelled_as_broker_data() -> None:
    settings = Settings(_env_file=None, trading_mode=TradingMode.DRY_RUN)
    broker = build_broker(settings)
    broker.connect()
    try:
        snapshot = broker.get_market_data("SPY", SecurityType.STOCK)
    finally:
        broker.disconnect()

    assert snapshot.origin is MarketDataOrigin.SIMULATED
    assert snapshot.origin not in {
        MarketDataOrigin.BROKER_REALTIME,
        MarketDataOrigin.BROKER_DELAYED,
        MarketDataOrigin.BROKER_FROZEN,
    }
    assert snapshot.source == "SIMULATOR"


# ---------------------------------------------------------------------------
# Option chain
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_option_chain_diagnostic_passes_against_the_simulator() -> None:
    result = runner.invoke(app, ["test", "ibkr-option-chain", "--symbol", "SPY", "--simulated"])

    assert result.exit_code == 0, result.output
    assert "Expirations" in result.output
    assert "Strikes" in result.output
    assert "Orders submitted: 0" in result.output


@pytest.mark.integration
def test_option_chain_selects_no_contract_and_recommends_nothing() -> None:
    """Proving connectivity is not the same as choosing a trade."""
    result = runner.invoke(app, ["test", "ibkr-option-chain", "--symbol", "SPY", "--simulated"])

    assert "No contract selected" in result.output
    for forbidden in ("BUY", "SELL", "recommend", "strategy"):
        assert forbidden not in result.output


@pytest.mark.integration
def test_option_chain_is_normalised_deterministically() -> None:
    settings = Settings(_env_file=None, trading_mode=TradingMode.DRY_RUN)
    broker = build_broker(settings)
    broker.connect()
    try:
        chain = broker.get_option_chain("SPY")
    finally:
        broker.disconnect()

    assert chain.expirations == sorted(set(chain.expirations))
    assert chain.strikes == sorted(set(chain.strikes))
    assert chain.rights


@pytest.mark.integration
def test_missing_chain_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_system import cli as cli_module
    from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState

    broker = SimulatedBroker(SimulatedBrokerState(chainless_symbols={"ZZZZ"}))
    monkeypatch.setattr(cli_module, "build_broker", lambda *a, **k: broker)

    result = runner.invoke(app, ["test", "ibkr-option-chain", "--symbol", "ZZZZ"])

    assert result.exit_code == 1
    assert "PASS" not in result.output


# ---------------------------------------------------------------------------
# Gateway-backed (skipped unless explicitly unlocked)
# ---------------------------------------------------------------------------
@pytest.mark.ibkr
@pytest.mark.integration
def test_real_market_data_is_labelled_with_its_true_origin() -> None:
    """Requires a running IB Gateway.

    Accepts unavailability as a valid outcome: without a market data
    subscription IBKR legitimately returns nothing, and reporting that is
    correct behaviour, not a failure of the adapter.
    """
    from trading_system.broker.base import MarketDataUnavailableError

    settings = Settings()
    assert settings.trading_mode is not TradingMode.LIVE, "refusing to run against LIVE"

    broker = build_broker(settings)
    try:
        broker.connect()
        try:
            snapshot = broker.get_market_data("SPY", SecurityType.STOCK)
        except MarketDataUnavailableError:
            pytest.skip("no market data subscription for SPY on this account")

        assert snapshot.origin in {
            MarketDataOrigin.BROKER_REALTIME,
            MarketDataOrigin.BROKER_DELAYED,
            MarketDataOrigin.BROKER_FROZEN,
        }
        assert snapshot.source == "IBKR"
        assert snapshot.has_quote
    finally:
        broker.disconnect()

    assert broker.orders_submitted == 0


@pytest.mark.ibkr
@pytest.mark.integration
def test_real_option_chain_can_be_normalised() -> None:
    """Requires a running IB Gateway."""
    settings = Settings()
    broker = build_broker(settings)
    try:
        broker.connect()
        chain = broker.get_option_chain("SPY")

        assert chain.underlying == "SPY"
        assert chain.expirations, "expected at least one expiration"
        assert chain.strikes, "expected at least one strike"
        assert chain.expirations == sorted(set(chain.expirations))
    finally:
        broker.disconnect()

    assert broker.orders_submitted == 0
