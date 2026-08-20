"""Broker failure modes (brief section 23).

Every external dependency fails eventually. The requirement for all of these is
the same: fail safe, fail loudly, and never substitute invented data. A broker
that cannot answer must produce an error, not a plausible-looking number.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from trading_system.broker import base as broker_base
from trading_system.broker.base import (
    BrokerAuthenticationError,
    BrokerConfigurationError,
    BrokerConnectionError,
    BrokerError,
    BrokerResponseError,
    BrokerTimeoutError,
    MarketDataUnavailableError,
    OptionChainUnavailableError,
)
from trading_system.broker.ibkr import client as client_module
from trading_system.broker.ibkr.reconciliation import Reconciler
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import ReconciliationStatus, SecurityType
from trading_system.infrastructure.clock import FixedClock

from .test_ibkr_adapter import FakeIB, connect_fake, make_broker


def fake_ib_module(connect_error: BaseException | None = None, ib: Any = None) -> SimpleNamespace:
    """A stand-in for the ``ib_async`` module whose connect() can fail."""
    instance: Any = ib if ib is not None else FakeIB()

    def connect(**kwargs: Any) -> None:
        if connect_error is not None:
            raise connect_error

    instance.connect = connect
    return SimpleNamespace(IB=lambda: instance, Contract=SimpleNamespace)


# ---------------------------------------------------------------------------
# Connection failures
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_connection_refused_is_reported_clearly(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> None:
    monkeypatch.setattr(
        client_module,
        "_import_ib_async",
        lambda: fake_ib_module(ConnectionRefusedError(111, "refused")),
    )
    broker = make_broker(broker_clock)

    with pytest.raises(BrokerConnectionError, match="refused the connection"):
        broker.connect()
    assert not broker.is_connected


@pytest.mark.unit
def test_timeout_is_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> None:
    monkeypatch.setattr(client_module, "_import_ib_async", lambda: fake_ib_module(TimeoutError()))
    broker = make_broker(broker_clock, connect_timeout_seconds=2.0)

    with pytest.raises(BrokerTimeoutError, match="did not respond"):
        broker.connect()


@pytest.mark.unit
def test_unreachable_host_is_a_connection_error(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> None:
    monkeypatch.setattr(
        client_module,
        "_import_ib_async",
        lambda: fake_ib_module(OSError("no route to host")),
    )
    broker = make_broker(broker_clock, host="10.255.255.1")

    with pytest.raises(BrokerConnectionError, match="could not reach"):
        broker.connect()


@pytest.mark.unit
def test_library_specific_failure_is_wrapped(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> None:
    """An unexpected library exception must still surface as a BrokerError."""
    monkeypatch.setattr(
        client_module,
        "_import_ib_async",
        lambda: fake_ib_module(RuntimeError("API version mismatch")),
    )

    with pytest.raises(BrokerConnectionError, match="IBKR connection failed"):
        make_broker(broker_clock).connect()


@pytest.mark.unit
def test_authentication_failure_closes_the_connection(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> None:
    """A gateway that is up but not logged in must not leave a half-open broker."""
    ib = FakeIB(accounts=[])
    monkeypatch.setattr(client_module, "_import_ib_async", lambda: fake_ib_module(ib=ib))
    broker = make_broker(broker_clock)

    with pytest.raises(BrokerAuthenticationError, match="no managed accounts"):
        broker.connect()
    assert not broker.is_connected
    assert broker.account_id is None


@pytest.mark.unit
def test_ambiguous_account_closes_the_connection(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> None:
    ib = FakeIB(accounts=["DU1", "DU2"])
    monkeypatch.setattr(client_module, "_import_ib_async", lambda: fake_ib_module(ib=ib))

    with pytest.raises(BrokerConfigurationError):
        make_broker(broker_clock).connect()


@pytest.mark.unit
def test_successful_connect_sets_the_market_data_type(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> None:
    ib = FakeIB()
    monkeypatch.setattr(client_module, "_import_ib_async", lambda: fake_ib_module(ib=ib))
    broker = make_broker(broker_clock, market_data_type=3)

    health = broker.connect()

    assert health.is_usable
    assert ib.market_data_type == 3
    assert broker.orders_submitted == 0


@pytest.mark.unit
def test_connect_does_not_spend_a_live_round_trip_on_its_own_health_check(
    monkeypatch: pytest.MonkeyPatch, broker_clock: FixedClock
) -> None:
    """``connect`` must not probe latency itself.

    Regression test: against a real TWS instance, only the first live
    request/response round trip on a freshly opened connection is reliably
    answered — a second one (e.g. a ``reqCurrentTime`` probe issued right
    after the connection handshake) can go unanswered forever even though
    the connection itself is healthy. ``connect`` used to call the full
    ``health_check`` (which issues that probe) on every connection, which
    burned the connection's one reliable round trip before the caller ever
    got to do real work. It must now report CONNECTED using only
    information already available from the handshake (``isConnected``,
    ``managedAccounts``, ``client.serverVersion``), leaving the live round
    trip for the caller.
    """

    class NoCurrentTimeIB(FakeIB):
        def reqCurrentTime(self) -> None:  # noqa: N802
            raise AssertionError("connect() must not call reqCurrentTime")

        async def reqCurrentTimeAsync(self) -> None:  # noqa: N802
            raise AssertionError("connect() must not call reqCurrentTimeAsync")

    ib = NoCurrentTimeIB()
    monkeypatch.setattr(client_module, "_import_ib_async", lambda: fake_ib_module(ib=ib))
    broker = make_broker(broker_clock)

    health = broker.connect()

    assert health.is_usable
    assert health.state.value == "CONNECTED"
    assert broker.orders_submitted == 0


@pytest.mark.unit
def test_missing_library_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing optional dependency must say how to install it."""
    import builtins

    real_import = builtins.__import__

    def fail_ib_async(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "ib_async":
            raise ImportError("No module named 'ib_async'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_ib_async)

    with pytest.raises(BrokerConfigurationError, match="ib_async is not installed"):
        client_module._import_ib_async()


# ---------------------------------------------------------------------------
# Failures mid-session
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_disconnect_mid_session_blocks_further_reads(broker_clock: FixedClock) -> None:
    ib = FakeIB()
    broker = connect_fake(make_broker(broker_clock), ib)
    assert broker.get_account_summary()

    ib.connected = False

    with pytest.raises(BrokerConnectionError, match="not connected"):
        broker.get_positions()


@pytest.mark.unit
def test_malformed_response_is_a_response_error(broker_clock: FixedClock) -> None:
    class GarbageIB(FakeIB):
        def accountSummary(self, account: str = "") -> list[Any]:  # noqa: N802
            raise RuntimeError("unparseable payload")

    broker = connect_fake(make_broker(broker_clock), GarbageIB())

    with pytest.raises(BrokerResponseError, match="account summary"):
        broker.get_account()


@pytest.mark.unit
def test_partial_response_keeps_missing_fields_none(broker_clock: FixedClock) -> None:
    """A partial answer is used as far as it goes, never padded with zeros."""
    from .test_ibkr_adapter import account_value

    broker = connect_fake(
        make_broker(broker_clock),
        FakeIB(summary=[account_value("NetLiquidation", "1000.00", "EUR")]),
    )
    account = broker.get_account()

    assert account.net_liquidation is not None
    assert account.buying_power is None
    assert account.maintenance_margin is None


@pytest.mark.unit
def test_missing_account_identity_is_refused(broker_clock: FixedClock) -> None:
    broker = make_broker(broker_clock)
    broker._ib = FakeIB()

    with pytest.raises(BrokerAuthenticationError, match="identity is unknown"):
        broker.get_account()


@pytest.mark.unit
def test_unresolvable_symbol_yields_no_quote(broker_clock: FixedClock) -> None:
    class NoContractIB(FakeIB):
        def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
            return []

    broker = connect_fake(make_broker(broker_clock), NoContractIB())

    with pytest.raises(MarketDataUnavailableError, match="MARKET_DATA_UNAVAILABLE"):
        broker.get_market_data("NOPE")


@pytest.mark.unit
def test_rejected_market_data_request_does_not_invent_a_price(
    broker_clock: FixedClock,
) -> None:
    class RejectingIB(FakeIB):
        def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
            return [SimpleNamespace(symbol="SPY", secType="STK", conId=1)]

        def reqMktData(self, *contracts: Any, **kwargs: Any) -> Any:  # noqa: N802
            raise RuntimeError("market data farm connection is broken")

    broker = connect_fake(make_broker(broker_clock), RejectingIB())

    with pytest.raises(MarketDataUnavailableError, match="MARKET_DATA_UNAVAILABLE"):
        broker.get_market_data("SPY")


@pytest.mark.unit
def test_all_nan_quote_is_reported_unavailable(broker_clock: FixedClock) -> None:
    """No subscription means no price — not a zero, and not the last close."""
    from .conftest import NAN, make_ticker

    class NanIB(FakeIB):
        def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
            return [SimpleNamespace(symbol="SPY", secType="STK", conId=1)]

        def reqMktData(self, *contracts: Any, **kwargs: Any) -> Any:  # noqa: N802
            return make_ticker(
                bid=NAN, ask=NAN, last=NAN, close=NAN, volume=NAN, average_daily_volume=NAN
            )

    broker = connect_fake(make_broker(broker_clock), NanIB())

    with pytest.raises(MarketDataUnavailableError, match="subscription"):
        broker.get_market_data("SPY")


@pytest.mark.unit
def test_missing_option_chain_is_reported(broker_clock: FixedClock) -> None:
    class NoChainIB(FakeIB):
        def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
            return [
                SimpleNamespace(
                    symbol="SPY",
                    secType="STK",
                    conId=1,
                    exchange="SMART",
                    primaryExchange="ARCA",
                    currency="USD",
                    localSymbol="SPY",
                    tradingClass="SPY",
                    multiplier="",
                    lastTradeDateOrContractMonth="",
                    strike=0.0,
                    right="",
                )
            ]

        def reqSecDefOptParams(self, *args: Any) -> list[Any]:  # noqa: N802
            return []

    broker = connect_fake(make_broker(broker_clock), NoChainIB())

    with pytest.raises(OptionChainUnavailableError, match="no option chain"):
        broker.get_option_chain("SPY")


# ---------------------------------------------------------------------------
# Fail-safe consequences
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_unavailable_broker_blocks_new_positions(broker_clock: FixedClock) -> None:
    """broker unavailable -> no trading action."""
    report = Reconciler(broker_clock).reconcile(SimulatedBroker(clock=broker_clock))

    assert report.status is ReconciliationStatus.BROKER_UNAVAILABLE
    assert report.blocks_new_executions is True


@pytest.mark.unit
def test_every_failure_is_catchable_as_one_type() -> None:
    """Callers can fail safe on BrokerError without enumerating subclasses."""
    for error_type in (
        BrokerConfigurationError,
        BrokerConnectionError,
        BrokerAuthenticationError,
        BrokerTimeoutError,
        BrokerResponseError,
        MarketDataUnavailableError,
        OptionChainUnavailableError,
    ):
        assert issubclass(error_type, BrokerError)


@pytest.mark.unit
def test_timeout_and_auth_errors_are_connection_errors() -> None:
    """Both mean "the broker is not usable", so both must be caught as such."""
    assert issubclass(BrokerTimeoutError, BrokerConnectionError)
    assert issubclass(BrokerAuthenticationError, BrokerConnectionError)


@pytest.mark.unit
def test_no_failure_path_increments_the_order_counter(broker_clock: FixedClock) -> None:
    state = SimulatedBrokerState(unquotable_symbols={"ZZZZ"}, chainless_symbols={"ZZZZ"})
    broker = SimulatedBroker(state, clock=broker_clock)
    broker.connect()

    for call in (
        lambda: broker.get_market_data("ZZZZ", SecurityType.STOCK),
        lambda: broker.get_option_chain("ZZZZ"),
    ):
        with pytest.raises(BrokerError):
            call()

    assert broker.orders_submitted == 0


@pytest.mark.unit
def test_base_module_exports_the_error_hierarchy() -> None:
    for name in (
        "BrokerError",
        "BrokerConnectionError",
        "BrokerTimeoutError",
        "MarketDataUnavailableError",
        "ReadOnlyBrokerError",
        "OrderSubmissionNotImplementedError",
    ):
        assert name in broker_base.__all__
