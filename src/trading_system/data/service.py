"""Composition root for the data layer.

One place builds the registry, the repository, the cache, the quality engine
and the collectors from configuration. The CLI and the scheduler both call it,
so a command and a scheduled job cannot end up with differently configured
pipelines — the same failure mode the broker factory exists to prevent.

The broker-backed providers receive a *factory*, not a connection. Each
retrieval opens its own short-lived, read-only connection through
:class:`~trading_system.data.providers.broker_session.BrokerSession`, which is
what keeps the one-reliable-round-trip-per-connection constraint structural
rather than something every call site has to remember.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trading_system.broker.base import Broker
from trading_system.broker.factory import build_broker
from trading_system.data.cache import DataCache
from trading_system.data.collectors import CollectionReport, DataCollector, RegistryCollector
from trading_system.data.collectors.base import detect_gap
from trading_system.data.market_calendar import MarketCalendar
from trading_system.data.models import (
    CollectionState,
    DataSnapshot,
    FundamentalSnapshot,
    HistoricalGap,
    MarketQuote,
    NewsArticle,
    OptionChain,
    OptionQuote,
    RegulatoryEvent,
    SnapshotIndexEntry,
)
from trading_system.data.providers import (
    DataProvider,
    FixtureCorporateEventProvider,
    FixtureNewsProvider,
    FundamentalsProvider,
    IBKRMarketDataProvider,
    IBKROptionsDataProvider,
    MarketDataProvider,
    NewsProvider,
    OptionsDataProvider,
    ProviderDescription,
    RegulatoryProvider,
    SecEdgarRegulatoryProvider,
    SecFundamentalsProvider,
    SimulatedMarketDataProvider,
    SimulatedOptionsDataProvider,
    UrllibHttpFetcher,
)
from trading_system.data.providers.base import ProviderResult
from trading_system.data.providers.broker_session import BrokerSession
from trading_system.data.quality import QualityEngine
from trading_system.data.registry import ProviderRegistry
from trading_system.data.repository import DataRepository, FilesystemDataRepository
from trading_system.domain.enums import DataType
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.settings import (
    BrokerBackend,
    Settings,
    SystemConfig,
    project_root,
)

__all__ = ["DataService", "DataStatus"]


@dataclass(frozen=True, slots=True)
class DataStatus:
    """What the store holds for one provider, data type and key."""

    state: CollectionState
    gap: HistoricalGap


class DataService:
    """Builds and drives the data layer from configuration."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        repository: DataRepository | None = None,
        simulated: bool = False,
        broker_factory: Callable[[], Broker] | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()
        self._simulated = simulated

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = project_root() / data_root
        self._data_root = data_root

        self._repository = repository or FilesystemDataRepository(data_root, clock=self._clock)
        self._cache = DataCache(
            data_root / "cache",
            clock=self._clock,
            default_ttl_seconds=config.data.cache.default_ttl_seconds,
            enabled=config.data.cache.enabled,
        )
        self._quality = QualityEngine(config.data, clock=self._clock)
        self._calendar = MarketCalendar(config.data.market_calendar)
        self._broker_factory = broker_factory or self._default_broker_factory
        self._registry = self._build_registry()

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> DataRepository:
        return self._repository

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def cache(self) -> DataCache:
        return self._cache

    @property
    def calendar(self) -> MarketCalendar:
        return self._calendar

    @property
    def quality_engine(self) -> QualityEngine:
        return self._quality

    @property
    def data_root(self) -> Path:
        return self._data_root

    def providers(self) -> list[ProviderDescription]:
        return list(self._registry.describe())

    # --- collection --------------------------------------------------------
    def collect_quote(self, symbol: str, *, preferred: str | None = None) -> CollectionReport:
        return self._collector(
            DataType.MARKET_QUOTE,
            lambda provider, key: _as_market_provider(provider).fetch_quote(key),
        ).collect(symbol, preferred=preferred)

    def collect_option_chain(
        self, symbol: str, *, preferred: str | None = None
    ) -> CollectionReport:
        return self._collector(
            DataType.OPTION_CHAIN,
            lambda provider, key: _as_options_provider(provider).fetch_chain(key),
        ).collect(symbol, preferred=preferred)

    def collect_option_quotes(
        self, symbol: str, *, expiration: date | None = None, preferred: str | None = None
    ) -> CollectionReport:
        return self._collector(
            DataType.OPTION_QUOTE,
            lambda provider, key: _as_options_provider(provider).fetch_option_quotes(
                key, expiration=expiration
            ),
        ).collect(symbol, preferred=preferred)

    def collect_news(self, symbol: str, *, preferred: str | None = None) -> CollectionReport:
        return self._collector(
            DataType.NEWS_ARTICLE,
            lambda provider, key: _as_news_provider(provider).fetch_news(key),
        ).collect(symbol, preferred=preferred)

    def collect_fundamentals(
        self, symbol: str, *, preferred: str | None = None
    ) -> CollectionReport:
        return self._collector(
            DataType.FUNDAMENTAL_SNAPSHOT,
            lambda provider, key: _as_fundamentals_provider(provider).fetch_fundamentals(key),
        ).collect(symbol, preferred=preferred)

    def collect_filings(self, symbol: str, *, preferred: str | None = None) -> CollectionReport:
        return self._collector(
            DataType.REGULATORY_EVENT,
            lambda provider, key: _as_regulatory_provider(provider).fetch_filings(key),
        ).collect(symbol, preferred=preferred)

    def collect_all(self, symbol: str) -> list[CollectionReport]:
        """Everything configured for one symbol.

        Each data type is collected independently: one provider being down must
        not prevent the others from accumulating history.
        """
        reports = [self.collect_quote(symbol)]
        if symbol.upper() in {s.upper() for s in self._config.data.collection.option_chain_symbols}:
            reports.append(self.collect_option_chain(symbol))
        return reports

    # --- reading -----------------------------------------------------------
    def latest(self, data_type: DataType, symbol: str) -> DataSnapshot | None:
        return self._repository.get_latest(data_type, symbol.upper())

    def as_of(self, data_type: DataType, symbol: str, instant: datetime) -> DataSnapshot | None:
        return self._repository.get_as_of(data_type, symbol.upper(), instant)

    def history(self, data_type: DataType, symbol: str) -> list[SnapshotIndexEntry]:
        return self._repository.ledger(data_type, symbol.upper())

    def status(self) -> list[DataStatus]:
        """Collection state and gap assessment for everything ever collected."""
        now = self._clock.now()
        statuses: list[DataStatus] = []
        for state in self._repository.list_collection_states():
            window = self._config.data.freshness.window_seconds(state.data_type.value)
            statuses.append(
                DataStatus(
                    state=state,
                    gap=detect_gap(
                        state=state,
                        data_type=state.data_type,
                        key=state.key,
                        now=now,
                        # A gap is judged against several freshness windows, not
                        # one: missing a single 5-minute quote is not a hole in
                        # the history, missing a day is.
                        expected_interval_seconds=window * 4,
                    ),
                )
            )
        return statuses

    def configured_symbols(self) -> list[str]:
        return [s.upper() for s in self._config.data.collection.symbols]

    def configured_option_symbols(self) -> list[str]:
        return [s.upper() for s in self._config.data.collection.option_chain_symbols]

    # --- construction ------------------------------------------------------
    def _collector(
        self,
        data_type: DataType,
        operation: Callable[[DataProvider, str], ProviderResult[Any]],
    ) -> RegistryCollector[Any]:
        return RegistryCollector(
            registry=self._registry,
            collector=DataCollector(
                repository=self._repository,
                quality_engine=self._quality,
                data_type=data_type,
                clock=self._clock,
                cache=self._cache,
                config_version=self._config.application.config_version,
                max_records=self._config.data.collection.max_records_per_snapshot,
            ),
            operation=operation,
        )

    def _default_broker_factory(self) -> Broker:
        return build_broker(
            self._settings,
            clock=self._clock,
            backend=BrokerBackend.SIMULATOR if self._simulated else None,
        )

    def _build_registry(self) -> ProviderRegistry:
        registry = ProviderRegistry()
        session = BrokerSession(self._broker_factory)
        timeout = self._settings.ibkr_request_timeout_seconds

        if self._simulated:
            # Offline mode registers only synthetic providers, so nothing can
            # accidentally reach a broker or the network from a --simulated run.
            registry.register(SimulatedMarketDataProvider(clock=self._clock))
            registry.register(SimulatedOptionsDataProvider(clock=self._clock))
        else:
            registry.register(
                IBKRMarketDataProvider(session, clock=self._clock, timeout_seconds=timeout)
            )
            registry.register(
                IBKROptionsDataProvider(session, clock=self._clock, timeout_seconds=timeout)
            )
            self._register_web_providers(registry)

        registry.register(FixtureNewsProvider(self._fixture_dir("news"), clock=self._clock))
        registry.register(
            FixtureCorporateEventProvider(self._fixture_dir("events"), clock=self._clock)
        )
        return registry

    def _register_web_providers(self, registry: ProviderRegistry) -> None:
        """Register the SEC providers, if a User-Agent has been configured.

        The SEC requires a descriptive User-Agent and blocks requests without
        one. Rather than sending an anonymous request that would be refused,
        the providers are simply not registered and ``data providers`` says so.
        """
        user_agent = self._config.data.providers.sec_user_agent.strip()
        if not user_agent:
            return
        fetcher = UrllibHttpFetcher(user_agent=user_agent)
        timeout = self._config.data.providers.http_timeout_seconds
        base_url = self._config.data.providers.sec_base_url
        registry.register(
            SecEdgarRegulatoryProvider(
                fetcher, base_url=base_url, clock=self._clock, timeout_seconds=timeout
            )
        )
        registry.register(
            SecFundamentalsProvider(
                fetcher, base_url=base_url, clock=self._clock, timeout_seconds=timeout
            )
        )

    def _fixture_dir(self, name: str) -> Path:
        return self._data_root / "raw" / "fixtures" / name


# ---------------------------------------------------------------------------
# Narrowing helpers
#
# The registry stores providers by capability, so a call site has to state
# which interface it expects. Failing loudly on a mismatch beats a duck-typed
# AttributeError three frames deeper.
# ---------------------------------------------------------------------------
def _as_market_provider(provider: DataProvider) -> MarketDataProvider:
    if not isinstance(provider, MarketDataProvider):
        raise TypeError(f"{provider.provider_id} is not a market data provider")
    return provider


def _as_options_provider(provider: DataProvider) -> OptionsDataProvider:
    if not isinstance(provider, OptionsDataProvider):
        raise TypeError(f"{provider.provider_id} is not an options data provider")
    return provider


def _as_news_provider(provider: DataProvider) -> NewsProvider:
    if not isinstance(provider, NewsProvider):
        raise TypeError(f"{provider.provider_id} is not a news provider")
    return provider


def _as_fundamentals_provider(provider: DataProvider) -> FundamentalsProvider:
    if not isinstance(provider, FundamentalsProvider):
        raise TypeError(f"{provider.provider_id} is not a fundamentals provider")
    return provider


def _as_regulatory_provider(provider: DataProvider) -> RegulatoryProvider:
    if not isinstance(provider, RegulatoryProvider):
        raise TypeError(f"{provider.provider_id} is not a regulatory provider")
    return provider


#: Canonical record type for each stored data type, for typed snapshot reads.
RECORD_TYPES = {
    DataType.MARKET_QUOTE: MarketQuote,
    DataType.OPTION_CHAIN: OptionChain,
    DataType.OPTION_QUOTE: OptionQuote,
    DataType.NEWS_ARTICLE: NewsArticle,
    DataType.FUNDAMENTAL_SNAPSHOT: FundamentalSnapshot,
    DataType.REGULATORY_EVENT: RegulatoryEvent,
}
