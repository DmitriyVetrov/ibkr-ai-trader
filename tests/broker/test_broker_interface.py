"""The Broker contract, and the guarantees it makes about order submission."""

from __future__ import annotations

import inspect

import pytest

from trading_system.broker.base import (
    Broker,
    BrokerError,
    OrderSubmissionNotImplementedError,
    ReadOnlyBrokerError,
)
from trading_system.broker.ibkr import IBKRBroker
from trading_system.broker.simulator import SimulatedBroker
from trading_system.domain.enums import TradingMode

from .conftest import RecordingBroker

REQUIRED_METHODS = (
    "connect",
    "disconnect",
    "health_check",
    "get_account",
    "get_account_summary",
    "get_positions",
    "get_open_orders",
    "get_executions",
    "get_contract",
    "get_market_data",
    "get_option_chain",
    "place_order",
    "cancel_order",
)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("method", REQUIRED_METHODS)
def test_interface_declares_required_method(method: str) -> None:
    assert hasattr(Broker, method), f"Broker is missing {method}()"


@pytest.mark.unit
@pytest.mark.parametrize("implementation", [SimulatedBroker, IBKRBroker])
@pytest.mark.parametrize("method", REQUIRED_METHODS)
def test_implementations_provide_every_method(implementation: type, method: str) -> None:
    assert callable(getattr(implementation, method, None))


@pytest.mark.unit
def test_broker_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        Broker()  # type: ignore[abstract]


@pytest.mark.unit
@pytest.mark.parametrize("implementation", [SimulatedBroker, IBKRBroker])
def test_implementations_are_brokers(implementation: type) -> None:
    assert issubclass(implementation, Broker)


# ---------------------------------------------------------------------------
# Order submission is hard to reach
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_place_order_is_final() -> None:
    """A subclass must not be able to bypass the read-only check or the counter."""
    assert getattr(Broker.place_order, "__final__", False) is True
    assert getattr(Broker.cancel_order, "__final__", False) is True


@pytest.mark.unit
def test_no_implementation_overrides_place_order() -> None:
    for implementation in (SimulatedBroker, IBKRBroker, RecordingBroker):
        assert implementation.place_order is Broker.place_order
        assert implementation.cancel_order is Broker.cancel_order


@pytest.mark.unit
def test_brokers_are_read_only_by_default() -> None:
    assert SimulatedBroker().read_only is True
    assert inspect.signature(Broker.__init__).parameters["read_only"].default is True


@pytest.mark.unit
def test_read_only_broker_refuses_to_place_an_order(simulated_broker: SimulatedBroker) -> None:
    with pytest.raises(ReadOnlyBrokerError, match="read-only"):
        simulated_broker.place_order(None)  # type: ignore[arg-type]
    assert simulated_broker.orders_submitted == 0


@pytest.mark.unit
def test_read_only_refusal_survives_a_malformed_intent(
    simulated_broker: SimulatedBroker,
) -> None:
    """The refusal must not depend on the payload being well-formed."""
    for bad_intent in (None, object(), "not-an-intent", 42):
        with pytest.raises(ReadOnlyBrokerError):
            simulated_broker.place_order(bad_intent)  # type: ignore[arg-type]
    assert simulated_broker.orders_submitted == 0


@pytest.mark.unit
def test_read_only_broker_refuses_to_cancel(simulated_broker: SimulatedBroker) -> None:
    with pytest.raises(ReadOnlyBrokerError):
        simulated_broker.cancel_order("sim-order-1")


@pytest.mark.unit
def test_a_writable_simulator_can_execute_since_milestone_8() -> None:
    """Milestone 8 supplies the engine that Milestone 2 deliberately left absent.

    Clearing ``read_only`` is now sufficient *for the simulator*, which never
    leaves the process. Obtaining a writable **IBKR** broker still requires
    ``build_execution_broker``, PAPER mode and an explicit ``IBKR_READ_ONLY``
    — see ``tests/execution/test_boundaries.py``.
    """
    from trading_system.execution.models import EXECUTION_SCHEMA_VERSION

    assert EXECUTION_SCHEMA_VERSION  # the milestone that made this reachable

    broker = SimulatedBroker(read_only=False)
    broker.connect()

    assert broker.orders_submitted == 0
    assert broker.book.orders == {}


@pytest.mark.unit
def test_a_broker_with_no_engine_still_reports_not_implemented() -> None:
    """The default hook is unchanged: a broker with no execution path says so.

    Reported rather than fabricated — a simulated fill that looks real is
    exactly the output that makes an unfinished system appear to work.
    """
    broker = RecordingBroker()
    broker.connect()

    with pytest.raises(OrderSubmissionNotImplementedError, match="NOT_IMPLEMENTED"):
        broker.place_order(None)  # type: ignore[arg-type]


@pytest.mark.unit
def test_not_implemented_error_names_the_milestone_and_denies_sending() -> None:
    error = OrderSubmissionNotImplementedError("SIMULATOR")
    assert "NOT_IMPLEMENTED" in str(error)
    assert "Milestone 8" in str(error)
    assert "No order was sent" in str(error)


@pytest.mark.unit
def test_a_failed_submission_still_counts_as_an_attempt() -> None:
    """Deliberately the opposite of what Milestone 2 asserted, and a safety fix.

    The counter is incremented *before* ``_submit_order``. If a submission
    raises — a client timeout, a dropped socket — the order may already have
    reached the broker. Counting only successes would let exactly that case
    report zero submitted orders, which is the one circumstance in which a
    caller must **not** believe nothing was sent.

    Milestone 2 could safely count successes because no submission could ever
    succeed. Milestone 8 can, so the count means "attempted".
    """
    broker = RecordingBroker()
    broker.connect()

    with pytest.raises(OrderSubmissionNotImplementedError):
        broker.place_order(None)  # type: ignore[arg-type]

    assert broker.orders_submitted == 1
    assert broker.order_submission_count == 1


@pytest.mark.unit
def test_a_read_only_refusal_is_not_an_attempt() -> None:
    """The guard runs first, so nothing left the process and nothing is counted."""
    broker = RecordingBroker(read_only=True)
    broker.connect()

    with pytest.raises(ReadOnlyBrokerError):
        broker.place_order(None)  # type: ignore[arg-type]

    assert broker.orders_submitted == 0
    assert broker.order_submission_count == 0


@pytest.mark.unit
def test_every_broker_error_is_a_broker_error() -> None:
    for error in (ReadOnlyBrokerError("x"), OrderSubmissionNotImplementedError("SIM")):
        assert isinstance(error, BrokerError)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_context_manager_connects_and_disconnects() -> None:
    broker = SimulatedBroker()
    with broker as connected:
        assert connected.is_connected
    assert not broker.is_connected


@pytest.mark.unit
def test_disconnect_is_safe_when_never_connected() -> None:
    SimulatedBroker().disconnect()


@pytest.mark.unit
def test_disconnect_is_idempotent(simulated_broker: SimulatedBroker) -> None:
    simulated_broker.disconnect()
    simulated_broker.disconnect()
    assert not simulated_broker.is_connected


@pytest.mark.unit
def test_repr_does_not_leak_state_but_shows_safety_flags() -> None:
    text = repr(SimulatedBroker(trading_mode=TradingMode.PAPER))
    assert "read_only=True" in text
    assert "orders_submitted=0" in text
