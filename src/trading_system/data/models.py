"""Canonical data models.

Everything that leaves the data layer is one of these. Raw provider payloads
never reach a consumer: an AI agent or a deterministic module sees a normalised,
timestamped, provenance-carrying, quality-assessed record and nothing else.

Four properties are structural rather than conventional:

* **Provenance is mandatory.** Every record carries
  :class:`DataSourceMetadata`. There is no way to construct one without saying
  who produced it, when it was retrieved, and how live it is.
* **Unavailable is not zero.** Every measured field is optional and ``None``
  means *not available*. ``implied_volatility=None`` is "we do not know";
  ``implied_volatility=0`` would be a claim about the market.
* **Numbers are exact.** Prices, Greeks, IV and volumes are ``Decimal`` and
  reject binary floats, the same rule the domain layer applies to money.
* **Time is answerable.** ``retrieved_at``, ``source_timestamp``,
  ``published_at`` and ``effective_at`` are kept separately so that "what did
  the system know, and when did it know it" has an answer
  (:mod:`trading_system.data.point_in_time`).

Broker identifiers are preserved exactly as the broker gave them. In
particular ``trading_class`` is never derived from the underlying symbol: real
IBKR validation showed SPY options trade under class ``2SPY`` while the
underlying trades under ``SPY``, and reconstructing one from the other would
produce a contract that does not exist.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Self

from pydantic import Field, model_validator

from trading_system.domain.enums import (
    BarInterval,
    CorporateEventType,
    DataGapStatus,
    DataQuality,
    DataQualityIssue,
    DataType,
    MarketDataOrigin,
    OptionRight,
    RegulatoryFormType,
    SecurityType,
    SourceTier,
)
from trading_system.domain.models import (
    Confidence,
    Identifier,
    ImmutableModel,
    Money,
    UtcDatetime,
)

__all__ = [
    "COLLECTION_FAILED",
    "DATA_SCHEMA_VERSION",
    "SNAPSHOT_CREATED",
    "SNAPSHOT_REOBSERVED",
    "CollectionState",
    "CorporateEvent",
    "DataQualityReport",
    "DataRecord",
    "DataSnapshot",
    "DataSourceMetadata",
    "ExactDecimal",
    "FundamentalSnapshot",
    "HistoricalGap",
    "MarketBar",
    "MarketQuote",
    "NewsArticle",
    "OptionChain",
    "OptionContract",
    "OptionQuote",
    "OptionSnapshot",
    "RawRecord",
    "RegulatoryEvent",
    "SnapshotIndexEntry",
    "clean_report",
]

# --- historical ledger event names -----------------------------------------
#: A new snapshot was written.
SNAPSHOT_CREATED = "SNAPSHOT_CREATED"
#: The provider returned data identical to the newest stored snapshot. No new
#: snapshot; the fact that we looked, and when, is still recorded.
SNAPSHOT_REOBSERVED = "SNAPSHOT_REOBSERVED"
#: A collection attempt did not produce usable data. Existing history is
#: untouched — a bad new record never deletes a good old one.
COLLECTION_FAILED = "COLLECTION_FAILED"

#: Version of the canonical record shapes. Stamped onto every snapshot so a
#: stored record can never silently change meaning after a schema change
#: (Milestone 3 brief section 37). Bump on any incompatible field change.
DATA_SCHEMA_VERSION = "1.0.0"

#: Exact decimal for non-monetary numerics — Greeks, implied volatility,
#: volume, open interest. Deliberately the same float-rejecting validator the
#: domain layer uses for money: a Greek that arrived as a binary float and was
#: stored as one cannot later be compared reproducibly against a threshold.
ExactDecimal = Money

#: Origins that assert the data is currently live from its source. Anything
#: read back out of a cache, a snapshot or the historical store must be
#: relabelled before it is handed on.
LIVE_ORIGINS: frozenset[MarketDataOrigin] = frozenset(
    {
        MarketDataOrigin.BROKER_REALTIME,
        MarketDataOrigin.PROVIDER_REALTIME,
    }
)

#: Origins that are explicitly *not* a live observation of the market now.
DERIVED_ORIGINS: frozenset[MarketDataOrigin] = frozenset(
    {
        MarketDataOrigin.CACHED,
        MarketDataOrigin.HISTORICAL,
        MarketDataOrigin.SIMULATED,
    }
)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
class DataSourceMetadata(ImmutableModel):
    """Where a record came from, and when each of its clocks says it happened.

    The four timestamps are separate on purpose (Milestone 3 brief section 11):

    ``source_timestamp``
        The instant the provider itself stamped on the data.
    ``published_at``
        When the information became public — a news publication time, a filing
        acceptance time. Distinct from ``source_timestamp``, which is when the
        provider produced *its response*.
    ``effective_at``
        When the information became true or applicable. A revised fundamental
        keeps the original period's effective time even though it was published
        later.
    ``retrieved_at``
        When *this system* actually fetched it. Nothing is visible to a
        historical reconstruction before this instant, no matter how old the
        underlying event is.
    """

    provider: Identifier
    source_tier: SourceTier
    origin: MarketDataOrigin
    retrieved_at: UtcDatetime

    source_name: str | None = None
    #: URL, accession number, or whatever identifier the source uses.
    source_identifier: str | None = None
    source_timestamp: UtcDatetime | None = None
    published_at: UtcDatetime | None = None
    effective_at: UtcDatetime | None = None
    #: When the value was observed at the venue, where the provider says so.
    observed_at: UtcDatetime | None = None
    provider_version: str | None = None

    #: Set only when a fallback occurred: the provider we asked first, which
    #: did not answer. ``provider`` always names whoever actually produced the
    #: data, so a record can never claim a source that was not used
    #: (Milestone 3 brief sections 22 and 28).
    requested_provider: Identifier | None = None

    #: Field name -> the provider or source concept that supplied it, for
    #: records assembled from more than one source or from several distinct
    #: series within one source. Empty means every field came from
    #: ``provider`` directly. Merging sources without filling this in is what
    #: section 29 forbids.
    field_provenance: dict[str, str] = Field(default_factory=dict)

    @property
    def used_fallback(self) -> bool:
        return self.requested_provider is not None and self.requested_provider != self.provider

    @property
    def is_live_origin(self) -> bool:
        """Whether this claims to be a live observation of the market now."""
        return self.origin in LIVE_ORIGINS

    def known_at(self, instant: datetime) -> bool:
        """Whether this record was already available to us at ``instant``.

        Every timestamp the record carries must be at or before ``instant``.
        Retrieval is the binding one: information we had not yet fetched was
        not information we had, regardless of when it was published.
        """
        if self.retrieved_at > instant:
            return False
        return all(
            stamp is None or stamp <= instant
            for stamp in (self.source_timestamp, self.published_at, self.effective_at)
        )

    def age_seconds(self, instant: datetime) -> float:
        """Seconds between the most recent source-side stamp and ``instant``.

        Falls back to ``retrieved_at`` when the provider stamped nothing, which
        is the conservative reading: we can vouch for when we fetched it, not
        for when the venue produced it.
        """
        reference = self.observed_at or self.source_timestamp or self.retrieved_at
        return (instant - reference).total_seconds()


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------
class DataQualityReport(ImmutableModel):
    """A structured, machine-readable verdict on one record.

    Deliberately not a single boolean (Milestone 3 brief section 2.4). A record
    can be transport-valid, schema-valid and source-valid while still being
    economically implausible — real IBKR paper validation produced exactly that
    for an SPY volume field. Such a record is preserved verbatim, flagged, and
    excluded from research by ``research_usable``; it is never corrected,
    smoothed or dropped.
    """

    #: The response arrived intact from the provider.
    transport_valid: bool = True
    #: The payload had the shape the canonical model requires.
    schema_valid: bool = True
    #: Provenance is present and internally consistent.
    source_valid: bool = True
    #: Timestamps are present, timezone-aware and not from the future.
    timestamp_valid: bool = True
    #: Within the configured freshness window for this data type.
    freshness_valid: bool = True
    #: Every field a consumer needs is present.
    completeness_valid: bool = True
    #: The values are economically believable.
    plausibility_valid: bool = True
    #: The fields agree with each other (bid <= ask, and so on).
    consistency_valid: bool = True

    #: Whether a research consumer may rely on this record. Derived by
    #: :mod:`trading_system.data.quality` from the dimensions above under a
    #: documented, configurable policy — technical validity alone is not
    #: enough (section 50).
    research_usable: bool = True

    issues: list[DataQualityIssue] = Field(default_factory=list)
    #: Human-readable detail, parallel to ``issues`` in spirit but not indexed
    #: by it: one issue can produce several notes.
    details: list[str] = Field(default_factory=list)
    evaluated_at: UtcDatetime | None = None

    @property
    def technically_valid(self) -> bool:
        """Whether the record is well-formed, regardless of whether it is believable."""
        return self.transport_valid and self.schema_valid and self.source_valid

    @property
    def classification(self) -> DataQuality:
        """Collapse to the coarse enum the Milestone 2 models already carry.

        Lossy on purpose, and only for interoperability with
        :class:`~trading_system.domain.models.MarketDataSnapshot`. Consumers
        that can read the full report should read the full report.
        """
        if not self.technically_valid:
            return DataQuality.UNUSABLE
        if not self.freshness_valid:
            return DataQuality.STALE
        if not (self.completeness_valid and self.plausibility_valid and self.consistency_valid):
            return DataQuality.DEGRADED
        return DataQuality.OK

    def has(self, issue: DataQualityIssue) -> bool:
        return issue in self.issues


def clean_report(evaluated_at: datetime | None = None) -> DataQualityReport:
    """A report with no findings, for records whose quality is assessed later."""
    return DataQualityReport(evaluated_at=evaluated_at)


# ---------------------------------------------------------------------------
# Record base
# ---------------------------------------------------------------------------
class DataRecord(ImmutableModel):
    """Common shape of every canonical record.

    ``as_of`` is the instant the record describes — a quote's exchange
    timestamp, a bar's close, a filing's acceptance. It is *not* when we
    fetched it; that is ``source.retrieved_at``.
    """

    #: Class-level, not a field: identifies the record kind for storage and
    #: routing without every construction site repeating it.
    DATA_TYPE: ClassVar[DataType]

    as_of: UtcDatetime
    source: DataSourceMetadata
    quality: DataQualityReport = Field(default_factory=DataQualityReport)

    @property
    def data_type(self) -> DataType:
        return self.DATA_TYPE

    def known_at(self, instant: datetime) -> bool:
        """Whether this record was available to the system at ``instant``."""
        return self.as_of <= instant and self.source.known_at(instant)

    def with_quality(self, report: DataQualityReport) -> Self:
        """Return a copy carrying ``report``. The original is not modified."""
        return self.model_copy(update={"quality": report})


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
class MarketQuote(DataRecord):
    """A quote for one instrument (Milestone 3 brief section 8).

    Every price is optional. A quote with no bid and no ask is a real and
    common outcome outside market hours; representing it as zeros would make an
    illiquid instrument look tradeable.
    """

    DATA_TYPE: ClassVar[DataType] = DataType.MARKET_QUOTE

    symbol: Identifier
    security_type: SecurityType
    currency: str | None = None
    contract_id: int | None = None
    exchange: str | None = None

    bid: ExactDecimal | None = None
    ask: ExactDecimal | None = None
    last: ExactDecimal | None = None
    close: ExactDecimal | None = None
    open: ExactDecimal | None = None
    high: ExactDecimal | None = None
    low: ExactDecimal | None = None
    volume: ExactDecimal | None = None

    bid_size: ExactDecimal | None = None
    ask_size: ExactDecimal | None = None

    @property
    def has_price(self) -> bool:
        return any(v is not None for v in (self.bid, self.ask, self.last, self.close))

    @property
    def mid(self) -> Decimal | None:
        """Midpoint, or ``None`` when either side is missing.

        Never falls back to ``last``: a midpoint derived from a single side is
        not a midpoint, and the caller must be able to tell the difference.
        """
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


class MarketBar(DataRecord):
    """One aggregated price bar.

    ``as_of`` is the bar's *close* instant, so a bar is never visible to a
    point-in-time reconstruction that sits inside the period it covers.
    """

    DATA_TYPE: ClassVar[DataType] = DataType.MARKET_BAR

    symbol: Identifier
    security_type: SecurityType
    interval: BarInterval
    period_start: UtcDatetime
    period_end: UtcDatetime
    currency: str | None = None

    open: ExactDecimal | None = None
    high: ExactDecimal | None = None
    low: ExactDecimal | None = None
    close: ExactDecimal | None = None
    volume: ExactDecimal | None = None
    trade_count: int | None = Field(default=None, ge=0)
    weighted_average_price: ExactDecimal | None = None

    @model_validator(mode="after")
    def _period_is_ordered(self) -> MarketBar:
        if self.period_end < self.period_start:
            raise ValueError("bar period_end precedes period_start")
        return self


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
class OptionContract(ImmutableModel):
    """A single option contract, identified exactly as the broker identifies it.

    Every identifier here is preserved verbatim. ``trading_class`` in
    particular is never inferred: IBKR reports SPY *options* under trading
    class ``2SPY`` while the SPY *underlying* uses ``SPY``, and a contract
    rebuilt on the assumption that the two match does not exist at the broker.
    """

    underlying: Identifier
    symbol: Identifier
    security_type: SecurityType = SecurityType.OPTION
    expiration: date | None = None
    strike: Money | None = Field(default=None, gt=0)
    right: OptionRight | None = None

    contract_id: int | None = None
    exchange: str | None = None
    primary_exchange: str | None = None
    currency: str | None = None
    multiplier: int | None = Field(default=None, ge=1)
    trading_class: str | None = None
    local_symbol: str | None = None

    #: The broker's raw ``lastTradeDateOrContractMonth`` string, kept even when
    #: it could not be parsed into ``expiration`` (``YYYYMM`` with no day).
    #: Inventing a day would fabricate an expiry that does not exist.
    raw_last_trade_date: str | None = None

    @property
    def is_fully_identified(self) -> bool:
        """Whether this names one specific tradeable contract."""
        return (
            self.expiration is not None
            and self.strike is not None
            and self.right is not None
            and self.contract_id is not None
        )

    def days_to_expiration(self, reference: date) -> int | None:
        if self.expiration is None:
            return None
        return (self.expiration - reference).days


class OptionQuote(DataRecord):
    """A quote for one option contract, with Greeks where the source has them.

    Every measured field is optional and ``None`` means *not available*. This
    is load-bearing for options: a missing implied volatility is routine, and
    reading it as ``0`` would make an expensive contract look free of
    volatility premium.
    """

    DATA_TYPE: ClassVar[DataType] = DataType.OPTION_QUOTE

    contract: OptionContract

    bid: ExactDecimal | None = None
    ask: ExactDecimal | None = None
    last: ExactDecimal | None = None
    close: ExactDecimal | None = None
    volume: ExactDecimal | None = None
    open_interest: ExactDecimal | None = None

    implied_volatility: ExactDecimal | None = None
    delta: ExactDecimal | None = None
    gamma: ExactDecimal | None = None
    theta: ExactDecimal | None = None
    vega: ExactDecimal | None = None
    underlying_price: ExactDecimal | None = None

    @property
    def has_price(self) -> bool:
        return any(v is not None for v in (self.bid, self.ask, self.last, self.close))

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal(2)


class OptionChain(DataRecord):
    """Chain metadata for one underlying: what expiries and strikes exist.

    ``contracts`` is populated when the provider enumerates concrete contracts;
    an empty list with populated ``expirations``/``strikes`` means only the
    chain's shape was available, which is what IBKR's chain parameters give.
    """

    DATA_TYPE: ClassVar[DataType] = DataType.OPTION_CHAIN

    underlying: Identifier
    underlying_contract_id: int | None = None
    exchange: str | None = None
    trading_class: str | None = None
    multiplier: int | None = Field(default=None, ge=1)

    expirations: list[date] = Field(default_factory=list)
    strikes: list[Money] = Field(default_factory=list)
    rights: list[OptionRight] = Field(default_factory=list)
    contracts: list[OptionContract] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sorted_and_unique(self) -> OptionChain:
        """Deterministic ordering: the same chain must normalise identically."""
        if list(self.expirations) != sorted(set(self.expirations)):
            raise ValueError("expirations must be sorted and unique")
        if list(self.strikes) != sorted(set(self.strikes)):
            raise ValueError("strikes must be sorted and unique")
        return self


class OptionSnapshot(DataRecord):
    """A complete point-in-time capture of an option chain.

    The whole chain is stored, not only the contract that might later be
    chosen: the purpose is to accumulate an option-chain history that does not
    yet exist anywhere for free (Milestone 3 brief section 30). Selecting a
    contract is Milestone 6's job and is not done here.
    """

    DATA_TYPE: ClassVar[DataType] = DataType.OPTION_SNAPSHOT

    underlying: Identifier
    chain: OptionChain
    quotes: list[OptionQuote] = Field(default_factory=list)
    underlying_quote: MarketQuote | None = None

    @property
    def quote_count(self) -> int:
        return len(self.quotes)


# ---------------------------------------------------------------------------
# News, events, fundamentals, regulatory
# ---------------------------------------------------------------------------
class NewsArticle(DataRecord):
    """One news item, as structured data. No interpretation is attached.

    Classifying an article as bullish or bearish is the research agent's job
    (Milestone 5). The data layer records what was published and when.
    """

    DATA_TYPE: ClassVar[DataType] = DataType.NEWS_ARTICLE

    article_id: Identifier
    headline: str = Field(min_length=1)
    summary: str | None = None
    symbols: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    language: str | None = None
    #: Provider-supplied relevance, where the provider supplies one. Never
    #: computed here — a fabricated relevance score would be an opinion
    #: disguised as data.
    relevance: Confidence | None = None

    @property
    def published_at(self) -> datetime | None:
        return self.source.published_at


class CorporateEvent(DataRecord):
    """A scheduled or announced corporate event.

    ``event_time`` may lie in the future — that is the point of a calendar.
    What must not happen is the event appearing in a snapshot taken before it
    was announced, which ``source.retrieved_at`` and ``announced_at`` together
    prevent (Milestone 3 brief section 26).
    """

    DATA_TYPE: ClassVar[DataType] = DataType.CORPORATE_EVENT

    event_id: Identifier
    event_type: CorporateEventType
    symbol: Identifier
    event_time: UtcDatetime
    announced_at: UtcDatetime | None = None
    #: Whether the issuer has confirmed the date, as opposed to it being an
    #: estimate. Estimated dates move; a consumer must be able to tell.
    confirmed: bool = False
    description: str | None = None
    detail: dict[str, str] = Field(default_factory=dict)

    def known_at(self, instant: datetime) -> bool:
        """A future-dated event is visible once it was announced, not before."""
        if self.announced_at is not None and self.announced_at > instant:
            return False
        return self.source.known_at(instant)


class FundamentalSnapshot(DataRecord):
    """Reported fundamentals for one period.

    A restatement is a new record with its own ``source.published_at``; the
    original keeps its own timestamps and is never edited. That is what lets a
    historical reconstruction see the numbers as first reported rather than as
    later revised.
    """

    DATA_TYPE: ClassVar[DataType] = DataType.FUNDAMENTAL_SNAPSHOT

    symbol: Identifier
    currency: str | None = None
    fiscal_period: str | None = None
    period_start: date | None = None
    period_end: date | None = None

    revenue: Money | None = None
    net_income: Money | None = None
    eps_basic: Money | None = None
    eps_diluted: Money | None = None
    shares_outstanding: ExactDecimal | None = None
    market_capitalization: Money | None = None
    guidance: str | None = None
    next_earnings_date: date | None = None

    #: Filing this was taken from, where it came from a filing.
    filing_accession_number: str | None = None
    filing_form: str | None = None
    filing_filed_at: UtcDatetime | None = None


class RegulatoryEvent(DataRecord):
    """A regulatory filing or event, as metadata. The filing is not interpreted.

    Reading meaning out of a 10-K belongs to the research layer; this records
    that the filing exists, what form it is, and when it was accepted.
    """

    DATA_TYPE: ClassVar[DataType] = DataType.REGULATORY_EVENT

    event_id: Identifier
    symbol: Identifier | None = None
    form_type: RegulatoryFormType
    #: The regulator's exact form string, preserved even when ``form_type``
    #: had to fall back to ``OTHER``.
    raw_form: str
    filed_at: UtcDatetime
    accepted_at: UtcDatetime | None = None
    period_of_report: date | None = None
    company_name: str | None = None
    cik: str | None = None
    accession_number: str | None = None
    url: str | None = None
    description: str | None = None
    items: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Storage envelopes
# ---------------------------------------------------------------------------
class RawRecord(ImmutableModel):
    """A provider response, preserved as received.

    Written once and never rewritten. If a provider returns something
    malformed or implausible, this is what proves it did — normalisation and
    quality assessment produce *new* artifacts and leave this one alone
    (Milestone 3 brief sections 13 and 46).
    """

    provider: Identifier
    data_type: DataType
    #: What was requested: a symbol, a CIK, a chain key.
    key: Identifier
    retrieved_at: UtcDatetime
    payload: Any
    payload_hash: str = Field(min_length=8)
    source_identifier: str | None = None
    source_timestamp: UtcDatetime | None = None
    request: dict[str, str] = Field(default_factory=dict)
    #: Non-fatal notes from retrieval, e.g. a truncated response.
    notes: list[str] = Field(default_factory=list)


class DataSnapshot(ImmutableModel):
    """An immutable point-in-time capture of one dataset.

    A snapshot answers "what did the system know about X at time T", which is a
    stronger requirement than "what is the latest value of X". It carries its
    own provenance, its own quality verdict, its own version stamps and a hash
    of its payload, so it can be verified, deduplicated and replayed without
    consulting anything else.
    """

    snapshot_id: Identifier
    data_type: DataType
    key: Identifier

    as_of: UtcDatetime
    retrieved_at: UtcDatetime
    source_timestamp: UtcDatetime | None = None

    provider: Identifier
    source_tier: SourceTier
    data_origin: MarketDataOrigin

    schema_version: Identifier
    application_version: Identifier
    config_version: Identifier | None = None

    data_quality: DataQualityReport
    payload_hash: str = Field(min_length=8)

    #: Canonical records, JSON-serialised. Kept generic so one storage path
    #: serves every data type; typed access goes through
    #: :func:`trading_system.data.repository.records_of`.
    records: list[dict[str, Any]] = Field(default_factory=list)
    record_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _record_count_matches(self) -> DataSnapshot:
        if self.record_count != len(self.records):
            raise ValueError(
                f"record_count {self.record_count} does not match "
                f"{len(self.records)} stored records"
            )
        return self

    def known_at(self, instant: datetime) -> bool:
        """Whether this snapshot was available to the system at ``instant``.

        Both clocks must have passed: a snapshot describes ``as_of`` but was
        only in our hands from ``retrieved_at``. Using it earlier than either
        is look-ahead bias.
        """
        if self.retrieved_at > instant or self.as_of > instant:
            return False
        return self.source_timestamp is None or self.source_timestamp <= instant


class SnapshotIndexEntry(ImmutableModel):
    """One line of the append-only historical ledger.

    The ledger records *what happened to the store*: snapshots created,
    unchanged payloads re-observed, collections that failed. It is appended to
    and never rewritten, so a failed collection can never erase the evidence of
    an earlier successful one.

    It doubles as the query index for point-in-time lookups, which is why it
    carries ``retrieved_at`` and ``source_timestamp``: a lookup can decide
    visibility from the ledger alone and only read the snapshot file it
    actually needs. The ledger is derived data and can be rebuilt from the
    snapshot files; the snapshots are the truth.
    """

    event: Identifier
    data_type: DataType
    key: Identifier
    provider: Identifier
    recorded_at: UtcDatetime

    snapshot_id: Identifier | None = None
    as_of: UtcDatetime | None = None
    retrieved_at: UtcDatetime | None = None
    source_timestamp: UtcDatetime | None = None
    payload_hash: str | None = None
    record_count: int | None = Field(default=None, ge=0)
    outcome: str | None = None
    research_usable: bool | None = None
    detail: str | None = None

    @property
    def is_snapshot(self) -> bool:
        return self.snapshot_id is not None and self.as_of is not None

    def known_at(self, instant: datetime) -> bool:
        """Whether the snapshot this entry indexes was available at ``instant``.

        Non-snapshot entries — failures, re-observations — are never visible as
        data: they describe the collector, not the market.
        """
        if not self.is_snapshot or self.event != SNAPSHOT_CREATED:
            return False
        if self.as_of is None or self.as_of > instant:
            return False
        if self.retrieved_at is not None and self.retrieved_at > instant:
            return False
        return self.source_timestamp is None or self.source_timestamp <= instant


class CollectionState(ImmutableModel):
    """What the collector knows about one provider/data type/symbol.

    Mutable bookkeeping, deliberately *not* the historical record: it is
    overwritten on each attempt, while the ledger it summarises is append-only.
    Its job is to let tomorrow's collector find yesterday's gap.
    """

    provider: Identifier
    data_type: DataType
    key: Identifier

    last_attempt: UtcDatetime | None = None
    last_successful_collection: UtcDatetime | None = None
    last_error: str | None = None
    last_outcome: str | None = None
    records_collected: int = Field(default=0, ge=0)
    snapshot_count: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)


class HistoricalGap(ImmutableModel):
    """A stretch of history the store does not cover.

    Reported, never silently filled: the initial implementation accumulates
    data going forward and does not download expensive backfills
    (Milestone 3 brief section 33).
    """

    data_type: DataType
    key: Identifier
    status: DataGapStatus
    expected_since: UtcDatetime | None = None
    last_seen: UtcDatetime | None = None
    gap_seconds: float | None = Field(default=None, ge=0)
    detail: str | None = None

    @property
    def blocks_history_dependent_analysis(self) -> bool:
        """Anything other than full coverage is a reason to be careful."""
        return self.status is not DataGapStatus.NO_GAP
