"""Broker abstraction (specification section 14).

The rest of the application talks to this interface and nothing else. No
module outside ``trading_system.broker.ibkr`` may import ``ib_async`` or touch
a raw IBKR object; the adapter translates broker responses into the domain
models in :mod:`trading_system.domain.models` before they cross this boundary.

Order submission is deliberately hard to reach. :meth:`Broker.place_order` is
final and cannot be overridden — a subclass implements ``_submit_order``
instead — and it refuses outright while the broker is read-only. Milestone 2
ships no ``_submit_order`` implementation at all, so every broker in the system
reports ``orders_submitted == 0`` and says ``NOT_IMPLEMENTED`` rather than
pretending to have executed anything.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import final

from trading_system.domain.enums import SecurityType, TradingMode
from trading_system.domain.models import (
    BrokerAccount,
    BrokerContract,
    BrokerExecution,
    BrokerHealth,
    BrokerOrder,
    BrokerPosition,
    ExecutionResult,
    MarketDataSnapshot,
    OptionChainSnapshot,
    OrderIntent,
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


# ---------------------------------------------------------------------------
# Errors
#
# Every failure mode is a distinct type so that callers can fail safe on the
# specific thing that went wrong instead of pattern-matching on message text.
# ---------------------------------------------------------------------------
class BrokerError(RuntimeError):
    """Base class for every broker failure."""


class BrokerConfigurationError(BrokerError):
    """Broker configuration is missing or invalid; do not attempt to connect."""


class BrokerConnectionError(BrokerError):
    """The broker could not be reached, or the connection dropped."""


class BrokerAuthenticationError(BrokerConnectionError):
    """The broker refused the credentials or the account is unavailable."""


class BrokerTimeoutError(BrokerConnectionError):
    """The broker did not answer within the configured timeout."""


class BrokerResponseError(BrokerError):
    """The broker answered, but the response was malformed or incomplete."""


class MarketDataUnavailableError(BrokerError):
    """No market data is available.

    Raised instead of returning a placeholder price. A missing subscription,
    a closed market or a rejected request must never turn into an invented
    quote (specification section 17).
    """


class OptionChainUnavailableError(BrokerError):
    """No option chain is available for the requested underlying."""


class ReadOnlyBrokerError(BrokerError):
    """An order was attempted against a broker opened in read-only mode."""


class OrderSubmissionNotImplementedError(BrokerError):
    """Order submission is not built yet.

    Milestone 2 is a read-only integration. The interface exists so later
    milestones have something to implement against; calling it now reports
    ``NOT_IMPLEMENTED`` rather than fabricating a successful execution.
    """

    def __init__(self, broker: str) -> None:
        self.broker = broker
        super().__init__(
            f"NOT_IMPLEMENTED: {broker} cannot submit orders. Order execution is "
            f"delivered in Milestone 8. No order was sent."
        )


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class Broker(ABC):
    """A broker connection.

    Implementations must translate every broker-native object into a domain
    model before returning it, and must raise rather than return placeholder
    data when the broker cannot answer.
    """

    def __init__(
        self,
        *,
        trading_mode: TradingMode = TradingMode.PAPER,
        read_only: bool = True,
    ) -> None:
        self._trading_mode = trading_mode
        self._read_only = read_only
        self._orders_submitted = 0

    # --- identity ----------------------------------------------------------
    @property
    @abstractmethod
    def name(self) -> str:
        """Short broker identifier, recorded as ``source`` on every artifact."""

    @property
    def trading_mode(self) -> TradingMode:
        return self._trading_mode

    @property
    def read_only(self) -> bool:
        """Whether this connection refuses to mutate anything."""
        return self._read_only

    @property
    def orders_submitted(self) -> int:
        """How many orders this broker has *attempted* to submit.

        Read-only diagnostics assert this is zero (specification section 30).
        The counter lives on the base class and is incremented in exactly one
        place, so a subclass cannot submit an order without it being counted.

        "Attempted" rather than "succeeded" is the load-bearing word, and the
        increment happens *before* the submission for that reason — see
        :meth:`place_order`.
        """
        return self._orders_submitted

    # --- connection --------------------------------------------------------
    @abstractmethod
    def connect(self) -> BrokerHealth:
        """Establish the connection. Raises :class:`BrokerConnectionError` on failure."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection. Must be safe to call when not connected."""

    @abstractmethod
    def health_check(self) -> BrokerHealth:
        """Report connection state without raising.

        Never raises: an unreachable broker is a result (``DISCONNECTED`` or
        ``ERROR``), not an exception, because the health check is what callers
        use to decide whether to proceed.
        """

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    # --- read-only state ---------------------------------------------------
    @abstractmethod
    def get_account(self) -> BrokerAccount:
        """Account identity and balances."""

    @abstractmethod
    def get_account_summary(self) -> dict[str, str]:
        """Raw account tag/value pairs exactly as the broker reported them."""

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def get_open_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    def get_executions(self) -> list[BrokerExecution]: ...

    @abstractmethod
    def get_contract(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
        *,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> BrokerContract:
        """Resolve a symbol to a broker contract."""

    @abstractmethod
    def get_market_data(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
    ) -> MarketDataSnapshot:
        """Fetch a quote.

        Returns a snapshot whose ``origin`` records where the numbers came
        from. Raises :class:`MarketDataUnavailableError` rather than inventing
        a price when the broker cannot supply one.
        """

    @abstractmethod
    def get_option_chain(self, underlying: str) -> OptionChainSnapshot:
        """Fetch and normalise the option chain metadata for an underlying.

        Selecting a contract is not this method's job — that is the
        deterministic contract selector in Milestone 6.
        """

    # --- mutation ----------------------------------------------------------
    #
    # The only way an order leaves this system. Reaching it requires a broker
    # that was deliberately constructed writable, which only
    # ``broker.factory.build_execution_broker`` does — ``build_broker`` always
    # returns a read-only connection, so the diagnostics, the data layer and
    # every upstream stage hold a broker that refuses here regardless of what
    # they ask for.
    @final
    def place_order(self, intent: OrderIntent) -> ExecutionResult:
        """Submit an order.

        Final by design: the read-only check and the submitted-order counter
        must not be bypassable by a subclass. Implementations override
        :meth:`_submit_order` instead. Milestone 8 supplies those
        implementations; do not un-final this to make execution "easier".

        The counter is incremented **before** the submission, and that ordering
        is a safety property rather than a detail. If ``_submit_order`` raises —
        a client timeout, a dropped socket — the order may already have reached
        the broker. Counting only successes would let exactly that case report
        zero submitted orders, which is the one circumstance in which a caller
        must *not* believe nothing was sent. An attempt counts.
        """
        if self._read_only:
            # Deliberately does not touch ``intent``: the refusal must hold even
            # for a malformed payload, so a bad intent cannot turn a safety
            # rejection into an unrelated AttributeError.
            raise ReadOnlyBrokerError(
                f"{self.name} is open in read-only mode; refusing to submit an "
                f"order. No order was sent."
            )
        self._orders_submitted += 1
        return self._submit_order(intent)

    @final
    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        """Cancel a live order.

        Narrow by design (brief section 37): cancellation exists so a submitted
        order's lifecycle can be closed safely, not so a strategy can manage
        exits. Exit policy is Milestone 9.
        """
        if self._read_only:
            raise ReadOnlyBrokerError(
                f"{self.name} is open in read-only mode; refusing to cancel {broker_order_id}."
            )
        return self._cancel_order(broker_order_id)

    def _submit_order(self, intent: OrderIntent) -> ExecutionResult:
        """Submission hook. Brokers that can trade override this.

        The default reports ``NOT_IMPLEMENTED`` rather than fabricating a fill,
        so a broker that has no execution path says so instead of appearing to
        work.
        """
        raise OrderSubmissionNotImplementedError(self.name)

    def _cancel_order(self, broker_order_id: str) -> BrokerOrder:
        """Cancellation hook. Brokers that can trade override this."""
        raise OrderSubmissionNotImplementedError(self.name)

    # --- context manager ---------------------------------------------------
    def __enter__(self) -> Broker:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(mode={self._trading_mode.value}, "
            f"read_only={self._read_only}, orders_submitted={self._orders_submitted})"
        )
