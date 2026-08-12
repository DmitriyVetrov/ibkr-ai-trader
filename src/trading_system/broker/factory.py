"""Construct the configured broker.

One place decides which implementation to build and with what safety flags, so
the CLI, the future scheduler and tests cannot drift into constructing brokers
with different guarantees.

**Two functions, and the difference between them is the order-submission
boundary.** :func:`build_broker` always returns a read-only connection — every
diagnostic, the data layer and every upstream stage gets one, and it refuses to
place an order no matter what is asked of it. :func:`build_execution_broker`
is the only way to obtain a writable one, it exists for the Milestone 8
execution service, and it refuses every mode except PAPER.

That split is what makes "arbitrary code cannot submit an order" structural: it
is not that upstream code declines to call ``place_order``, it is that the
broker upstream code holds would raise if it did.
"""

from __future__ import annotations

from trading_system.broker.base import Broker, BrokerConfigurationError
from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.settings import BrokerBackend, Settings

__all__ = ["build_broker", "build_execution_broker"]


def build_broker(
    settings: Settings,
    *,
    clock: Clock | None = None,
    backend: BrokerBackend | None = None,
) -> Broker:
    """Build the broker for the current settings.

    Args:
        settings: resolved runtime settings.
        clock: injected time source; defaults to wall clock.
        backend: overrides the setting-derived choice, for diagnostics that
            explicitly want the simulator.

    Raises:
        BrokerConfigurationError: if LIVE is requested, or if IBKR is selected
            with the read-only guard disabled.
    """
    clock = clock or SystemClock()
    chosen = backend or settings.resolved_broker_backend

    if settings.trading_mode is TradingMode.LIVE:
        raise BrokerConfigurationError(
            "LIVE trading is not available. Milestone 2 supports DRY_RUN and PAPER "
            "only; live readiness is Milestone 12."
        )

    if chosen is BrokerBackend.SIMULATOR:
        from trading_system.broker.simulator import SimulatedBroker

        return SimulatedBroker(
            clock=clock,
            trading_mode=settings.trading_mode,
            read_only=True,
        )

    from trading_system.broker.ibkr import IBKRBroker

    return IBKRBroker(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        account=settings.ibkr_account_id,
        trading_mode=settings.trading_mode,
        read_only=True,
        connect_timeout_seconds=settings.ibkr_connect_timeout_seconds,
        request_timeout_seconds=settings.ibkr_request_timeout_seconds,
        market_data_type=settings.ibkr_market_data_type,
        clock=clock,
    )


def build_execution_broker(
    settings: Settings,
    *,
    clock: Clock | None = None,
    backend: BrokerBackend | None = None,
) -> Broker:
    """Build a broker that is allowed to submit orders.

    The only writable broker constructor in the system, and every gate it
    applies is deliberate:

    * **LIVE is refused**, here as well as in the adapter and in the Milestone 1
      environment guards. Three independent refusals for the one irreversible
      action this system can take.
    * **PAPER is required for a real broker.** DRY_RUN gets a writable
      *simulator*, which never leaves the process; nothing else gets a writable
      IBKR connection.
    * **The read-only setting still binds.** ``IBKR_READ_ONLY=true`` — the
      shipped default — refuses execution outright, so an operator who has not
      deliberately opened the account for trading cannot be surprised by one.

    Args:
        settings: resolved runtime settings.
        clock: injected time source; defaults to wall clock.
        backend: overrides the setting-derived choice, for tests and dry runs
            that explicitly want the simulator.

    Raises:
        BrokerConfigurationError: for any mode or setting under which order
            submission is not permitted.
    """
    clock = clock or SystemClock()
    chosen = backend or settings.resolved_broker_backend

    if settings.trading_mode is TradingMode.LIVE:
        raise BrokerConfigurationError(
            "LIVE order submission is not available. Live readiness is Milestone 12 and "
            "requires a signed-off checklist; no execution path enables it."
        )

    if chosen is BrokerBackend.SIMULATOR:
        from trading_system.broker.simulator import SimulatedBroker

        return SimulatedBroker(
            clock=clock,
            trading_mode=settings.trading_mode,
            read_only=False,
        )

    if settings.trading_mode is not TradingMode.PAPER:
        raise BrokerConfigurationError(
            f"order submission through IBKR requires TRADING_MODE=PAPER, not "
            f"{settings.trading_mode.value}. Use the simulator for offline execution."
        )
    if settings.ibkr_read_only:
        raise BrokerConfigurationError(
            "IBKR_READ_ONLY=true, so this connection cannot submit orders. That is the "
            "shipped default and it is deliberate: set IBKR_READ_ONLY=false explicitly to "
            "open the paper account for trading."
        )

    from trading_system.broker.ibkr import IBKRBroker

    return IBKRBroker(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        account=settings.ibkr_account_id,
        trading_mode=settings.trading_mode,
        read_only=False,
        connect_timeout_seconds=settings.ibkr_connect_timeout_seconds,
        request_timeout_seconds=settings.ibkr_request_timeout_seconds,
        market_data_type=settings.ibkr_market_data_type,
        clock=clock,
    )
