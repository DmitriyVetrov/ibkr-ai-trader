"""The simulated broker: deterministic, offline, and honestly labelled."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_system.broker.base import (
    BrokerConnectionError,
    MarketDataUnavailableError,
    OptionChainUnavailableError,
)
from trading_system.broker.simulator import (
    SimulatedBroker,
    SimulatedBrokerState,
    simulated_reference_price,
)
from trading_system.broker.simulator.market import monthly_expirations, simulated_strikes
from trading_system.domain.enums import (
    BrokerConnectionState,
    MarketDataOrigin,
    SecurityType,
    TradingMode,
)
from trading_system.infrastructure.clock import FixedClock

from .conftest import BROKER_NOW


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_reference_price_is_stable_for_a_symbol() -> None:
    """Derived from a SHA-256 digest, not hash(), which varies per process."""
    assert simulated_reference_price("SPY") == simulated_reference_price("SPY")
    assert simulated_reference_price("spy") == simulated_reference_price("SPY")


@pytest.mark.unit
def test_reference_prices_differ_between_symbols() -> None:
    assert simulated_reference_price("SPY") != simulated_reference_price("NVDA")


@pytest.mark.unit
def test_quotes_are_reproducible(broker_clock: FixedClock) -> None:
    first = SimulatedBroker(clock=broker_clock)
    second = SimulatedBroker(clock=broker_clock)
    first.connect()
    second.connect()
    assert first.get_market_data("SPY") == second.get_market_data("SPY")


@pytest.mark.unit
def test_option_chain_is_reproducible(broker_clock: FixedClock) -> None:
    first = SimulatedBroker(clock=broker_clock)
    second = SimulatedBroker(clock=broker_clock)
    first.connect()
    second.connect()
    assert first.get_option_chain("SPY") == second.get_option_chain("SPY")


@pytest.mark.unit
def test_expirations_are_third_fridays_in_the_future() -> None:
    reference = datetime(2026, 8, 10, tzinfo=UTC).date()
    expirations = monthly_expirations(reference, count=4)

    assert len(expirations) == 4
    assert expirations == sorted(expirations)
    for expiry in expirations:
        assert expiry > reference
        assert expiry.weekday() == 4, "third Friday expected"
        assert 15 <= expiry.day <= 21


@pytest.mark.unit
def test_strikes_are_sorted_unique_and_positive() -> None:
    strikes = simulated_strikes(Decimal("208.58"))
    assert strikes == sorted(set(strikes))
    assert all(s > 0 for s in strikes)


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_simulated_data_is_labelled_simulated(simulated_broker: SimulatedBroker) -> None:
    """Simulated numbers must never be mistakable for broker data."""
    quote = simulated_broker.get_market_data("SPY")
    assert quote.origin is MarketDataOrigin.SIMULATED
    assert quote.source == "SIMULATOR"

    chain = simulated_broker.get_option_chain("SPY")
    assert chain.origin is MarketDataOrigin.SIMULATED
    assert chain.source == "SIMULATOR"


@pytest.mark.unit
def test_broker_name_is_simulator(simulated_broker: SimulatedBroker) -> None:
    assert simulated_broker.name == "SIMULATOR"
    assert all(p.source == "SIMULATOR" for p in simulated_broker.get_positions())


@pytest.mark.unit
def test_default_mode_is_dry_run() -> None:
    assert SimulatedBroker().trading_mode is TradingMode.DRY_RUN


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_default_fixture_is_not_empty(simulated_broker: SimulatedBroker) -> None:
    """An all-empty fixture would let position-handling bugs pass unnoticed."""
    assert len(simulated_broker.get_positions()) == 2
    assert len(simulated_broker.get_open_orders()) == 1
    assert len(simulated_broker.get_executions()) == 1


@pytest.mark.unit
def test_fixture_covers_both_options_and_stock(simulated_broker: SimulatedBroker) -> None:
    types = {p.security_type for p in simulated_broker.get_positions()}
    assert types == {SecurityType.OPTION, SecurityType.STOCK}


@pytest.mark.unit
def test_option_position_carries_its_option_terms(simulated_broker: SimulatedBroker) -> None:
    option = next(
        p for p in simulated_broker.get_positions() if p.security_type is SecurityType.OPTION
    )
    assert option.expiration is not None
    assert option.strike is not None
    assert option.right is not None
    assert option.is_option


@pytest.mark.unit
def test_account_uses_decimal_money(simulated_broker: SimulatedBroker) -> None:
    account = simulated_broker.get_account()
    assert isinstance(account.net_liquidation, Decimal)
    assert isinstance(account.buying_power, Decimal)


@pytest.mark.unit
def test_reads_do_not_mutate_state(simulated_broker: SimulatedBroker) -> None:
    before = simulated_broker.get_positions()
    simulated_broker.get_account()
    simulated_broker.get_open_orders()
    simulated_broker.get_executions()
    assert simulated_broker.get_positions() == before


@pytest.mark.unit
def test_returned_lists_are_copies(simulated_broker: SimulatedBroker) -> None:
    """A caller mutating the returned list must not corrupt broker state."""
    positions = simulated_broker.get_positions()
    positions.clear()
    assert len(simulated_broker.get_positions()) == 2


@pytest.mark.unit
def test_custom_state_is_respected(broker_clock: FixedClock) -> None:
    broker = SimulatedBroker(SimulatedBrokerState(account_id="DU9999999"), clock=broker_clock)
    broker.connect()
    assert broker.get_account().account_id == "DU9999999"
    assert broker.get_positions() == []


# ---------------------------------------------------------------------------
# Connection and failure paths
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_reads_require_a_connection(broker_clock: FixedClock) -> None:
    broker = SimulatedBroker(clock=broker_clock)
    for read in (
        broker.get_account,
        broker.get_positions,
        broker.get_open_orders,
        broker.get_executions,
    ):
        with pytest.raises(BrokerConnectionError):
            read()


@pytest.mark.unit
def test_connection_failure_can_be_simulated(broker_clock: FixedClock) -> None:
    broker = SimulatedBroker(clock=broker_clock, fail_to_connect=True)
    with pytest.raises(BrokerConnectionError):
        broker.connect()
    assert not broker.is_connected


@pytest.mark.unit
def test_health_reports_disconnected_before_connecting(broker_clock: FixedClock) -> None:
    health = SimulatedBroker(clock=broker_clock).health_check()
    assert health.state is BrokerConnectionState.DISCONNECTED
    assert health.is_usable is False


@pytest.mark.unit
def test_health_reports_connected_after_connecting(simulated_broker: SimulatedBroker) -> None:
    health = simulated_broker.health_check()
    assert health.state is BrokerConnectionState.CONNECTED
    assert health.is_usable
    assert health.as_of == BROKER_NOW
    assert health.account_id == "DU0000000"


@pytest.mark.unit
def test_unavailable_market_data_raises_rather_than_inventing(
    broker_clock: FixedClock,
) -> None:
    state = SimulatedBrokerState(unquotable_symbols={"ZZZZ"})
    broker = SimulatedBroker(state, clock=broker_clock)
    broker.connect()
    with pytest.raises(MarketDataUnavailableError, match="MARKET_DATA_UNAVAILABLE"):
        broker.get_market_data("ZZZZ")


@pytest.mark.unit
def test_missing_option_chain_raises(broker_clock: FixedClock) -> None:
    state = SimulatedBrokerState(chainless_symbols={"ZZZZ"})
    broker = SimulatedBroker(state, clock=broker_clock)
    broker.connect()
    with pytest.raises(OptionChainUnavailableError):
        broker.get_option_chain("ZZZZ")
