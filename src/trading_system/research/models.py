"""Canonical market-research models.

Four artifacts, each with a JSON schema in ``schemas/``:

:class:`ResearchInput`
    Everything the Market Researcher agent is given about one underlying, and
    the **only** thing it may reason from. Assembled point-in-time from the
    data repository; carries no repository handle, no provider, no broker and
    no path.
:class:`ResearchAgentOutput`
    The agent's structured response. Prose is not a valid response.
:class:`MarketResearchReport`
    The canonical, immutable outlook for one underlying at one instant.
:class:`ResearchRunResult`
    The immutable record of one run: which universe it consumed, which
    underlyings were researched, how each ended, and which model answered.

Five properties are structural rather than conventional, and they are what
make a stored report auditable months later:

* **Evidence cannot be fabricated.** Every evidence item in the input carries a
  derived ``evidence_id``. The agent's output references those ids and nothing
  else; the report then copies each item's *facts* — source, tier,
  ``published_at``, ``retrieved_at``, snapshot id — from the input rather than
  from the model. There is no field on an agent's response into which it could
  write a publication date or a URL, so it cannot invent one.
* **No contract is expressible.** There is no field anywhere here for a strike,
  an expiry date, a right, a quantity, a price to pay or an amount of money.
  The option context the agent sees is deliberately aggregate — days to
  expiration and implied volatility, never a contract — so "buy the 680 call"
  is not merely discouraged, it cannot be represented.
* **Unavailable is not zero.** Every measured field is optional, and a missing
  implied volatility is ``None`` plus a recorded
  :class:`~trading_system.domain.enums.ResearchDataGap`, never ``0``.
* **A failure carries no view.** A report whose status is not ``SUCCESS`` has
  no hypothesis, no confidence and no catalysts. An outage cannot become an
  opinion.
* **Confidence is a band, never a probability.** There is no percentage in a
  research report. A numeric value appears only when projecting onto the
  Milestone 1 workflow boundary, and it is documented there as a band
  representative with no calibration claimed.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    ClaimSupport,
    ConfidenceLevel,
    DataQuality,
    Direction,
    EvidenceDirection,
    EvidenceKind,
    EvidenceStance,
    ExpectedMagnitude,
    MarketDataOrigin,
    MarketEventType,
    MarketHypothesis,
    RelevanceLevel,
    ResearchDataGap,
    ResearchStatus,
    RiskCategory,
    SourceTier,
)
from trading_system.domain.models import (
    Confidence,
    Identifier,
    ImmutableModel,
    Money,
    ResearchReport,
    SourceReference,
    SystemVersions,
    Ticker,
    UtcDatetime,
)

__all__ = [
    "CONFIDENCE_BAND_VALUE",
    "RESEARCH_SCHEMA_VERSION",
    "AgentEventAssessment",
    "AgentEvidenceAssessment",
    "Catalyst",
    "EventItem",
    "EvidenceItem",
    "FundamentalItem",
    "HistoricalContext",
    "InvalidationCondition",
    "MarketContext",
    "MarketResearchReport",
    "MarketSnapshot",
    "OptionMarketContext",
    "OptionTermPoint",
    "ReportedEvent",
    "ReportedEvidence",
    "ResearchAgentMetadata",
    "ResearchAgentOutput",
    "ResearchDataQualitySummary",
    "ResearchHorizon",
    "ResearchInput",
    "ResearchLimitsSnapshot",
    "ResearchRunCounts",
    "ResearchRunResult",
    "ResearchSourcePolicySnapshot",
    "ResearchWindowSnapshot",
    "RiskAssessment",
    "SourceProvenance",
    "SupportedClaim",
    "evidence_identifier",
    "report_identifier",
]

#: Version of the research artifact shapes. Stamped onto every stored report so
#: a past outlook can never silently change meaning after a schema change.
RESEARCH_SCHEMA_VERSION = "1.0.0"

#: Band representatives used **only** when projecting a report onto the
#: Milestone 1 workflow boundary, whose ``confidence`` field is a float.
#:
#: These are **not probabilities**. No calibration has been measured and none is
#: claimed. They exist so a coarse band survives a numeric field without any
#: stage inventing a finer judgement than the evidence supports. The only
#: property downstream code may rely on is the ordering LOW < MEDIUM < HIGH.
CONFIDENCE_BAND_VALUE: dict[ConfidenceLevel, float] = {
    ConfidenceLevel.LOW: 0.25,
    ConfidenceLevel.MEDIUM: 0.55,
    ConfidenceLevel.HIGH: 0.85,
}

#: Trust ordering, most trusted first. Used to prefer evidence and to decide
#: whether a confidence band is licensed — never to discard a source.
TIER_ORDER: tuple[SourceTier, ...] = (
    SourceTier.TIER_1,
    SourceTier.TIER_2,
    SourceTier.TIER_3,
    SourceTier.TIER_4,
)


def tier_rank(tier: SourceTier) -> int:
    """Position in the trust ordering; lower is more trusted."""
    return TIER_ORDER.index(tier)


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------
class SourceProvenance(ImmutableModel):
    """Where a fact came from, precisely enough to cite and to re-find.

    ``provider`` always names whoever actually produced the record;
    ``requested_provider`` records who was asked and did not answer. A research
    claim can therefore never cite a source that was not consulted
    (specification section 37).

    ``source_tier`` is the *resolved* tier: the record's own tier unless
    ``config/sources.yaml`` classifies the identifier explicitly, in which case
    the configured policy wins. There is one source-ranking system and this is
    what reading it produces.
    """

    provider: Identifier
    source_tier: SourceTier
    retrieved_at: UtcDatetime
    snapshot_id: Identifier

    source_name: str | None = None
    #: URL, accession number, article id — whatever identifies it at the source.
    source_identifier: str | None = None
    published_at: UtcDatetime | None = None
    source_timestamp: UtcDatetime | None = None
    requested_provider: Identifier | None = None
    origin: MarketDataOrigin | None = None
    #: Provider-supplied relevance, carried through unchanged. Never computed
    #: here: a fabricated relevance score is an opinion disguised as data.
    provider_relevance: Confidence | None = None

    def to_source_reference(self) -> SourceReference:
        """Project onto the Milestone 1 provenance record."""
        return SourceReference(
            source_name=self.source_name or self.provider,
            source_url_or_identifier=self.source_identifier or self.snapshot_id,
            source_tier=self.source_tier,
            published_at=self.published_at,
            retrieved_at=self.retrieved_at,
            relevance=self.provider_relevance if self.provider_relevance is not None else 1.0,
        )


class ResearchDataQualitySummary(ImmutableModel):
    """What the data behind this report was actually like (brief section 55).

    A projection of the data layer's verdicts plus what was simply absent.
    Nothing here is re-derived: the quality engine owns the judgement, and the
    gap list records what the pipeline could not find. The confidence policy
    reads both, which is what stops a ``HIGH`` resting on nothing.
    """

    research_usable: bool
    classification: DataQuality = DataQuality.OK
    freshness_valid: bool = True
    completeness_valid: bool = True
    plausibility_valid: bool = True
    consistency_valid: bool = True

    records_considered: int = Field(default=0, ge=0)
    records_research_usable: int = Field(default=0, ge=0)
    records_excluded_not_usable: int = Field(default=0, ge=0)
    #: Everything the input did not have. ``None`` is never rendered as zero.
    gaps: list[ResearchDataGap] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    @property
    def gap_count(self) -> int:
        return len(self.gaps)

    def has_gap(self, gap: ResearchDataGap) -> bool:
        return gap in self.gaps


class ResearchHorizon(ImmutableModel):
    """The window the question is asked about, carried with every artifact."""

    min_days: int = Field(ge=1, le=365)
    max_days: int = Field(ge=1, le=365)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> ResearchHorizon:
        if self.min_days > self.max_days:
            raise ValueError("research horizon min_days must not exceed max_days")
        return self

    def contains(self, days: int) -> bool:
        return self.min_days <= days <= self.max_days

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.min_days}-{self.max_days} days"


class ResearchWindowSnapshot(ImmutableModel):
    """The data windows actually applied, echoed into the stored artifact.

    A report that referred to "the configuration" would stop explaining itself
    the moment that configuration changed.
    """

    news_lookback_days: int = Field(ge=0)
    event_lookahead_days: int = Field(ge=0)
    event_lookback_days: int = Field(ge=0)
    historical_lookback_days: int = Field(ge=0)
    fundamentals_lookback_days: int = Field(ge=0)
    regulatory_lookback_days: int = Field(ge=0)
    volatility_annualization_days: int = Field(ge=1)


class ResearchLimitsSnapshot(ImmutableModel):
    """The cost ceilings applied, and whether any of them actually bit."""

    max_evidence_items: int = Field(ge=0)
    max_news_items: int = Field(ge=0)
    max_events: int = Field(ge=0)
    max_regulatory_items: int = Field(ge=0)
    max_fundamental_periods: int = Field(ge=0)
    #: Which sections were truncated, so a thin input is never mistaken for a
    #: quiet market.
    truncated: list[str] = Field(default_factory=list)


class ResearchSourcePolicySnapshot(ImmutableModel):
    """The trust policy in force, read from ``config/sources.yaml``."""

    config_version: Identifier
    require_source_attribution: bool = True
    min_sources_per_report: int = Field(default=1, ge=0)
    tier_1_count: int = Field(default=0, ge=0)
    tier_2_count: int = Field(default=0, ge=0)
    tier_3_count: int = Field(default=0, ge=0)
    tier_4_count: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Input evidence
# ---------------------------------------------------------------------------
class EvidenceItem(ImmutableModel):
    """One retrieved fact the agent may reason from, with its provenance.

    ``evidence_id`` is derived from the item's content and provenance, so the
    same stored record always produces the same id and an id the agent returns
    can be checked against the input it was given. That check is the whole
    anti-fabrication mechanism: an evidence id the input does not contain means
    the model invented a source, and the response is rejected.

    The item states *what was retrieved*, never what it means. Classification —
    bullish, bearish, decisive — is the agent's job, expressed separately in
    :class:`AgentEvidenceAssessment`, so the fact and the interpretation of it
    never merge into one unreviewable sentence.
    """

    evidence_id: Identifier
    kind: EvidenceKind
    summary: str = Field(min_length=1)
    source: SourceProvenance

    #: When the underlying information became public, where that is known. For
    #: a market observation this is the observation instant.
    occurred_at: UtcDatetime | None = None
    detail: str | None = None
    #: Present only for news that was grouped: how many further reports of the
    #: same story were folded in, and where they came from. Ten syndicated
    #: copies are corroboration, not ten independent catalysts.
    duplicate_count: int = Field(default=0, ge=0)
    duplicate_source_names: list[str] = Field(default_factory=list)
    #: The data layer's verdict on the record behind this item.
    research_usable: bool = True

    @property
    def is_corroborated(self) -> bool:
        return self.duplicate_count > 0


class EventItem(ImmutableModel):
    """A dated event that could move the underlying (brief sections 12, 57).

    ``announced_at`` is what makes a future-dated event legitimate evidence: an
    earnings date next month is exactly what a calendar is for, but it may only
    be visible to a reconstruction of an instant *after* it was announced. An
    event with no announcement time cannot support hypothesis ``D``, because
    nothing establishes that we knew about it.
    """

    event_id: Identifier
    event_type: MarketEventType
    summary: str = Field(min_length=1)
    expected_event_time: UtcDatetime
    source: SourceProvenance

    announced_at: UtcDatetime | None = None
    #: Whether the issuer has confirmed the date rather than it being an
    #: estimate. Estimated dates move; a consumer must be able to tell.
    confirmed: bool = False
    #: Days from the research instant to the event. Negative means it already
    #: happened, which is still evidence — a reaction is observable.
    days_until: int | None = None
    within_horizon: bool = False
    detail: str | None = None
    evidence_id: Identifier | None = None

    @property
    def is_announced(self) -> bool:
        return self.announced_at is not None


class FundamentalItem(ImmutableModel):
    """Reported fundamentals for one period, as first reported."""

    period_end: date | None = None
    fiscal_period: str | None = None
    currency: str | None = None
    revenue: Money | None = None
    net_income: Money | None = None
    eps_basic: Money | None = None
    eps_diluted: Money | None = None
    shares_outstanding: Money | None = None
    market_capitalization: Money | None = None
    guidance: str | None = None
    next_earnings_date: date | None = None
    filing_form: str | None = None
    filing_filed_at: UtcDatetime | None = None
    source: SourceProvenance
    evidence_id: Identifier | None = None


class MarketSnapshot(ImmutableModel):
    """The underlying's own market state at the research instant.

    Every price is optional. A quote with no bid and no ask is a real and
    common outcome outside market hours, and representing it as zeros would
    make an illiquid instrument look tradeable.
    """

    symbol: Ticker
    as_of: UtcDatetime
    origin: MarketDataOrigin
    source: SourceProvenance

    last: Money | None = None
    close: Money | None = None
    bid: Money | None = None
    ask: Money | None = None
    volume: Money | None = None
    currency: str | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    data_quality: ResearchDataQualitySummary | None = None

    @property
    def has_price(self) -> bool:
        return any(v is not None for v in (self.last, self.close, self.bid, self.ask))


class HistoricalContext(ImmutableModel):
    """Recent price behaviour, computed only from what the store actually had.

    Nothing here is a technical-analysis signal. There is no moving average, no
    oscillator and no pattern: the research agent is not a technical strategy
    (brief section 22). These are descriptive statistics over the configured
    window, and every one is ``None`` when the window did not contain enough
    observations to compute it honestly.

    ``realized_volatility`` carries its own window and annualisation factor so
    a comparison against an implied volatility over a different horizon is
    visibly a mismatch rather than an implicit one (brief section 24).
    """

    observation_count: int = Field(default=0, ge=0)
    period_start: UtcDatetime | None = None
    period_end: UtcDatetime | None = None
    first_close: Money | None = None
    last_close: Money | None = None
    high: Money | None = None
    low: Money | None = None
    change_pct: Money | None = None
    max_drawdown_pct: Money | None = None
    realized_volatility: Money | None = None
    realized_volatility_window_days: int | None = Field(default=None, ge=0)
    realized_volatility_annualization_days: int | None = Field(default=None, ge=1)
    snapshot_ids: list[str] = Field(default_factory=list)

    @property
    def has_volatility(self) -> bool:
        return self.realized_volatility is not None


class OptionTermPoint(ImmutableModel):
    """One point of the implied-volatility term structure.

    Deliberately keyed by *days to expiration* rather than by an expiry date,
    and carrying no strike and no right. The agent may observe that near-dated
    volatility is elevated; it must not be able to name a contract, and the
    cheapest way to guarantee that is to never show it one (brief section 23).
    """

    days_to_expiration: int = Field(ge=0)
    atm_implied_volatility: Money | None = None
    contract_count: int = Field(default=0, ge=0)
    total_volume: Money | None = None
    total_open_interest: Money | None = None


class OptionMarketContext(ImmutableModel):
    """Aggregate option-market conditions. Never a contract.

    ``implied_volatility`` is ``None`` when unavailable and the input records
    ``IMPLIED_VOLATILITY_UNAVAILABLE``. It is never ``0`` — "we have no implied
    volatility" and "this option carries no volatility premium" are different
    claims about a market (brief section 24).
    """

    underlying: Ticker
    as_of: UtcDatetime
    source: SourceProvenance

    expiration_count: int = Field(default=0, ge=0)
    strike_count: int = Field(default=0, ge=0)
    nearest_expiration_days: int | None = None
    atm_implied_volatility: Money | None = None
    total_option_volume: Money | None = None
    total_open_interest: Money | None = None
    term_structure: list[OptionTermPoint] = Field(default_factory=list)
    evidence_id: Identifier | None = None

    @property
    def has_implied_volatility(self) -> bool:
        return self.atm_implied_volatility is not None


class MarketContext(ImmutableModel):
    """Broad-market and reference context, when the store had it.

    When it did not, this is absent from the input and the gap list says
    ``MARKET_CONTEXT_UNAVAILABLE``. The research layer never retrieves it
    itself and never substitutes a remembered value (brief section 21).
    """

    broad_index_symbol: Ticker | None = None
    broad_index: MarketSnapshot | None = None
    broad_index_history: HistoricalContext | None = None
    reference_snapshots: list[MarketSnapshot] = Field(default_factory=list)
    available: bool = False
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _availability_matches_content(self) -> MarketContext:
        if self.available and self.broad_index is None and not self.reference_snapshots:
            raise ValueError("market context claims to be available but carries no data")
        if not self.available and self.unavailable_reason is None:
            raise ValueError("unavailable market context must say why")
        return self


# ---------------------------------------------------------------------------
# The agent's entire view of the world
# ---------------------------------------------------------------------------
class ResearchInput(ImmutableModel):
    """Everything the Market Researcher agent is given about one underlying.

    If a fact is not here, the agent does not have it and must not claim it.
    There is deliberately no repository handle, no provider, no broker, no
    filesystem path and no network client on this object: the agent is a
    consumer of validated, point-in-time data, and this contract is where that
    stops being a convention.

    Every item under ``news``, ``events``, ``regulatory_events``,
    ``fundamentals``, ``market_snapshot``, ``historical_context``,
    ``option_context`` and ``market_context`` was visible at ``as_of`` under
    the data layer's rules — retrieval binds, so a filing downloaded today is
    invisible to a reconstruction of last week.
    """

    run_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    horizon: ResearchHorizon
    schema_version: Identifier = RESEARCH_SCHEMA_VERSION

    universe_run_id: Identifier | None = None
    universe_snapshot_id: Identifier | None = None
    data_snapshot_ids: list[str] = Field(default_factory=list)

    market_snapshot: MarketSnapshot | None = None
    historical_context: HistoricalContext | None = None
    option_context: OptionMarketContext | None = None
    market_context: MarketContext | None = None

    news: list[EvidenceItem] = Field(default_factory=list)
    events: list[EventItem] = Field(default_factory=list)
    regulatory_events: list[EvidenceItem] = Field(default_factory=list)
    fundamentals: list[FundamentalItem] = Field(default_factory=list)
    #: Items derived from market, historical, option and context data. Kept
    #: alongside the retrieved documents so every citable fact has an id.
    observations: list[EvidenceItem] = Field(default_factory=list)

    data_quality_summary: ResearchDataQualitySummary
    window: ResearchWindowSnapshot
    limits: ResearchLimitsSnapshot
    source_policy: ResearchSourcePolicySnapshot

    @model_validator(mode="after")
    def _evidence_ids_are_unique(self) -> ResearchInput:
        """Two facts sharing an id would make a citation ambiguous."""
        ids = [item.evidence_id for item in self.all_evidence]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate evidence_id in research input: {duplicates}")
        event_ids = [event.event_id for event in self.events]
        duplicate_events = sorted({i for i in event_ids if event_ids.count(i) > 1})
        if duplicate_events:
            raise ValueError(f"duplicate event_id in research input: {duplicate_events}")
        return self

    @property
    def all_evidence(self) -> list[EvidenceItem]:
        """Every citable fact, in a stable order."""
        return [*self.news, *self.regulatory_events, *self.observations]

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.all_evidence)

    @property
    def event_ids(self) -> frozenset[str]:
        return frozenset(event.event_id for event in self.events)

    @property
    def usable_evidence_count(self) -> int:
        return sum(1 for item in self.all_evidence if item.research_usable)

    def evidence(self, evidence_id: str) -> EvidenceItem | None:
        return next((i for i in self.all_evidence if i.evidence_id == evidence_id), None)

    def event(self, event_id: str) -> EventItem | None:
        return next((e for e in self.events if e.event_id == event_id), None)

    def events_in_horizon(self) -> list[EventItem]:
        return [event for event in self.events if event.within_horizon]

    def distinct_sources(self) -> set[str]:
        return {
            item.source.source_name or item.source.provider
            for item in self.all_evidence
            if item.research_usable
        }

    def best_tier(self) -> SourceTier | None:
        """The most trusted tier among usable evidence, or ``None`` if empty."""
        tiers = [i.source.source_tier for i in self.all_evidence if i.research_usable]
        return min(tiers, key=tier_rank) if tiers else None


# ---------------------------------------------------------------------------
# Agent output
# ---------------------------------------------------------------------------
class AgentEvidenceAssessment(ImmutableModel):
    """The agent's reading of one supplied fact.

    ``evidence_id`` must name an item the input actually contained; there is no
    field here for a source name, a URL or a publication date, so the model has
    nowhere to put an invented one. ``claim`` is the agent's sentence about the
    fact, and it is stored next to the fact rather than instead of it.

    ``direction`` says what the fact points at; ``stance`` says how it relates
    to the thesis the report states. The two are separate because an item can
    point up and still contradict a bearish thesis, and both readings belong in
    the record.
    """

    evidence_id: Identifier
    claim: str = Field(min_length=1)
    direction: EvidenceDirection
    stance: EvidenceStance
    relevance: RelevanceLevel
    confidence: ConfidenceLevel


class AgentEventAssessment(ImmutableModel):
    """The agent's reading of one supplied event.

    Timing, source and announcement are *not* here: they are facts the input
    carried and the report copies them across. What the agent contributes is
    how much the event matters and whether its direction can be called.
    """

    event_id: Identifier
    expected_relevance: RelevanceLevel
    directional_uncertainty: bool
    rationale: str | None = None


class SupportedClaim(ImmutableModel):
    """Base for a stated claim whose backing is derived, never declared.

    ``support`` is recomputed from ``evidence_ids`` on every validation, and any
    value supplied for it is discarded. A claim that could label itself
    ``SUPPORTED`` while citing nothing would make the label worthless — and the
    label is the only thing separating "the filing says so" from "the model
    thinks so".

    This is not the same as repairing a judgement. ``support`` is a mechanical
    function of what was cited, so deriving it changes nothing the agent
    decided; an unsupported claim is still stored, still visible, and still
    attributed to the agent.
    """

    evidence_ids: list[str] = Field(default_factory=list)
    support: ClaimSupport = ClaimSupport.UNSUPPORTED

    @model_validator(mode="before")
    @classmethod
    def _derive_support(cls, data: Any) -> Any:
        if isinstance(data, dict):
            cited = data.get("evidence_ids") or []
            return {
                **data,
                "support": ClaimSupport.SUPPORTED if cited else ClaimSupport.UNSUPPORTED,
            }
        return data

    @property
    def is_supported(self) -> bool:
        return self.support is ClaimSupport.SUPPORTED


class Catalyst(SupportedClaim):
    """One thing that could push the underlying, with what backs it.

    A catalyst with no evidence is kept and labelled ``UNSUPPORTED`` rather
    than dropped: what the agent asserted without backing is itself worth
    seeing, and deleting it would hide the assertion instead of qualifying it.
    """

    summary: str = Field(min_length=1)


class RiskAssessment(SupportedClaim):
    """One risk, in a named category (brief section 20).

    An unavailable assessment is stated as such rather than omitted: a silence
    reads as "no risk", which is never what the evidence said.
    """

    category: RiskCategory
    description: str = Field(min_length=1)


class InvalidationCondition(SupportedClaim):
    """What would falsify the thesis (brief section 18).

    Mandatory on every successful report. A thesis that cannot be invalidated
    cannot be monitored, and Milestone 9's thesis monitor consumes exactly this
    list — which is why the condition has to be observable rather than
    rhetorical.
    """

    condition: str = Field(min_length=1)
    #: What one would have to look at to decide. Free text, but the field
    #: exists so "the story changes" cannot pass as a monitoring instruction.
    observable: str | None = None


class ResearchAgentOutput(ImmutableModel):
    """The agent's whole structured response for one underlying.

    Schema-valid is not the same as acceptable: this object can be well-formed
    and still be rejected by :mod:`trading_system.research.validation` for
    citing an evidence id the input never contained, for claiming a hypothesis
    its own evidence does not support, or for a confidence the data does not
    license.

    There is no field here for a strike, an expiry, a right, a delta, a
    strategy, a quantity or an amount of money. That is the point.
    """

    run_id: Identifier
    symbol: Ticker
    hypothesis: MarketHypothesis
    confidence: ConfidenceLevel
    direction: Direction
    expected_magnitude: ExpectedMagnitude
    horizon_days: int = Field(ge=1, le=365)

    thesis: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    explanation: str | None = None

    evidence: list[AgentEvidenceAssessment] = Field(default_factory=list)
    key_events: list[AgentEventAssessment] = Field(default_factory=list)
    bullish_catalysts: list[Catalyst] = Field(default_factory=list)
    bearish_catalysts: list[Catalyst] = Field(default_factory=list)
    risks: list[RiskAssessment] = Field(default_factory=list)
    invalidation_conditions: list[InvalidationCondition] = Field(min_length=1)

    #: Why one side of a disagreement currently dominates. Required by the
    #: confidence policy before a HIGH may stand over contradicting evidence.
    contradiction_resolution: str | None = None
    #: What the agent would have wanted and did not get (brief section 54.9).
    missing_information: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _explanation_required_for_other(self) -> ResearchAgentOutput:
        if self.hypothesis is MarketHypothesis.E and not (self.explanation or "").strip():
            raise ValueError(
                "hypothesis E requires a structured explanation; 'other' without a "
                "reason is not an outlook"
            )
        return self

    @property
    def cited_evidence_ids(self) -> list[str]:
        """Every evidence id referenced anywhere in the response."""
        cited = [item.evidence_id for item in self.evidence]
        for claim in (*self.bullish_catalysts, *self.bearish_catalysts):
            cited.extend(claim.evidence_ids)
        for risk in self.risks:
            cited.extend(risk.evidence_ids)
        for condition in self.invalidation_conditions:
            cited.extend(condition.evidence_ids)
        return cited

    def evidence_with(self, direction: EvidenceDirection) -> list[AgentEvidenceAssessment]:
        return [item for item in self.evidence if item.direction is direction]

    @property
    def contradicting(self) -> list[AgentEvidenceAssessment]:
        return [i for i in self.evidence if i.stance is EvidenceStance.CONTRADICTS]


# ---------------------------------------------------------------------------
# The canonical report
# ---------------------------------------------------------------------------
class ReportedEvidence(ImmutableModel):
    """One evidence item as it appears in the stored report.

    The *facts* — source, tier, publication and retrieval time, snapshot id —
    are copied from the input, not from the model. The *reading* — claim,
    direction, stance, relevance, confidence — is the agent's. Keeping them in
    one object with distinct provenance is what makes "why did the agent
    conclude this" answerable without trusting the agent's own account of what
    it was shown.
    """

    evidence_id: Identifier
    kind: EvidenceKind
    claim: str = Field(min_length=1)
    #: The input's own neutral description of the fact, preserved verbatim.
    fact: str = Field(min_length=1)
    direction: EvidenceDirection
    stance: EvidenceStance
    relevance: RelevanceLevel
    confidence: ConfidenceLevel
    source: SourceProvenance
    occurred_at: UtcDatetime | None = None
    duplicate_count: int = Field(default=0, ge=0)
    duplicate_source_names: list[str] = Field(default_factory=list)

    @property
    def source_tier(self) -> SourceTier:
        return self.source.source_tier


class ReportedEvent(ImmutableModel):
    """A material event as it appears in the stored report (brief section 57).

    Every timing and provenance field comes from the input. ``D`` requires all
    of them, which is checked deterministically: an event with no
    ``announced_at`` cannot support an event-driven hypothesis, because nothing
    establishes that the event was knowable at the research instant.
    """

    event_id: Identifier
    event_type: MarketEventType
    summary: str = Field(min_length=1)
    expected_event_time: UtcDatetime
    announced_at: UtcDatetime | None = None
    confirmed: bool = False
    days_until: int | None = None
    within_horizon: bool = False
    expected_relevance: RelevanceLevel
    directional_uncertainty: bool
    rationale: str | None = None
    source: SourceProvenance

    @property
    def source_tier(self) -> SourceTier:
        return self.source.source_tier

    @property
    def is_complete_for_event_hypothesis(self) -> bool:
        """Whether this event carries every field hypothesis ``D`` demands."""
        return self.announced_at is not None and self.within_horizon


class ResearchAgentMetadata(ImmutableModel):
    """Which model produced an outlook, precisely enough to reproduce the call.

    ``prompt_version`` and ``prompt_fingerprint`` are both recorded because they
    fail differently: the version is what an operator declares, the fingerprint
    is what the file actually contained. A prompt edited without a version bump
    is exactly the change that would otherwise be invisible in the audit trail.
    """

    model_provider: Identifier
    model_name: Identifier
    prompt_version: Identifier
    generated_at: UtcDatetime
    model_version: str | None = None
    prompt_fingerprint: str | None = None
    agent_version: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    #: The model's exact response, kept as evidence. The validated fields
    #: elsewhere are canonical; this is never re-parsed into a decision.
    raw_response: str | None = None


class MarketResearchReport(ImmutableModel):
    """The canonical, immutable outlook for one underlying at one instant.

    Written once and never rewritten. A later run produces a new report rather
    than replacing this one, which is what makes "what did the system believe
    on 11 August, on what evidence, and was it later invalidated" answerable
    after the configuration, the data and the model have all moved on.

    Distinct from :class:`~trading_system.domain.models.ResearchReport`, which
    is the narrow Milestone 1 workflow boundary the strategy stage consumes.
    This is the audit record; :meth:`to_research_report` projects onto that
    boundary, exactly as a universe run projects onto ``UniverseSelection``.

    A report whose ``status`` is not ``SUCCESS`` carries no hypothesis, no
    confidence and no catalysts. That is enforced below, because a failure that
    could still hand downstream a market view would have turned an outage into
    a trading opinion.
    """

    report_id: Identifier
    run_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    generated_at: UtcDatetime
    horizon: ResearchHorizon
    status: ResearchStatus
    schema_version: Identifier = RESEARCH_SCHEMA_VERSION

    hypothesis: MarketHypothesis | None = None
    confidence: ConfidenceLevel | None = None
    direction: Direction | None = None
    expected_magnitude: ExpectedMagnitude | None = None
    horizon_days: int | None = Field(default=None, ge=1, le=365)

    thesis: str | None = None
    expected_behavior: str | None = None
    explanation: str | None = None

    evidence: list[ReportedEvidence] = Field(default_factory=list)
    key_events: list[ReportedEvent] = Field(default_factory=list)
    bullish_catalysts: list[Catalyst] = Field(default_factory=list)
    bearish_catalysts: list[Catalyst] = Field(default_factory=list)
    risks: list[RiskAssessment] = Field(default_factory=list)
    invalidation_conditions: list[InvalidationCondition] = Field(default_factory=list)
    contradiction_resolution: str | None = None
    missing_information: list[str] = Field(default_factory=list)

    data_quality: ResearchDataQualitySummary
    window: ResearchWindowSnapshot
    limits: ResearchLimitsSnapshot
    source_policy: ResearchSourcePolicySnapshot

    universe_run_id: Identifier | None = None
    universe_snapshot_id: Identifier | None = None
    input_snapshot_ids: list[str] = Field(default_factory=list)
    agent_metadata: ResearchAgentMetadata | None = None
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _failure_carries_no_view(self) -> MarketResearchReport:
        """A report that did not succeed states no outlook, in any field."""
        if self.status.produced_an_outlook:
            missing = [
                name
                for name, value in (
                    ("hypothesis", self.hypothesis),
                    ("confidence", self.confidence),
                    ("direction", self.direction),
                    ("expected_magnitude", self.expected_magnitude),
                    ("horizon_days", self.horizon_days),
                    ("thesis", self.thesis),
                    ("expected_behavior", self.expected_behavior),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"a SUCCESS report must state {', '.join(missing)}")
            if not self.invalidation_conditions:
                raise ValueError(
                    "a SUCCESS report must state at least one invalidation condition; "
                    "a thesis that cannot be falsified cannot be monitored"
                )
            if self.horizon_days is not None and not self.horizon.contains(self.horizon_days):
                raise ValueError(
                    f"horizon_days {self.horizon_days} is outside the configured "
                    f"{self.horizon.min_days}-{self.horizon.max_days} day window"
                )
            return self

        stated = [
            name
            for name, value in (
                ("hypothesis", self.hypothesis),
                ("confidence", self.confidence),
                ("direction", self.direction),
                ("expected_magnitude", self.expected_magnitude),
                ("thesis", self.thesis),
                ("expected_behavior", self.expected_behavior),
            )
            if value is not None
        ]
        if stated or self.bullish_catalysts or self.bearish_catalysts:
            raise ValueError(
                f"status {self.status.value} must not carry an outlook "
                f"({', '.join(stated) or 'catalysts'}); a failure is never a market view"
            )
        return self

    @model_validator(mode="after")
    def _hypothesis_e_requires_explanation(self) -> MarketResearchReport:
        if self.hypothesis is MarketHypothesis.E and not (self.explanation or "").strip():
            raise ValueError("hypothesis E requires a structured explanation")
        return self

    @property
    def succeeded(self) -> bool:
        return self.status is ResearchStatus.SUCCESS

    @property
    def supporting_evidence(self) -> list[ReportedEvidence]:
        return [e for e in self.evidence if e.stance is EvidenceStance.SUPPORTS]

    @property
    def contradicting_evidence(self) -> list[ReportedEvidence]:
        return [e for e in self.evidence if e.stance is EvidenceStance.CONTRADICTS]

    @property
    def sources(self) -> list[SourceProvenance]:
        """Provenance of every cited fact, deduplicated and ordered by trust."""
        seen: dict[str, SourceProvenance] = {}
        for item in self.evidence:
            seen.setdefault(
                item.source.snapshot_id + (item.source.source_identifier or ""), item.source
            )
        for event in self.key_events:
            key = event.source.snapshot_id + (event.source.source_identifier or "")
            seen.setdefault(key, event.source)
        return sorted(
            seen.values(), key=lambda s: (tier_rank(s.source_tier), s.source_name or s.provider)
        )

    def to_research_report(self) -> ResearchReport:
        """Project onto the Milestone 1 workflow boundary the strategy stage consumes.

        The full record is the audit artifact; this is the narrow contract the
        next stage was built against (``schemas/research_report.json``).

        The boundary's ``confidence`` is a float, so the band is mapped through
        :data:`CONFIDENCE_BAND_VALUE`. That number is a band representative, not
        a probability, and nothing downstream may read it as one.

        Raises :class:`ValueError` for a report that did not produce an
        outlook: there is deliberately no way to hand a failed research run
        across the boundary as though it were a view.
        """
        if not self.succeeded:
            raise ValueError(
                f"cannot project a {self.status.value} report onto the strategy boundary; "
                f"a run that produced no outlook has nothing to hand on"
            )
        assert self.hypothesis is not None  # narrowing; guaranteed by the validator
        assert self.confidence is not None
        assert self.direction is not None
        assert self.expected_magnitude is not None
        assert self.horizon_days is not None

        return ResearchReport(
            report_id=self.report_id,
            ticker=self.symbol,
            as_of=self.as_of,
            hypothesis=self.hypothesis,
            direction=self.direction,
            expected_magnitude=self.expected_magnitude,
            confidence=CONFIDENCE_BAND_VALUE[self.confidence],
            expected_horizon_days=self.horizon_days,
            catalysts=[c.summary for c in (*self.bullish_catalysts, *self.bearish_catalysts)],
            invalidation_conditions=[c.condition for c in self.invalidation_conditions],
            evidence=[e.claim for e in self.evidence],
            sources=[s.to_source_reference() for s in self.sources],
            explanation=self.explanation,
            versions=self.versions,
        )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
class ResearchRunCounts(ImmutableModel):
    """How many underlyings reached each outcome.

    Every field is derived from the run's own reports, which is why elapsed
    time is deliberately *not* here: a measured duration is a fact about the
    machine, not about the decision, and storing it inside a content-addressed
    record would make two otherwise identical runs differ.
    """

    universe_assets: int = Field(default=0, ge=0)
    researched: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    insufficient_evidence: int = Field(default=0, ge=0)
    no_data: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class ResearchRunResult(ImmutableModel):
    """The immutable record of one research run.

    One report per underlying, never a single merged answer for several
    (brief section 33): a shared context lets one asset's story colour another's,
    and the resulting outlook could not be attributed to either.
    """

    run_id: Identifier
    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: ResearchStatus
    horizon: ResearchHorizon
    schema_version: Identifier = RESEARCH_SCHEMA_VERSION

    universe_run_id: Identifier | None = None
    universe_snapshot_id: Identifier | None = None
    reports: list[MarketResearchReport] = Field(default_factory=list)
    counts: ResearchRunCounts = Field(default_factory=ResearchRunCounts)
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _reports_are_one_per_symbol(self) -> ResearchRunResult:
        symbols = [report.symbol for report in self.reports]
        duplicates = sorted({s for s in symbols if symbols.count(s) > 1})
        if duplicates:
            raise ValueError(f"one report per underlying per run; duplicated: {duplicates}")
        wrong_run = sorted({r.symbol for r in self.reports if r.run_id != self.run_id})
        if wrong_run:
            raise ValueError(f"reports from another run: {wrong_run}")
        return self

    @property
    def successful_reports(self) -> list[MarketResearchReport]:
        return [report for report in self.reports if report.succeeded]

    @property
    def symbols(self) -> list[str]:
        return [report.symbol for report in self.reports]

    def report(self, symbol: str) -> MarketResearchReport | None:
        wanted = symbol.strip().upper()
        return next((r for r in self.reports if r.symbol == wanted), None)


# ---------------------------------------------------------------------------
# Derived identifiers
# ---------------------------------------------------------------------------
def evidence_identifier(
    *,
    kind: EvidenceKind,
    symbol: str,
    snapshot_id: str,
    source_identifier: str | None,
    content: str,
) -> str:
    """Derive an evidence id from the fact it identifies.

    Derived rather than generated so that the same stored record always yields
    the same id: an agent's citation can then be checked against the input, and
    a re-run over unchanged data produces byte-identical input.
    """
    digest = stable_hash(
        [
            "RESEARCH_EVIDENCE",
            kind.value,
            symbol.upper(),
            snapshot_id,
            source_identifier or "",
            content,
        ]
    )
    return f"ev-{kind.value.lower()}-{digest[:16]}"


def report_identifier(
    *,
    run_id: str,
    symbol: str,
    as_of: datetime,
    status: ResearchStatus,
    hypothesis: MarketHypothesis | None,
    confidence: ConfidenceLevel | None,
    evidence_ids: list[str],
    schema_version: str = RESEARCH_SCHEMA_VERSION,
) -> str:
    """Derive a report's identifier from its content.

    Deliberately excludes every runtime measurement — latency, token counts,
    wall-clock duration — so two runs over identical inputs producing an
    identical outlook produce an identical id. The same reasoning keeps
    observation clocks out of the data layer's payload hashes.
    """
    digest = stable_hash(
        [
            "RESEARCH_REPORT",
            schema_version,
            run_id,
            symbol.upper(),
            as_of.isoformat(),
            status.value,
            hypothesis.value if hypothesis else "",
            confidence.value if confidence else "",
            sorted(evidence_ids),
        ]
    )
    return f"research-{symbol.upper()}-{digest[:16]}"


def decimal_or_none(value: float | None, *, places: int = 6) -> Decimal | None:
    """Quantise a computed statistic to an exact decimal, or keep it absent.

    Computed statistics arrive as binary floats; storing one directly would put
    binary floating point into an artifact the rest of the system treats as
    exact. Rounding to a fixed number of places also makes the value stable
    across platforms, which reproducibility depends on.
    """
    if value is None:
        return None
    return Decimal(f"{value:.{places}f}")
