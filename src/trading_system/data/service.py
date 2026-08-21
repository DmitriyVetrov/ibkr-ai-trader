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
from decimal import Decimal
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
from trading_system.data.repository import (
    DataRepository,
    FilesystemDataRepository,
    records_of,
)
from trading_system.domain.enums import CollectionOutcome, DataType
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.settings import (
    BrokerBackend,
    OptionQuoteCollectionConfig,
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
        self,
        symbol: str,
        *,
        expiration: date | None = None,
        target_dte: int | None = None,
        preferred: str | None = None,
    ) -> CollectionReport:
        """Quote the option contracts a decision might actually need.

        *Which* contracts is resolved here rather than by the provider, and
        that split is the point: working them out needs the chain and the
        underlying's price, and a broker-backed provider gets one connection
        whose second uncached round trip may never be answered. Both are
        already in the store, so this reads them and hands the provider an
        explicit list.

        ``expiration`` names one outright. Otherwise the expiration nearest
        ``target_dte`` *within* ``data.yaml``'s ``option_quotes`` window is
        used — and if none falls inside that window the run reports what it
        found rather than reaching outside it, because a DTE-7 quote collected
        in place of a DTE-21 one would satisfy the collector and fail the
        selector for reasons nobody could see.
        """
        key = symbol.upper()
        policy = self._config.data.collection.option_quotes
        started = self._clock.now()

        def refused(error: str) -> CollectionReport:
            """A resolution failure is a collection outcome, not an exception.

            NO_DATA, deliberately: the store is missing something the caller
            can go and collect, which is a different fact from the provider
            being unreachable.
            """
            completed = self._clock.now()
            return CollectionReport(
                provider="NONE",
                data_type=DataType.OPTION_QUOTE,
                key=key,
                outcome=CollectionOutcome.NO_DATA,
                started_at=started,
                completed_at=completed,
                duration_seconds=max((completed - started).total_seconds(), 0.0),
                error=error,
            )

        chain = self._latest_option_chain(key)
        if chain is None:
            return refused(
                f"no stored option chain for {key}. Run 'data collect-options --symbol {key}' "
                f"first: the expirations and strikes to quote come from the chain, not from a "
                f"second broker request."
            )

        resolved = self._resolve_expiration(chain, expiration, target_dte, policy)
        if isinstance(resolved, str):
            return refused(resolved)

        reference = self._reference_price(key)
        if reference is None:
            return refused(
                f"no stored reference price for {key}. Run 'data collect --symbol {key}' first: "
                f"the strike band is a percentage around the underlying's price, and a band "
                f"around an assumed price would quote the wrong contracts."
            )

        strikes, capped = self._strike_window(chain, reference, policy)
        if not strikes:
            return refused(
                f"no strike within {policy.strike_window_pct}% of {reference} for {key}; the "
                f"stored chain lists {len(chain.strikes)} strike(s). Widen "
                f"data.collection.option_quotes.strike_window_pct or collect a fresher chain."
            )

        report = self._collector(
            DataType.OPTION_QUOTE,
            lambda provider, provider_key: _as_options_provider(provider).fetch_option_quotes(
                provider_key,
                expiration=resolved,
                strikes=strikes,
                trading_class=chain.trading_class,
            ),
        ).collect(symbol, preferred=preferred)

        notes = [
            f"expiration {resolved.isoformat()} "
            f"(DTE {(resolved - self._clock.now().date()).days}); "
            f"{len(strikes)} strike(s) within {policy.strike_window_pct}% of {reference}"
        ]
        if capped:
            # Recorded rather than silently applied: a cap that binds changes
            # which contracts a later selection can even see.
            notes.append(
                f"CONTRACT_LIMIT_APPLIED: the band held more than "
                f"max_contracts={policy.max_contracts}; the strikes nearest the money were kept"
            )
        return report.model_copy(update={"notes": [*report.notes, *notes]})

    # --- option quote resolution -------------------------------------------
    def _latest_option_chain(self, symbol: str) -> OptionChain | None:
        """The newest stored chain, re-validated on read.

        A snapshot's ``records`` are stored JSON, not model instances, so they
        are parsed through :func:`records_of` — which also means a stored chain
        that no longer satisfies the current model raises rather than being
        quietly read as absent.
        """
        snapshot = self._repository.get_latest(DataType.OPTION_CHAIN, symbol)
        if snapshot is None or not snapshot.records:
            return None
        chains = records_of(snapshot, OptionChain)
        return chains[0] if chains else None

    def _reference_price(self, symbol: str) -> Decimal | None:
        """The underlying's price, from the store. Never a fresh broker request."""
        snapshot = self._repository.get_latest(DataType.MARKET_QUOTE, symbol)
        if snapshot is None or not snapshot.records:
            return None
        quotes = records_of(snapshot, MarketQuote)
        if not quotes:
            return None
        quote = quotes[0]
        # `last` before `close` before the midpoint: the strike band should be
        # centred on the most recent real trade where there is one. The mid is
        # last because it exists only when both sides were quoted.
        for value in (quote.last, quote.close, quote.mid):
            if value is not None and value > 0:
                return value
        return None

    def _resolve_expiration(
        self,
        chain: OptionChain,
        expiration: date | None,
        target_dte: int | None,
        policy: OptionQuoteCollectionConfig,
    ) -> date | str:
        """The expiration to quote, or a sentence saying why there is none."""
        today = self._clock.now().date()
        if expiration is not None:
            if expiration not in chain.expirations:
                available = ", ".join(e.isoformat() for e in chain.expirations[:8])
                return (
                    f"the stored chain does not list {expiration.isoformat()}; it has "
                    f"{len(chain.expirations)} expiration(s): {available}..."
                )
            return expiration

        eligible = [
            candidate
            for candidate in chain.expirations
            if policy.min_dte <= (candidate - today).days <= policy.max_dte
        ]
        if not eligible:
            listed = ", ".join(f"{(e - today).days}" for e in chain.expirations[:10])
            return (
                f"no stored expiration falls within the configured DTE window "
                f"[{policy.min_dte}, {policy.max_dte}]; the chain's DTEs are: {listed}. "
                f"Collect a fresher chain, widen data.collection.option_quotes, or name an "
                f"expiration explicitly."
            )

        wanted = target_dte if target_dte is not None else (policy.min_dte + policy.max_dte) // 2
        # Ties break on the earlier expiration, so the choice is reproducible.
        return min(eligible, key=lambda e: (abs((e - today).days - wanted), e))

    def _strike_window(
        self,
        chain: OptionChain,
        reference: Decimal,
        policy: OptionQuoteCollectionConfig,
    ) -> tuple[list[Decimal], bool]:
        """Strikes inside the configured band, capped nearest-the-money."""
        band = reference * Decimal(str(policy.strike_window_pct)) / Decimal(100)
        inside = [s for s in chain.strikes if abs(s - reference) <= band]
        # Two rights per strike, so the contract cap halves into a strike cap.
        strike_cap = max(1, policy.max_contracts // 2)
        capped = len(inside) > strike_cap
        if capped:
            inside = sorted(sorted(inside, key=lambda s: (abs(s - reference), s))[:strike_cap])
        return inside, capped

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
