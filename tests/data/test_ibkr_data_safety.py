"""IBKR safety properties of the data layer.

Three things are being protected, all of them learned the hard way in
Milestone 2:

1. **One live round trip per connection.** Only the first uncached IBKR
   request on a freshly opened connection is reliably answered; a second can
   stay pending forever. The data layer therefore opens a one-purpose
   connection per retrieval, and the structure — not a convention — enforces
   it.
2. **Every request is bounded.** ``ib_async``'s synchronous wrappers wait
   forever by default, which turns the constraint above into a hung process.
3. **No data path can trade.** Read-only connections, zero orders, and an
   assertion after every session.

There is also a boundary test: the data layer must not contain a second IBKR
client. It reuses the validated Milestone 2 adapter or it does not talk to IBKR
at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trading_system.broker.base import (
    Broker,
    BrokerConfigurationError,
    BrokerTimeoutError,
    OrderSubmissionNotImplementedError,
    ReadOnlyBrokerError,
)
from trading_system.broker.ibkr import IBKRBroker
from trading_system.broker.simulator import SimulatedBroker
from trading_system.data.providers.broker_session import (
    BrokerSession,
    OrderSubmissionDetectedError,
)
from trading_system.data.providers.market import IBKRMarketDataProvider
from trading_system.data.providers.options import IBKROptionsDataProvider
from trading_system.domain.enums import SecurityType

pytestmark = pytest.mark.unit


class _CountingBroker(SimulatedBroker):
    """Records how often it was connected and what was asked of it."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.connects = 0
        self.disconnects = 0
        self.data_calls: list[str] = []

    def connect(self):
        self.connects += 1
        return super().connect()

    def disconnect(self) -> None:
        self.disconnects += 1
        super().disconnect()

    def get_market_data(self, symbol: str, security_type=SecurityType.STOCK):
        self.data_calls.append(f"market_data:{symbol}")
        return super().get_market_data(symbol, security_type)

    def get_option_chain(self, underlying: str):
        self.data_calls.append(f"chain:{underlying}")
        return super().get_option_chain(underlying)


# ---------------------------------------------------------------------------
# One live round trip per connection
# ---------------------------------------------------------------------------
def test_each_retrieval_gets_its_own_connection(data_clock) -> None:
    brokers: list[_CountingBroker] = []

    def _factory() -> Broker:
        broker = _CountingBroker(clock=data_clock)
        brokers.append(broker)
        return broker

    provider = IBKRMarketDataProvider(BrokerSession(_factory), clock=data_clock)
    provider.fetch_quote("SPY")
    provider.fetch_quote("QQQ")

    assert len(brokers) == 2
    for broker in brokers:
        assert broker.connects == 1
        assert len(broker.data_calls) == 1


def test_a_session_makes_exactly_one_broker_operation(data_clock) -> None:
    """The session runs one operation; there is no API for a second."""
    broker = _CountingBroker(clock=data_clock)
    session = BrokerSession(lambda: broker)

    session.fetch(lambda b: b.get_market_data("SPY"), description="quote")

    assert broker.data_calls == ["market_data:SPY"]
    assert broker.connects == 1
    assert broker.disconnects >= 1


def test_the_session_exposes_no_way_to_reuse_a_connection() -> None:
    """The constraint is structural, not a comment asking people to be careful."""
    public = {name for name in dir(BrokerSession) if not name.startswith("_")}
    assert public == {"fetch", "probe"}


def test_the_connection_is_closed_even_when_the_operation_fails(data_clock) -> None:
    from trading_system.broker.simulator import SimulatedBrokerState
    from trading_system.data.providers.base import ProviderUnavailableError

    broker = _CountingBroker(
        state=SimulatedBrokerState(unquotable_symbols={"ZZZZ"}), clock=data_clock
    )
    session = BrokerSession(lambda: broker)

    with pytest.raises(ProviderUnavailableError):
        session.fetch(lambda b: b.get_market_data("ZZZZ"), description="quote")

    # A leaked connection would take its client id with it and block the next.
    assert broker.disconnects >= 1
    assert not broker.is_connected


def test_a_chain_retrieval_also_uses_one_connection(data_clock) -> None:
    broker = _CountingBroker(clock=data_clock)
    provider = IBKROptionsDataProvider(BrokerSession(lambda: broker), clock=data_clock)

    provider.fetch_chain("SPY")

    assert broker.connects == 1
    assert broker.data_calls == ["chain:SPY"]


# ---------------------------------------------------------------------------
# Bounded requests
# ---------------------------------------------------------------------------
def test_the_broker_refuses_an_unbounded_request_timeout() -> None:
    """There is no "wait forever" setting on the adapter."""
    for bad in (0, -1):
        with pytest.raises(BrokerConfigurationError, match="request_timeout_seconds"):
            IBKRBroker(
                host="127.0.0.1",
                port=4002,
                client_id=1,
                request_timeout_seconds=bad,
            )


def test_the_request_timeout_is_configurable_and_positive() -> None:
    broker = IBKRBroker(host="127.0.0.1", port=4002, client_id=1, request_timeout_seconds=12.5)
    assert broker._request_timeout == 12.5


def test_settings_supply_a_positive_request_timeout() -> None:
    from trading_system.infrastructure.settings import Settings

    settings = Settings(_env_file=None)
    assert settings.ibkr_request_timeout_seconds > 0


def test_the_factory_passes_the_request_timeout_through(monkeypatch) -> None:
    from trading_system.broker import factory
    from trading_system.infrastructure.settings import BrokerBackend, Settings

    captured: dict[str, object] = {}

    class _Spy:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("trading_system.broker.ibkr.IBKRBroker", _Spy)
    settings = Settings(_env_file=None, ibkr_request_timeout_seconds=21.0)
    factory.build_broker(settings, backend=BrokerBackend.IBKR)

    assert captured["request_timeout_seconds"] == 21.0


def test_a_broker_timeout_surfaces_as_a_provider_timeout(data_clock) -> None:
    class _Timeout(SimulatedBroker):
        def get_market_data(self, symbol, security_type=SecurityType.STOCK):
            raise BrokerTimeoutError("IBKR did not answer")

    provider = IBKRMarketDataProvider(
        BrokerSession(lambda: _Timeout(clock=data_clock)), clock=data_clock
    )
    result = provider.fetch_quote("SPY")

    assert not result.succeeded
    assert "timed out" in (result.error or "")


def test_every_provider_declares_a_positive_timeout(data_clock) -> None:
    from trading_system.data.providers.market import SimulatedMarketDataProvider
    from trading_system.data.providers.options import SimulatedOptionsDataProvider

    providers = [
        IBKRMarketDataProvider(BrokerSession(lambda: SimulatedBroker()), clock=data_clock),
        IBKROptionsDataProvider(BrokerSession(lambda: SimulatedBroker()), clock=data_clock),
        SimulatedMarketDataProvider(clock=data_clock),
        SimulatedOptionsDataProvider(clock=data_clock),
    ]
    assert all(p.timeout_seconds > 0 for p in providers)


# ---------------------------------------------------------------------------
# No data path can trade
# ---------------------------------------------------------------------------
def test_a_data_retrieval_submits_zero_orders(data_clock) -> None:
    broker = _CountingBroker(clock=data_clock)
    IBKRMarketDataProvider(BrokerSession(lambda: broker), clock=data_clock).fetch_quote("SPY")

    assert broker.orders_submitted == 0


def test_a_session_that_somehow_submitted_an_order_fails_loudly(data_clock) -> None:
    """Should be impossible. Checked anyway, because the cost of not is a fill."""

    class _Sneaky(SimulatedBroker):
        def get_market_data(self, symbol, security_type=SecurityType.STOCK):
            self._orders_submitted += 1
            return super().get_market_data(symbol, security_type)

    session = BrokerSession(lambda: _Sneaky(clock=data_clock))

    with pytest.raises(OrderSubmissionDetectedError, match="must never mutate"):
        session.fetch(lambda b: b.get_market_data("SPY"), description="quote")


def test_the_brokers_the_data_layer_builds_are_read_only(data_clock) -> None:
    from trading_system.broker.factory import build_broker
    from trading_system.infrastructure.settings import BrokerBackend, Settings

    broker = build_broker(Settings(_env_file=None), backend=BrokerBackend.SIMULATOR)

    assert broker.read_only
    with pytest.raises(ReadOnlyBrokerError):
        broker.place_order(None)  # type: ignore[arg-type]


def test_order_submission_remains_unimplemented(data_clock) -> None:
    broker = SimulatedBroker(clock=data_clock, read_only=False)

    with pytest.raises(OrderSubmissionNotImplementedError):
        broker.place_order(None)  # type: ignore[arg-type]
    assert broker.orders_submitted == 0


# ---------------------------------------------------------------------------
# No second IBKR client
# ---------------------------------------------------------------------------
def _imported_modules(path: Path) -> set[str]:
    """Top-level modules a file actually imports.

    Parsed rather than grepped: ``ib_async`` appears in several docstrings that
    explain the boundary, and those must not count as violations of it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    return imported


def test_the_data_layer_does_not_import_ib_async(repo_root: Path) -> None:
    """One IBKR client exists in this system, and it is not in data/."""
    data_package = repo_root / "src" / "trading_system" / "data"
    offenders = [
        str(path.relative_to(repo_root))
        for path in data_package.rglob("*.py")
        if "ib_async" in _imported_modules(path)
    ]
    assert offenders == []


def test_the_data_layer_reuses_the_broker_abstraction(repo_root: Path) -> None:
    """The IBKR providers go through ``Broker``, not through a private client."""
    import inspect

    from trading_system.data.providers import broker_session

    source = inspect.getsource(broker_session)
    assert "from trading_system.broker.base import" in source
    assert "IB(" not in source
    assert "connectAsync" not in source


def test_no_module_outside_the_adapter_creates_an_ibkr_connection(repo_root: Path) -> None:
    data_package = repo_root / "src" / "trading_system" / "data"
    for path in data_package.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "reqTickers" not in source, path
        assert "reqSecDefOptParams" not in source, path
        assert "qualifyContracts" not in source, path
