"""Market data providers.

Two implementations ship in Milestone 3:

``IBKRMarketDataProvider``
    Real quotes through the validated Milestone 2 broker adapter. Free with an
    IBKR account, no market-data subscription required when the delayed feed is
    configured — and a delayed quote honestly labelled ``BROKER_DELAYED`` is
    worth more than a realtime one that never arrives.
``SimulatedMarketDataProvider``
    Deterministic offline data for tests and dry runs. Everything it produces
    is stamped ``SIMULATED`` and can never be mistaken for a market quote.

Historical bars are **not** implemented for IBKR. Doing so would mean adding a
historical-data method to the broker abstraction, and the free alternatives
either require a key or are not reliably available. ``fetch_bars`` therefore
reports that it has no data rather than fabricating any; the model and the
interface exist so a later provider can fill them in without a consumer change.
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from trading_system.broker.base import Broker
from trading_system.data.hashing import payload_hash
from trading_system.data.models import MarketBar, MarketQuote, RawRecord
from trading_system.data.normalizers.broker import market_quote_from_broker
from trading_system.data.providers.base import (
    DataProvider,
    ProviderAvailability,
    ProviderCost,
    ProviderResult,
    ProviderUnavailableError,
    failed_result,
    successful_result,
)
from trading_system.data.providers.broker_session import BrokerSession
from trading_system.domain.enums import (
    BarInterval,
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    SecurityType,
    SourceTier,
)
from trading_system.domain.models import MarketDataSnapshot
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = [
    "IBKRMarketDataProvider",
    "MarketDataProvider",
    "SimulatedMarketDataProvider",
]


class MarketDataProvider(DataProvider):
    """Interface for anything that can quote an instrument."""

    @property
    def data_types(self) -> frozenset[DataType]:
        return frozenset({DataType.MARKET_QUOTE})

    @abstractmethod
    def fetch_quote(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
    ) -> ProviderResult[MarketQuote]:
        """Retrieve the current quote for one instrument."""

    def fetch_bars(
        self,
        symbol: str,
        interval: BarInterval = BarInterval.DAY_1,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> ProviderResult[MarketBar]:
        """Retrieve historical bars.

        The default reports ``NO_DATA``. A provider that cannot supply bars
        says so; it does not synthesise them from quotes.
        """
        return failed_result(
            provider_id=self.provider_id,
            data_type=DataType.MARKET_BAR,
            key=symbol.upper(),
            outcome=CollectionOutcome.NO_DATA,
            error=f"{self.provider_id} does not supply historical bars",
        )


class IBKRMarketDataProvider(MarketDataProvider):
    """Quotes from IBKR, through the Milestone 2 broker adapter.

    Reuses the validated adapter rather than opening its own connection: there
    is exactly one piece of IBKR client code in this system, and it is not
    here. Each retrieval runs on its own connection through
    :class:`~trading_system.data.providers.broker_session.BrokerSession`,
    respecting the one-reliable-round-trip-per-connection constraint.
    """

    provider_id = "IBKR"
    display_name = "Interactive Brokers"
    #: An exchange feed relayed by the broker we actually trade through — the
    #: same venue our orders reach, which is as authoritative as a price gets.
    tier = SourceTier.TIER_1
    cost = ProviderCost.FREE_WITH_ACCOUNT
    origin = MarketDataOrigin.BROKER_DELAYED
    requires_broker = True
    requires_network = True
    notes = "Read-only. Origin is taken from the broker response, never assumed."

    def __init__(
        self,
        session: BrokerSession,
        *,
        clock: Clock | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self._session = session
        self._clock = clock or SystemClock()

    def availability(self) -> ProviderAvailability:
        return (
            ProviderAvailability.AVAILABLE
            if self._session.probe()
            else ProviderAvailability.UNAVAILABLE
        )

    def fetch_quote(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
    ) -> ProviderResult[MarketQuote]:
        key = symbol.upper()
        try:
            snapshot = self._session.fetch(
                lambda broker: _quote(broker, key, security_type),
                description=f"market data for {key}",
            )
        except ProviderUnavailableError as exc:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.MARKET_QUOTE,
                key=key,
                outcome=CollectionOutcome.PROVIDER_UNAVAILABLE,
                error=str(exc),
            )
        except Exception as exc:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.MARKET_QUOTE,
                key=key,
                outcome=CollectionOutcome.INVALID_DATA,
                error=str(exc),
            )

        retrieved_at = self._clock.now()
        # The closest thing to a raw payload that can leave the broker layer:
        # ib_async objects are confined to broker/ibkr/ by design, so the raw
        # record holds the adapter's unmodified snapshot rather than the wire
        # object. Nothing has been repaired or filled in at this point.
        payload = snapshot.model_dump(mode="json")
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.MARKET_QUOTE,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            source_timestamp=snapshot.as_of,
            request={"symbol": key, "security_type": security_type.value},
            notes=["payload is the broker adapter's snapshot; ib_async objects never leave"],
        )

        quote = market_quote_from_broker(
            snapshot,
            source=self.metadata(
                retrieved_at=retrieved_at,
                # Taken from the response, not assumed: the broker tells us
                # whether the feed was realtime, delayed or frozen.
                origin=snapshot.origin,
                source_identifier=f"ibkr:{key}",
                source_timestamp=snapshot.as_of,
                observed_at=snapshot.as_of,
            ),
        )
        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.MARKET_QUOTE,
            key=key,
            records=[quote],
            raw=raw,
        )


def _quote(broker: Broker, symbol: str, security_type: SecurityType) -> MarketDataSnapshot:
    """The single broker call a market-data session is allowed to make."""
    return broker.get_market_data(symbol, security_type)


class SimulatedMarketDataProvider(MarketDataProvider):
    """Deterministic offline quotes, stamped ``SIMULATED``.

    Exists so the collection pipeline, the CLI and the tests can run with no
    gateway and no network. Its output is labelled at every layer — provider
    id, source name and origin — so it cannot be read as market data by
    accident.
    """

    provider_id = "SIMULATOR"
    display_name = "Deterministic simulator"
    #: Not a source of truth about the world at all. Tier is a trust ranking,
    #: and simulated data has none.
    tier = SourceTier.TIER_4
    cost = ProviderCost.FREE
    origin = MarketDataOrigin.SIMULATED
    notes = "Synthetic. Never a substitute for market data."

    def __init__(self, *, clock: Clock | None = None, timeout_seconds: float = 5.0) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self._clock = clock or SystemClock()

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability.AVAILABLE

    def fetch_quote(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
    ) -> ProviderResult[MarketQuote]:
        from trading_system.broker.simulator.market import simulated_quote

        key = symbol.upper()
        snapshot = simulated_quote(key, self._clock, security_type)
        retrieved_at = self._clock.now()
        payload = snapshot.model_dump(mode="json")
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.MARKET_QUOTE,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            request={"symbol": key, "security_type": security_type.value},
            notes=["SIMULATED - not market data"],
        )
        quote = market_quote_from_broker(
            snapshot,
            source=self.metadata(
                retrieved_at=retrieved_at,
                source_identifier=f"simulator:{key}",
                source_timestamp=snapshot.as_of,
                observed_at=snapshot.as_of,
            ),
        )
        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.MARKET_QUOTE,
            key=key,
            records=[quote],
            raw=raw,
        )
