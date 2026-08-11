"""Provider adapters behind a common interface.

Providers retrieve data. They do not rank, classify, allocate or execute — see
:mod:`trading_system.data.providers.base` for the boundary and why it is drawn
where it is.

What ships in Milestone 3, and at what cost:

============================  =========  ==================================
Provider                      Cost       Status
============================  =========  ==================================
``IBKR`` market data          account    implemented, paper-validated
``IBKR`` option chains        account    implemented, paper-validated
``SEC_EDGAR`` filings         free       implemented, needs outbound HTTPS
``SEC_XBRL`` fundamentals     free       implemented, needs outbound HTTPS
``FIXTURE_NEWS``              free       interface + replay; live news deferred
``FIXTURE_EVENTS``            free       interface + replay; live calendar deferred
``SIMULATOR``                 free       synthetic, for offline runs and tests
============================  =========  ==================================

No paid provider is required, configured, or referenced anywhere.
"""

from trading_system.data.providers.base import (
    DataProvider,
    ProviderAvailability,
    ProviderCost,
    ProviderDescription,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from trading_system.data.providers.broker_session import BrokerSession
from trading_system.data.providers.fundamentals import (
    FundamentalsProvider,
    SecFundamentalsProvider,
)
from trading_system.data.providers.http import (
    HttpFetcher,
    HttpResponse,
    StaticHttpFetcher,
    UrllibHttpFetcher,
)
from trading_system.data.providers.market import (
    IBKRMarketDataProvider,
    MarketDataProvider,
    SimulatedMarketDataProvider,
)
from trading_system.data.providers.news import (
    CorporateEventProvider,
    FixtureCorporateEventProvider,
    FixtureNewsProvider,
    NewsProvider,
)
from trading_system.data.providers.options import (
    IBKROptionsDataProvider,
    OptionsDataProvider,
    SimulatedOptionsDataProvider,
)
from trading_system.data.providers.regulatory import (
    RegulatoryProvider,
    SecEdgarRegulatoryProvider,
    SecTickerResolver,
)

__all__ = [
    "BrokerSession",
    "CorporateEventProvider",
    "DataProvider",
    "FixtureCorporateEventProvider",
    "FixtureNewsProvider",
    "FundamentalsProvider",
    "HttpFetcher",
    "HttpResponse",
    "IBKRMarketDataProvider",
    "IBKROptionsDataProvider",
    "MarketDataProvider",
    "NewsProvider",
    "OptionsDataProvider",
    "ProviderAvailability",
    "ProviderCost",
    "ProviderDescription",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderResult",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "RegulatoryProvider",
    "SecEdgarRegulatoryProvider",
    "SecFundamentalsProvider",
    "SecTickerResolver",
    "SimulatedMarketDataProvider",
    "SimulatedOptionsDataProvider",
    "StaticHttpFetcher",
    "UrllibHttpFetcher",
]
