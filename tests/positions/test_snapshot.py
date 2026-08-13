"""Capturing broker position state (brief sections 12, 50-55, 73, 80).

The claims under test:

* one capture reads the broker once, over one short-lived connection, and
  submits zero orders;
* every failure mode produces a *distinct* recorded state, and none of them
  produces an empty portfolio;
* a snapshot is deterministic with respect to broker content, and observation
  clocks do not corrupt content identity.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.positions.factories import (
    ACCOUNT,
    NOW,
    broker_execution,
    option_position,
    stock_position,
)
from trading_system.broker.base import (
    BrokerConnectionError,
    BrokerResponseError,
    BrokerTimeoutError,
)
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import BrokerReadStatus, TradingMode
from trading_system.domain.models import BrokerExecution, BrokerPosition
from trading_system.infrastructure.clock import FixedClock
from trading_system.positions.snapshot import build_position_snapshot, snapshot_payload

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# One read, zero orders (brief sections 50-52, 70)
# ---------------------------------------------------------------------------
def test_a_capture_submits_no_orders(service, broker) -> None:
    capture = service.capture()
    assert capture.orders_submitted == 0
    assert broker.orders_submitted == 0
    assert capture.snapshot.orders_submitted == 0


def test_a_capture_reads_account_positions_orders_and_fills_from_one_connection(
    make_service, clock
) -> None:
    """All four are startup-cache backed, so one connection answers them all."""
    connection = _CountingBroker(
        _state(positions=[option_position()], executions=[broker_execution()]), clock=clock
    )
    service = make_service(connection)

    broker_state = service.read_broker_state()

    assert connection.connects == 1
    assert connection.disconnects == 1
    assert broker_state.account is not None
    assert broker_state.positions_status is BrokerReadStatus.OK
    assert broker_state.orders_status is BrokerReadStatus.EMPTY
    assert broker_state.executions_status is BrokerReadStatus.OK


def test_only_the_four_cache_backed_reads_are_issued(make_service, clock) -> None:
    """Nothing extra is asked of the connection.

    The Milestone 2 constraint: only the first uncached round trip on a TWS
    connection is reliably answered. Account summary, positions, open orders
    and fills all come from the startup handshake cache; anything else — a
    latency probe, a contract qualification, a quote — would spend the one
    reliable request on something that is not the point of the capture.
    """
    connection = _CountingBroker(_state(positions=[option_position()]), clock=clock)
    make_service(connection).read_broker_state()

    assert connection.calls == ["account", "positions", "open_orders", "executions"]


# ---------------------------------------------------------------------------
# The portfolio itself (brief section 73)
# ---------------------------------------------------------------------------
def test_an_empty_broker_portfolio_is_recorded_as_empty(service) -> None:
    capture = service.capture()
    assert capture.snapshot.read_status is BrokerReadStatus.EMPTY
    assert capture.snapshot.positions == []
    assert capture.snapshot.usable is True


def test_one_long_option_is_recorded_with_its_terms(make_service, clock) -> None:
    service = make_service(_broker([option_position()], clock))
    [position] = service.capture().snapshot.positions
    assert position.quantity == Decimal("2")
    assert position.right is not None
    assert position.strike == Decimal("180.00")
    assert position.average_cost == Decimal("595.00")
    assert position.market_value == Decimal("1250.00")
    assert position.unrealized_pnl == Decimal("60.00")


def test_multiple_positions_are_all_recorded(make_service, clock) -> None:
    service = make_service(_broker([option_position(), stock_position()], clock))
    snapshot = service.capture().snapshot
    assert len(snapshot.positions) == 2
    assert len(snapshot.option_positions) == 1


def test_positions_are_ordered_deterministically(make_service, clock) -> None:
    forwards = _broker([option_position(), stock_position()], clock)
    backwards = _broker([stock_position(), option_position()], clock)
    first = make_service(forwards).capture().snapshot
    second = make_service(backwards).capture().snapshot
    assert [p.key for p in first.positions] == [p.key for p in second.positions]
    assert first.content_hash == second.content_hash


def test_a_missing_optional_broker_field_stays_none(make_service, clock) -> None:
    service = make_service(
        _broker([option_position(market_value=None, unrealized_pnl=None)], clock)
    )
    [position] = service.capture().snapshot.positions
    assert position.market_value is None
    assert position.unrealized_pnl is None


# ---------------------------------------------------------------------------
# Failure modes (brief section 80)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BrokerConnectionError("gateway down"), BrokerReadStatus.UNAVAILABLE),
        (BrokerTimeoutError("no answer"), BrokerReadStatus.TIMEOUT),
        (BrokerResponseError("garbage"), BrokerReadStatus.UNAVAILABLE),
        (RuntimeError("unexpected"), BrokerReadStatus.MALFORMED),
    ],
)
def test_each_broker_failure_has_its_own_recorded_state(
    make_service, clock, error, expected
) -> None:
    service = make_service(_failing_broker(error, clock))
    capture = service.capture()
    assert capture.snapshot.read_status is expected
    assert capture.snapshot.usable is False


def test_a_broker_failure_is_never_recorded_as_an_empty_portfolio(make_service, clock) -> None:
    """The distinction the whole milestone rests on."""
    service = make_service(_failing_broker(BrokerConnectionError("gateway down"), clock))
    capture = service.capture()
    assert capture.snapshot.read_status is not BrokerReadStatus.EMPTY
    assert capture.snapshot.detail is not None


def test_a_broker_that_cannot_be_built_produces_an_unavailable_snapshot(make_service) -> None:
    def refuse(*args: object, **kwargs: object):
        from trading_system.broker.base import BrokerConfigurationError

        raise BrokerConfigurationError("no broker configured")

    service = make_service()
    service._broker_factory = refuse
    capture = service.capture()
    assert capture.snapshot.read_status is BrokerReadStatus.UNAVAILABLE
    assert capture.orders_submitted == 0


def test_a_failed_read_is_still_stored_as_evidence_that_we_looked(
    service, make_service, clock
) -> None:
    failing = make_service(_failing_broker(BrokerConnectionError("gateway down"), clock))
    capture = failing.capture()
    assert capture.stored is True
    assert failing.latest_snapshot() is not None
    assert failing.latest_usable_snapshot() is None


# ---------------------------------------------------------------------------
# Determinism and re-observation (brief sections 12, 37-38)
# ---------------------------------------------------------------------------
def test_the_same_holdings_produce_the_same_content_hash(make_service, clock) -> None:
    first = make_service(_broker([option_position()], clock)).capture().snapshot
    second = make_service(_broker([option_position()], clock)).capture().snapshot
    assert first.content_hash == second.content_hash
    assert first.snapshot_id == second.snapshot_id


def test_a_changed_quantity_produces_a_different_snapshot(make_service, clock) -> None:
    first = make_service(_broker([option_position()], clock)).capture().snapshot
    second = (
        make_service(_broker([option_position(quantity=Decimal("3"))], clock)).capture().snapshot
    )
    assert first.content_hash != second.content_hash


def test_the_content_hash_covers_holdings_not_their_valuation() -> None:
    """A snapshot answers 'what does this account hold', not 'at what mark'."""
    marked = build_position_snapshot(
        [option_position(market_value=Decimal("1250.00"))],
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
    )
    remarked = build_position_snapshot(
        [option_position(market_value=Decimal("1400.00"))],
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
    )
    assert marked.content_hash == remarked.content_hash


def test_the_payload_is_sorted_so_broker_ordering_cannot_change_identity() -> None:
    forwards = snapshot_payload(
        build_position_snapshot(
            [option_position(), stock_position()],
            broker="SIMULATOR",
            account_id=ACCOUNT,
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
        ).positions
    )
    backwards = snapshot_payload(
        build_position_snapshot(
            [stock_position(), option_position()],
            broker="SIMULATOR",
            account_id=ACCOUNT,
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
        ).positions
    )
    assert forwards == backwards


def test_capturing_twice_records_a_re_observation_rather_than_a_second_account_state(
    make_service, clock
) -> None:
    service = make_service(_broker([option_position()], clock))
    first = service.capture()
    second = service.capture()

    assert first.snapshot.snapshot_id == second.snapshot.snapshot_id
    history = service.repository.history()
    assert len(history) == 2
    assert sum(entry.reobserved for entry in history) == 1


def test_a_re_capture_records_no_second_fill(make_service, clock) -> None:
    state = SimulatedBrokerState(
        account_id=ACCOUNT,
        currency="EUR",
        positions=[option_position()],
        executions=[broker_execution()],
    )
    connection = SimulatedBroker(state, clock=clock, read_only=True)
    connection.connect()
    service = make_service(connection)

    first = service.capture()
    second = service.capture()

    assert len(first.recorded_fills) == 1
    assert second.recorded_fills == ()
    assert len(second.reobserved_fills) == 1
    assert len(service.fills.all()) == 1


def _broker(positions: list[BrokerPosition], clock: FixedClock) -> SimulatedBroker:
    state = SimulatedBrokerState(
        account_id=ACCOUNT, currency="EUR", positions=positions, open_orders=[], executions=[]
    )
    connection = SimulatedBroker(state, clock=clock, read_only=True)
    connection.connect()
    return connection


def _failing_broker(error: Exception, clock: FixedClock) -> SimulatedBroker:
    class Failing(SimulatedBroker):
        def get_positions(self):
            raise error

        def get_account(self):
            raise error

    connection = Failing(
        SimulatedBrokerState(account_id=ACCOUNT, currency="EUR"), clock=clock, read_only=True
    )
    connection.connect()
    return connection


def _state(
    *,
    positions: list[BrokerPosition] | None = None,
    executions: list[BrokerExecution] | None = None,
) -> SimulatedBrokerState:
    return SimulatedBrokerState(
        account_id=ACCOUNT,
        currency="EUR",
        positions=positions or [],
        open_orders=[],
        executions=executions or [],
    )


class _CountingBroker(SimulatedBroker):
    """Records exactly what a capture asks the broker to do, and in what order."""

    def __init__(self, state: SimulatedBrokerState, *, clock: FixedClock) -> None:
        super().__init__(state, clock=clock, read_only=True)
        self.connects = 0
        self.disconnects = 0
        self.calls: list[str] = []

    def connect(self):
        self.connects += 1
        return super().connect()

    def disconnect(self) -> None:
        self.disconnects += 1
        super().disconnect()

    def get_account(self):
        self.calls.append("account")
        return super().get_account()

    def get_positions(self):
        self.calls.append("positions")
        return super().get_positions()

    def get_open_orders(self):
        self.calls.append("open_orders")
        return super().get_open_orders()

    def get_executions(self):
        self.calls.append("executions")
        return super().get_executions()
