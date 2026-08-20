"""Canonical universe-selection models.

Three artifacts, each with a JSON schema in ``schemas/``:

:class:`UniverseSelectionInput`
    What the deterministic pre-filter produces and the **only** thing the agent
    is given. It carries evidence the data layer already retrieved, normalised
    and quality-assessed — never a provider payload, never a broker handle.
:class:`UniverseAgentRanking`
    The agent's structured response. Prose is not a valid response.
:class:`UniverseSelectionResult`
    The immutable record of one run: what was considered, what survived, how it
    was ranked, which model ranked it, and which data snapshots it rests on.

Two properties are structural rather than conventional:

* **Underlyings only.** There is no field anywhere here for a strike, an
  expiry, a right, a strategy, a direction or an amount of money to spend. The
  universe selector answers "which underlying assets deserve deeper research",
  and the shape of these models is what stops it answering anything else.
* **Every asset is explainable.** A selected asset carries its deterministic
  eligibility, its reason codes and the provenance of the evidence behind it; a
  rejected asset carries a machine-readable reason. Neither can be constructed
  without them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.data.models import DataQualityReport
from trading_system.domain.enums import (
    ConfidenceLevel,
    DataQuality,
    MarketDataOrigin,
    Optionability,
    SecurityType,
    SelectionMethod,
    SourceTier,
    UniverseEligibility,
    UniverseRejectionReason,
    UniverseSelectionReason,
    UniverseSelectionStatus,
    UniverseSourceKind,
)
from trading_system.domain.models import (
    Identifier,
    ImmutableModel,
    Money,
    SystemVersions,
    Ticker,
    UniverseCandidate,
    UniverseSelection,
    UtcDatetime,
)

__all__ = [
    "UNIVERSE_SCHEMA_VERSION",
    "AgentMetadata",
    "CandidateAsset",
    "CandidateProvenance",
    "DataQualitySummary",
    "FilterConfigSnapshot",
    "PriceField",
    "RejectedAsset",
    "SelectedAsset",
    "UniverseAgentRanking",
    "UniverseAssetRanking",
    "UniverseRunCounts",
    "UniverseSelectionInput",
    "UniverseSelectionResult",
    "UniverseSourceRef",
    "universe_snapshot_id",
]

#: Version of the universe artifact shapes. Stamped onto every stored run so a
#: past universe can never silently change meaning after a schema change.
UNIVERSE_SCHEMA_VERSION = "1.0.0"


class PriceField:
    """Which quote field a reference price was taken from.

    Recorded rather than assumed: "the price" is ambiguous when a quote has a
    last, a close and a midpoint that disagree, and a threshold comparison
    against an unrecorded field cannot be audited afterwards.
    """

    LAST = "LAST"
    CLOSE = "CLOSE"
    MID = "MID"
    BID = "BID"
    ASK = "ASK"


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------
class UniverseSourceRef(ImmutableModel):
    """Where the candidate pool came from, explicitly and with a version."""

    kind: UniverseSourceKind
    name: Identifier
    version: Identifier
    symbol_count: int = Field(default=0, ge=0)
    location: str | None = None


class DataQualitySummary(ImmutableModel):
    """The data layer's verdict, carried through rather than recomputed.

    A projection of :class:`~trading_system.data.models.DataQualityReport`: the
    dimensions a ranking decision can legitimately reference. Nothing here is
    re-derived — the quality engine owns the judgement, and this only transports
    it to the agent.
    """

    research_usable: bool
    classification: DataQuality
    freshness_valid: bool = True
    completeness_valid: bool = True
    plausibility_valid: bool = True
    consistency_valid: bool = True
    issues: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, report: DataQualityReport) -> DataQualitySummary:
        return cls(
            research_usable=report.research_usable,
            classification=report.classification,
            freshness_valid=report.freshness_valid,
            completeness_valid=report.completeness_valid,
            plausibility_valid=report.plausibility_valid,
            consistency_valid=report.consistency_valid,
            issues=[issue.value for issue in report.issues],
        )


class CandidateProvenance(ImmutableModel):
    """Who actually produced a candidate's evidence, and when we had it.

    ``provider`` always names whoever answered; ``requested_provider`` records
    who was asked and did not. A candidate can never claim a source that was not
    consulted.
    """

    provider: Identifier
    retrieved_at: UtcDatetime
    requested_provider: Identifier | None = None
    source_tier: SourceTier | None = None
    #: The immutable data snapshots this candidate was derived from.
    snapshot_ids: list[str] = Field(default_factory=list)


class FilterConfigSnapshot(ImmutableModel):
    """The filter thresholds actually applied, echoed into the stored run.

    A run that referred to "the configuration" would stop explaining itself the
    moment that configuration changed.
    """

    allowed_security_types: list[str] = Field(default_factory=list)
    allowed_currencies: list[str] = Field(default_factory=list)
    allowed_exchanges: list[str] = Field(default_factory=list)
    min_price: Money | None = None
    min_average_daily_volume: int | None = Field(default=None, ge=0)
    max_data_age_seconds: int | None = Field(default=None, ge=0)
    require_research_usable: bool = True
    optionability_policy: str = "REQUIRED"
    exclusions: list[str] = Field(default_factory=list)
    max_candidates: int = Field(ge=0)
    max_selected_assets: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Agent input
# ---------------------------------------------------------------------------
class CandidateAsset(ImmutableModel):
    """One underlying, with the evidence a ranking may legitimately use.

    Every measured field is optional and ``None`` means *not available* — the
    data layer's rule, carried forward. ``underlying_volume`` is deliberately
    named: it is share volume for the underlying, and it does not establish
    that the underlying's *options* are liquid. That needs option-level data
    this milestone does not collect.

    ``underlying_volume`` and ``average_daily_volume`` are two different
    observations and are never merged. The first is the current session's
    cumulative volume as the broker reported it — from IBKR's delayed feed it
    may well be the corrupted tick 74, preserved and flagged rather than
    repaired. The second is the trailing 90-day average (IBKR tick 21) and is
    the only one the liquidity floor reads.
    """

    symbol: Ticker
    security_type: SecurityType
    currency: str = Field(min_length=1)
    deterministic_eligibility: UniverseEligibility
    optionability: Optionability
    data_quality: DataQualitySummary
    source: CandidateProvenance

    exchange: str | None = None
    reference_price: Money | None = None
    reference_price_field: str | None = None
    underlying_volume: Money | None = None
    average_daily_volume: Money | None = None
    market_data_as_of: UtcDatetime | None = None
    market_data_age_seconds: float | None = Field(default=None, ge=0)
    market_data_origin: MarketDataOrigin | None = None
    option_expiration_count: int | None = Field(default=None, ge=0)
    option_strike_count: int | None = Field(default=None, ge=0)
    research_usable: bool = True

    @property
    def has_volume(self) -> bool:
        return self.underlying_volume is not None


class UniverseSelectionInput(ImmutableModel):
    """The agent's entire view of the world.

    If a fact is not here, the agent does not have it and must not claim it.
    There is deliberately no repository handle, no provider, no broker and no
    filesystem path on this object: the agent is a consumer of validated data,
    and the input contract is where that stops being a convention.
    """

    run_id: Identifier
    as_of: UtcDatetime
    universe_source: UniverseSourceRef
    deterministic_filter_config: FilterConfigSnapshot
    schema_version: Identifier = UNIVERSE_SCHEMA_VERSION
    candidate_assets: list[CandidateAsset] = Field(default_factory=list)
    data_snapshot_ids: list[str] = Field(default_factory=list)
    max_selected_assets: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _candidates_are_eligible_and_unique(self) -> UniverseSelectionInput:
        """Only eligible, deduplicated candidates may be shown to the agent.

        Checked here rather than trusted from the caller: an ineligible asset
        that reached the agent could be ranked, and the whole point of the
        deterministic layer is that it cannot.
        """
        symbols = [c.symbol for c in self.candidate_assets]
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate symbol in universe selection input")
        ineligible = [
            c.symbol
            for c in self.candidate_assets
            if c.deterministic_eligibility is not UniverseEligibility.ELIGIBLE
        ]
        if ineligible:
            raise ValueError(
                f"deterministically rejected assets must never reach the agent: {ineligible}"
            )
        return self

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(c.symbol for c in self.candidate_assets)

    def candidate(self, symbol: str) -> CandidateAsset | None:
        return next((c for c in self.candidate_assets if c.symbol == symbol), None)


# ---------------------------------------------------------------------------
# Agent output
# ---------------------------------------------------------------------------
class UniverseAssetRanking(ImmutableModel):
    """The agent's verdict on one candidate.

    ``reasons`` are codes from a closed vocabulary, never prose: an agent that
    could invent a reason could justify anything, and
    :mod:`trading_system.universe.validation` checks each code against the
    candidate's own evidence.
    """

    symbol: Ticker
    selection: str
    reasons: list[UniverseSelectionReason] = Field(min_length=1)
    confidence: ConfidenceLevel
    rank: int | None = Field(default=None, ge=1)
    selection_score: float | None = Field(default=None, ge=0.0, le=100.0)
    rationale: str | None = None

    @model_validator(mode="after")
    def _rank_matches_selection(self) -> UniverseAssetRanking:
        if self.selection not in ("SELECTED", "NOT_SELECTED"):
            raise ValueError(f"selection must be SELECTED or NOT_SELECTED, got {self.selection!r}")
        if self.selection == "SELECTED" and self.rank is None:
            raise ValueError(f"{self.symbol}: SELECTED requires a rank")
        if self.selection == "NOT_SELECTED" and self.rank is not None:
            raise ValueError(f"{self.symbol}: NOT_SELECTED must not carry a rank")
        return self

    @property
    def is_selected(self) -> bool:
        return self.selection == "SELECTED"


class UniverseAgentRanking(ImmutableModel):
    """The agent's whole structured response.

    Schema-valid is not the same as acceptable: this object can be well-formed
    and still be rejected by the deterministic post-validation for naming an
    unknown symbol, duplicating a rank, exceeding the size limit or claiming
    evidence the input contradicts.
    """

    run_id: Identifier
    rankings: list[UniverseAssetRanking] = Field(default_factory=list)
    #: Free text, retained for audit only. Never parsed; cannot change an outcome.
    notes: str | None = None

    @property
    def selected(self) -> list[UniverseAssetRanking]:
        return sorted(
            (r for r in self.rankings if r.is_selected),
            key=lambda r: (r.rank if r.rank is not None else 0, r.symbol),
        )

    @property
    def not_selected(self) -> list[UniverseAssetRanking]:
        return [r for r in self.rankings if not r.is_selected]


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------
class SelectedAsset(ImmutableModel):
    """A research-ready underlying, with everything needed to explain it."""

    symbol: Ticker
    rank: int = Field(ge=1)
    deterministic_eligibility: UniverseEligibility
    reasons: list[UniverseSelectionReason] = Field(min_length=1)
    data_quality: DataQualitySummary
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    selection_score: float | None = Field(default=None, ge=0.0, le=100.0)
    rationale: str | None = None
    optionability: Optionability = Optionability.UNKNOWN
    reference_price: Money | None = None
    underlying_volume: Money | None = None
    average_daily_volume: Money | None = None
    source: CandidateProvenance | None = None

    @model_validator(mode="after")
    def _selected_assets_carry_provenance(self) -> SelectedAsset:
        """A selected asset without provenance is an unexplainable decision.

        Milestone 4 brief section 36: every selected asset must be traceable to
        the data it rests on.
        """
        if self.source is None or not self.source.snapshot_ids:
            raise ValueError(
                f"{self.symbol}: a selected asset must name the data snapshots it rests on"
            )
        return self


class RejectedAsset(ImmutableModel):
    """Something considered and not selected, with a machine-readable reason."""

    symbol: Ticker
    deterministic_eligibility: UniverseEligibility
    reason: UniverseRejectionReason
    detail: str | None = None
    optionability: Optionability = Optionability.UNKNOWN
    source: CandidateProvenance | None = None


class AgentMetadata(ImmutableModel):
    """Which model produced a ranking.

    Required for reproducibility (Milestone 4 brief section 20): a universe
    whose author is unknown cannot be re-derived or compared against a later
    one. ``None`` on the run means no model was involved, which must be visible
    rather than inferred from a missing field.
    """

    model_provider: Identifier
    model_name: Identifier
    prompt_version: Identifier
    generated_at: UtcDatetime
    model_version: str | None = None
    agent_version: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    #: The model's exact response, kept as evidence. The validated fields
    #: elsewhere are canonical; this is never re-parsed into a decision.
    raw_response: str | None = None


class UniverseRunCounts(ImmutableModel):
    """How many assets reached each stage (Milestone 4 brief section 41).

    Every field is derived from the run's inputs, which is why elapsed time is
    deliberately *not* here: a measured duration is a fact about the machine,
    not about the decision, and storing it inside a content-addressed record
    would make two otherwise identical runs differ. Duration is logged and
    returned on :class:`~trading_system.universe.service.UniverseRun` instead —
    the same reasoning that keeps observation clocks out of the data layer's
    payload hashes.
    """

    candidates: int = Field(default=0, ge=0)
    deterministic_pass: int = Field(default=0, ge=0)
    deterministic_reject: int = Field(default=0, ge=0)
    ai_input: int = Field(default=0, ge=0)
    ai_selected: int = Field(default=0, ge=0)
    final: int = Field(default=0, ge=0)


class UniverseSelectionResult(ImmutableModel):
    """The immutable record of one universe run.

    Written once and never rewritten; a later run appends a new record rather
    than replacing this one. That is what makes "what was considered on date T,
    why was NVDA selected, why was AAPL rejected, which data was available, and
    which model ranked it" answerable months later without consulting an LLM's
    memory.
    """

    snapshot_id: Identifier
    run_id: Identifier
    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: UniverseSelectionStatus
    selection_method: SelectionMethod
    universe_source: UniverseSourceRef
    deterministic_filter_config: FilterConfigSnapshot
    versions: SystemVersions
    schema_version: Identifier = UNIVERSE_SCHEMA_VERSION

    selected_assets: list[SelectedAsset] = Field(default_factory=list)
    rejected_assets: list[RejectedAsset] = Field(default_factory=list)
    agent_metadata: AgentMetadata | None = None
    input_snapshot_ids: list[str] = Field(default_factory=list)
    counts: UniverseRunCounts = Field(default_factory=UniverseRunCounts)
    status_detail: str | None = None

    @model_validator(mode="after")
    def _selection_is_well_formed(self) -> UniverseSelectionResult:
        """Ranks are unique and contiguous, and a failed run selects nothing.

        The second rule is the load-bearing one: a run that could report
        ``AI_UNAVAILABLE`` while still handing downstream a universe would have
        turned a failure into a trade candidate.
        """
        symbols = [a.symbol for a in self.selected_assets]
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate symbol in selected assets")
        ranks = sorted(a.rank for a in self.selected_assets)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("selected asset ranks must be unique and contiguous from 1")
        if not self.status.produced_a_universe and self.selected_assets:
            raise ValueError(
                f"status {self.status.value} must not carry selected assets; "
                f"a failed or empty run selects nothing"
            )
        if self.selection_method is SelectionMethod.AI_RANKED and self.agent_metadata is None:
            raise ValueError("AI_RANKED requires agent metadata naming the model that ranked")
        return self

    @property
    def symbols(self) -> list[str]:
        """The research-ready universe, in rank order."""
        return [a.symbol for a in sorted(self.selected_assets, key=lambda a: a.rank)]

    def to_universe_selection(self) -> UniverseSelection:
        """Project onto the Milestone 1 workflow boundary the researcher consumes.

        The full result is the audit record; this is the narrow contract the
        next stage was built against (``schemas/universe_selection.json``).
        Selection scores default to a rank-derived value only when the ranking
        supplied none, so a downstream consumer always has a comparable number
        without any stage inventing a confidence it was not given.
        """
        count = len(self.selected_assets)
        candidates = [
            UniverseCandidate(
                ticker=asset.symbol,
                rank=asset.rank,
                selection_score=(
                    asset.selection_score
                    if asset.selection_score is not None
                    else round(100.0 * (count - asset.rank + 1) / count, 4)
                ),
                rationale=asset.rationale,
            )
            for asset in sorted(self.selected_assets, key=lambda a: a.rank)
        ]
        return UniverseSelection(
            universe_id=self.snapshot_id,
            as_of=self.as_of,
            candidates=candidates,
            versions=self.versions,
        )


def universe_snapshot_id(
    *,
    run_id: str,
    as_of: datetime,
    source: UniverseSourceRef,
    filter_config: FilterConfigSnapshot,
    selected: list[SelectedAsset],
    schema_version: str = UNIVERSE_SCHEMA_VERSION,
) -> str:
    """Derive a run's identifier from its content.

    Derived rather than generated so that a re-run over identical inputs,
    configuration and ranking produces the same identifier — which is what makes
    the reproducibility claim checkable rather than asserted. ``run_id`` is
    included because two runs at the same instant over the same data are still
    two separate observations of the process.
    """
    return stable_hash(
        [
            "UNIVERSE_SELECTION",
            run_id,
            as_of.isoformat(),
            schema_version,
            source.kind.value,
            source.name,
            source.version,
            filter_config.model_dump(mode="json"),
            [
                [asset.symbol, asset.rank, [r.value for r in asset.reasons]]
                for asset in sorted(selected, key=lambda a: a.rank)
            ],
        ]
    )
