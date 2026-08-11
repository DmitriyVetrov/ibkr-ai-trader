"""Construct the configured broker.

One place decides which implementation to build and with what safety flags, so
the CLI, the future scheduler and tests cannot drift into constructing brokers
with different guarantees.

Milestone 2 builds every broker read-only, regardless of settings.
"""

from __future__ import annotations

from trading_system.broker.base import Broker, BrokerConfigurationError
from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.settings import BrokerBackend, Settings

__all__ = ["build_broker"]


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

    if not settings.ibkr_read_only:
        raise BrokerConfigurationError(
            "IBKR_READ_ONLY=false is refused in Milestone 2. Order execution is "
            "delivered in Milestone 8."
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
