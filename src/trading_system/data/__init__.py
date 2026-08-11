"""Market, news, fundamental and regulatory data acquisition.

The pipeline, and why each stage exists:

.. code-block:: text

    PROVIDERS      retrieve, and say honestly who they are
        |
    RAW            preserved verbatim, never edited - the evidence
        |
    NORMALIZATION  canonical shape, provenance attached, values untouched
        |
    QUALITY        eight independent dimensions; flags, never fixes
        |
    SNAPSHOT       immutable, hashed, version-stamped, point-in-time
        |
    HISTORICAL     append-only ledger; accumulates, never overwrites
        |
    REPOSITORY     the only interface consumers see

The layer's job is not to be clever. It is to make the system able to answer
"what did I know, when did I know it, where did it come from, and can I trust
it" — for every number a future agent or risk check will act on.

Three rules do most of the work:

* an unavailable value is ``None``, never ``0``;
* a suspicious value is preserved and flagged, never corrected;
* a record is invisible to any reconstruction of a time before we retrieved it.
"""

from typing import TYPE_CHECKING, Any

from trading_system.data.models import (
    DATA_SCHEMA_VERSION,
    CorporateEvent,
    DataQualityReport,
    DataRecord,
    DataSnapshot,
    DataSourceMetadata,
    FundamentalSnapshot,
    MarketBar,
    MarketQuote,
    NewsArticle,
    OptionChain,
    OptionContract,
    OptionQuote,
    OptionSnapshot,
    RawRecord,
    RegulatoryEvent,
)
from trading_system.data.point_in_time import LookAheadError, assert_no_look_ahead, visible_at

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.data.quality import QualityContext, QualityEngine
    from trading_system.data.registry import ProviderRegistry
    from trading_system.data.repository import DataRepository, FilesystemDataRepository
    from trading_system.data.service import DataService

#: Members loaded on first access rather than at import time.
#:
#: Importing ``trading_system.data.models`` executes this file, and
#: ``data.service`` reaches the broker factory. Loading it eagerly therefore
#: pulled the whole broker package into the import graph of anything that
#: merely wanted a canonical *value type* — including the AI agents, whose
#: entire premise is that they cannot reach a broker. The boundary tests in
#: ``tests/research/test_boundaries.py`` follow package ``__init__`` files
#: precisely because Python does, and they found this.
#:
#: Deferring costs nothing: the public API is unchanged, and the first real
#: consumer of a repository or a service pays the import then. Do not "tidy"
#: the ``__getattr__`` away.
_LAZY = {
    "DataRepository": "repository",
    "DataService": "service",
    "FilesystemDataRepository": "repository",
    "ProviderRegistry": "registry",
    "QualityContext": "quality",
    "QualityEngine": "quality",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        module = import_module(f"trading_system.data.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DATA_SCHEMA_VERSION",
    "CorporateEvent",
    "DataQualityReport",
    "DataRecord",
    "DataRepository",
    "DataService",
    "DataSnapshot",
    "DataSourceMetadata",
    "FilesystemDataRepository",
    "FundamentalSnapshot",
    "LookAheadError",
    "MarketBar",
    "MarketQuote",
    "NewsArticle",
    "OptionChain",
    "OptionContract",
    "OptionQuote",
    "OptionSnapshot",
    "ProviderRegistry",
    "QualityContext",
    "QualityEngine",
    "RawRecord",
    "RegulatoryEvent",
    "assert_no_look_ahead",
    "visible_at",
]
