"""Broker health checks.

A health check is what callers use to decide whether it is safe to proceed, so
it must always return a verdict rather than raising, and must distinguish "not
connected" from "cannot tell".
"""

from __future__ import annotations

import pytest

from trading_system.broker.ibkr import IBKRBroker
from trading_system.broker.simulator import SimulatedBroker
from trading_system.domain.enums import BrokerConnectionState, TradingMode
from trading_system.infrastructure.clock import FixedClock

from .conftest import BROKER_NOW
from .test_ibkr_adapter import FakeIB, connect_fake, make_broker


@pytest.mark.unit
def test_all_five_states_exist() -> None:
    """The specification requires these five to be distinguishable."""
    assert {s.value for s in BrokerConnectionState} == {
        "CONNECTED",
        "CONNECTING",
        "DISCONNECTED",
        "ERROR",
        "UNKNOWN",
    }


@pytest.mark.unit
def test_unknown_is_distinct_from_disconnected() -> None:
    """Not knowing the broker's state is itself a reason not to trade.

    These must stay separate members: collapsing "cannot tell" into "not
    connected" would let an unknown state read as a definite answer.
    """
    unsafe_states = {BrokerConnectionState.UNKNOWN, BrokerConnectionState.DISCONNECTED}
    assert len(unsafe_states) == 2


# ---------------------------------------------------------------------------
# Diagnostics content
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_health_reports_the_required_diagnostics(broker_clock: FixedClock) -> None:
    broker = connect_fake(make_broker(broker_clock), FakeIB())
    health = broker.health_check()

    assert health.broker == "IBKR"
    assert health.host == "127.0.0.1"
    assert health.port == 4002
    assert health.trading_mode is TradingMode.PAPER
    assert health.account_id == "DU1234567"
    assert health.read_only is True
    assert health.latency_ms is not None
    assert health.last_successful_communication is not None
    assert health.server_version == 176


@pytest.mark.unit
def test_health_never_exposes_credentials(broker_clock: FixedClock) -> None:
    """Health output is logged and pasted into issues; it must be safe to share."""
    broker = IBKRBroker(
        host="gw.internal",
        port=4002,
        client_id=1,
        account="DU1234567",
        clock=broker_clock,
    )
    connect_fake(broker, FakeIB())
    text = broker.health_check().model_dump_json()

    for secret in ("password", "hunter2", "TWS_PASSWORD", "username"):
        assert secret not in text.lower()


@pytest.mark.unit
def test_health_check_does_not_raise_when_the_library_errors(
    broker_clock: FixedClock,
) -> None:
    """A broken connection must yield ERROR, not an exception."""

    class ExplodingIB(FakeIB):
        def isConnected(self) -> bool:  # noqa: N802
            raise RuntimeError("socket is gone")

    broker = connect_fake(make_broker(broker_clock), ExplodingIB())
    health = broker.health_check()

    assert health.state is BrokerConnectionState.ERROR
    assert health.error is not None
    assert health.is_usable is False


@pytest.mark.unit
def test_health_reports_error_when_the_keepalive_fails(broker_clock: FixedClock) -> None:
    class StaleIB(FakeIB):
        def reqCurrentTime(self) -> None:  # noqa: N802
            raise RuntimeError("no response")

    broker = connect_fake(make_broker(broker_clock), StaleIB())
    health = broker.health_check()

    assert health.state is BrokerConnectionState.ERROR
    assert health.is_usable is False


@pytest.mark.unit
def test_health_reports_disconnected_after_the_socket_drops(
    broker_clock: FixedClock,
) -> None:
    ib = FakeIB()
    broker = connect_fake(make_broker(broker_clock), ib)
    assert broker.health_check().state is BrokerConnectionState.CONNECTED

    ib.connected = False
    assert broker.health_check().state is BrokerConnectionState.DISCONNECTED


@pytest.mark.unit
def test_health_before_connecting_is_disconnected(broker_clock: FixedClock) -> None:
    health = make_broker(broker_clock).health_check()
    assert health.state is BrokerConnectionState.DISCONNECTED
    assert health.account_id is None
    assert health.is_usable is False


@pytest.mark.unit
def test_is_usable_only_when_connected(broker_clock: FixedClock) -> None:
    connected = connect_fake(make_broker(broker_clock), FakeIB()).health_check()
    disconnected = make_broker(broker_clock).health_check()

    assert connected.is_usable is True
    assert disconnected.is_usable is False


@pytest.mark.unit
def test_simulator_health_is_timestamped_from_the_clock(
    simulated_broker: SimulatedBroker,
) -> None:
    assert simulated_broker.health_check().as_of == BROKER_NOW


@pytest.mark.unit
def test_health_check_submits_no_orders(broker_clock: FixedClock) -> None:
    broker = connect_fake(make_broker(broker_clock), FakeIB())
    broker.health_check()
    assert broker.orders_submitted == 0
