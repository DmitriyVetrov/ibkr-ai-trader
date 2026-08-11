"""Broker abstraction. Application code never calls a broker API directly.

Import the interface and errors from here; import a concrete implementation
only where one is actually constructed (the CLI and the broker factory).
``trading_system.broker.ibkr`` is the only package permitted to know that IBKR
exists.
"""

from trading_system.broker.base import (
    Broker,
    BrokerAuthenticationError,
    BrokerConfigurationError,
    BrokerConnectionError,
    BrokerError,
    BrokerResponseError,
    BrokerTimeoutError,
    MarketDataUnavailableError,
    OptionChainUnavailableError,
    OrderSubmissionNotImplementedError,
    ReadOnlyBrokerError,
)

__all__ = [
    "Broker",
    "BrokerAuthenticationError",
    "BrokerConfigurationError",
    "BrokerConnectionError",
    "BrokerError",
    "BrokerResponseError",
    "BrokerTimeoutError",
    "MarketDataUnavailableError",
    "OptionChainUnavailableError",
    "OrderSubmissionNotImplementedError",
    "ReadOnlyBrokerError",
]
