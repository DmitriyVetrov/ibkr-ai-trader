"""One-purpose broker connections for broker-backed data providers.

This exists because of a measured constraint, not a stylistic preference.
Against the validated TWS environment, **only the first uncached
request/response round trip on a freshly opened connection is reliably
answered**. A second explicit request on the same connection can stay pending
forever even though the socket is healthy and the first request worked. It was
reproduced at the raw socket level and survived a TWS restart, so it is not a
stale-session artifact.

The discipline that follows is structural rather than advisory:
:meth:`BrokerSession.fetch` connects, runs **one** operation, and disconnects.
There is no way to hold a connection open and issue a second uncached request
through this class, because there is no method that would let you.

Two further guarantees ride along:

* **Bounded.** The broker is constructed with a request timeout, so an
  unanswered round trip surfaces as a timeout instead of hanging the collector.
* **Zero orders.** Every session asserts the broker submitted nothing. A data
  provider that somehow reached a mutation path fails loudly rather than
  quietly trading.

The data layer does not open IBKR connections itself and contains no IBKR
client code: it goes through the Milestone 2 broker adapter, which remains the
only thing in the system that knows IBKR exists.
"""

from __future__ import annotations

from collections.abc import Callable

from trading_system.broker.base import (
    Broker,
    BrokerConnectionError,
    BrokerError,
    BrokerTimeoutError,
    MarketDataUnavailableError,
    OptionChainUnavailableError,
)
from trading_system.data.providers.base import (
    ProviderError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

__all__ = ["BrokerSession", "OrderSubmissionDetectedError"]


class OrderSubmissionDetectedError(ProviderError):
    """A read-only data path submitted an order. Should be impossible.

    Kept as a live assertion anyway: the cost of checking is a comparison, and
    the cost of not checking is discovering it from a broker statement.
    """


class BrokerSession:
    """Runs exactly one broker operation per connection.

    Args:
        factory: builds a fresh, disconnected broker. Called once per
            :meth:`fetch`, so each retrieval gets its own connection and its
            own round-trip budget.
    """

    def __init__(self, factory: Callable[[], Broker]) -> None:
        self._factory = factory

    def fetch[ResultT](
        self, operation: Callable[[Broker], ResultT], *, description: str
    ) -> ResultT:
        """Open a connection, run ``operation``, close it, return the result.

        ``operation`` must issue at most one uncached broker request. Batching
        several data needs into one operation is only safe when the extra data
        is served from ``ib_async``'s startup cache — account summary,
        positions, open orders and fills — which needs no fresh round trip.

        Raises:
            ProviderUnavailableError: the broker could not be reached.
            ProviderTimeoutError: the broker did not answer in time.
            ProviderResponseError: the broker answered unusably.
        """
        try:
            broker = self._factory()
        except BrokerError as exc:
            raise ProviderUnavailableError(
                f"could not build a broker for {description}: {exc}"
            ) from exc

        try:
            broker.connect()
        except BrokerTimeoutError as exc:
            broker.disconnect()
            raise ProviderTimeoutError(
                f"broker connect timed out for {description}: {exc}"
            ) from exc
        except BrokerConnectionError as exc:
            broker.disconnect()
            raise ProviderUnavailableError(f"broker unavailable for {description}: {exc}") from exc
        except BrokerError as exc:
            broker.disconnect()
            raise ProviderUnavailableError(f"broker refused the connection: {exc}") from exc

        try:
            result = operation(broker)
        except BrokerTimeoutError as exc:
            raise ProviderTimeoutError(f"{description} timed out: {exc}") from exc
        except (MarketDataUnavailableError, OptionChainUnavailableError) as exc:
            # Not a plumbing failure: the broker answered and has nothing.
            raise ProviderUnavailableError(f"{description} unavailable: {exc}") from exc
        except BrokerConnectionError as exc:
            raise ProviderUnavailableError(
                f"broker connection lost during {description}: {exc}"
            ) from exc
        except BrokerError as exc:
            raise ProviderResponseError(f"{description} failed: {exc}") from exc
        finally:
            submitted = broker.orders_submitted
            broker.disconnect()
            if submitted:
                raise OrderSubmissionDetectedError(
                    f"a read-only data provider submitted {submitted} order(s) during "
                    f"{description}; this path must never mutate broker state"
                )

        return result

    def probe(self) -> bool:
        """Whether a broker can even be constructed. Makes no network call."""
        try:
            self._factory()
        except Exception:
            return False
        return True
