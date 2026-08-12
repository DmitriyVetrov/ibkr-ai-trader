"""Canonical strategy-selection and contract-selection models.

Four artifacts, two of which have a JSON schema in ``schemas/``:

:class:`StrategySelectionInput`
    Everything the strategy agent is given about one underlying, and the
    **only** thing it may reason from. A projection of a research report plus
    the metadata of the strategies that are eligible for its hypothesis.
:class:`StrategyAgentOutput`
    The agent's structured response: an action, a strategy, reason codes and a
    rationale. Prose is not a valid response.
:class:`StrategyDecisionRecord`
    The immutable record of one strategy decision
    (``schemas/strategy_selection.json``).
:class:`ContractSelectionResult`
    The immutable record of one deterministic contract selection
    (``schemas/contract_selection.json``).

The boundary this milestone exists to draw is enforced by the *shapes* here,
not by the prompt that accompanies them:

* **The agent cannot choose a contract.** There is no field anywhere on
  :class:`StrategySelectionInput` or :class:`StrategyAgentOutput` for a strike,
  an expiration, a right, a delta, a contract id, a quantity, a price or an
  amount of money. It is not shown a chain, so it cannot name a contract from
  one; it has nowhere to put a contract, so it cannot invent one. Tests assert
  the absence of every such field.
* **The agent cannot widen its own remit.** An eligible strategy is one the
  registry resolved for the report's hypothesis; :class:`StrategySelectionInput`
  refuses to carry a strategy that does not declare that hypothesis, so an
  ineligible strategy is not merely unlikely to be chosen — it is not
  expressible in the agent's input.
* **The contract selector cannot fabricate.** Every optional measurement on a
  selected leg is ``None`` when the data did not carry it, never a filled-in
  approximation, and a cost estimate that could not be computed says so instead
  of guessing a midpoint.
* **A failure carries no strategy.** A decision whose status is not ``SUCCESS``
  names no strategy, exactly as a failed research report carries no hypothesis.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    ClaimSupport,
    ConfidenceLevel,
    ContractRejectionReason,
    ContractSelectionStatus,
    DataQuality,
    DecisionMethod,
    Direction,
    ExpectedMagnitude,
    ExpirationSelectionPolicy,
    LegAction,
    MarketEventType,
    MarketHypothesis,
    OptionRight,
    RelevanceLevel,
    SecurityType,
    SourceTier,
    StrategyAction,
    StrategySelectionReason,
    StrategySelectionStatus,
    StrategyType,
    StrikeSelectionPolicy,
)
from trading_system.domain.models import (
    ContractSelection,
    Identifier,
    ImmutableModel,
    Money,
    OptionLeg,
    StrategyDecision,
    SystemVersions,
    Ticker,
    UtcDatetime,
)

__all__ = [
    "CONFIDENCE_ORDER",
    "STRATEGY_SCHEMA_VERSION",
    "ContractCostEstimate",
    "ContractRunCounts",
    "ContractSelectionResult",
    "ContractSelectionRunResult",
    "DataReadiness",
    "RejectedContract",
    "ResearchClaim",
    "ResearchEventSummary",
    "ResearchQualitySnapshot",
    "ResearchSummary",
    "SelectedLeg",
    "StrategyAgentMetadata",
    "StrategyAgentOutput",
    "StrategyDecisionRecord",
    "StrategyOption",
    "StrategyRunCounts",
    "StrategyRunResult",
    "StrategySelectionInput",
    "confidence_rank",
    "decision_identifier",
    "selection_identifier",
]

#: Version of the Milestone 6 artifact shapes. Stamped onto every stored record
#: so a past decision can never silently change meaning after a schema change.
STRATEGY_SCHEMA_VERSION = "1.0.0"

#: Ordering of the confidence bands, least to most. A band is not a
#: probability; the only property anything may rely on is this ordering.
CONFIDENCE_ORDER: tuple[ConfidenceLevel, ...] = (
    ConfidenceLevel.LOW,
    ConfidenceLevel.MEDIUM,
    ConfidenceLevel.HIGH,
)


def confidence_rank(level: ConfidenceLevel) -> int:
    """Position in the confidence ordering; higher is more confident."""
    return CONFIDENCE_ORDER.index(level)


# ---------------------------------------------------------------------------
# What the agent is shown
# ---------------------------------------------------------------------------
class StrategyOption(ImmutableModel):
    """One eligible strategy, as the agent sees it.

    Structure and window, never a contract: ``legs`` is ``["BUY CALL x1"]``, not
    a strike; ``dte_min``/``dte_max`` bound the window a later stage will choose
    an expiration *inside*, and naming that window is not naming an expiry.
    """

    strategy_id: StrategyType
    name: Identifier
    version: Identifier
    description: str = ""
    structure: str = ""
    applicable_hypotheses: list[MarketHypothesis] = Field(min_length=1)
    legs: list[str] = Field(min_length=1)
    leg_count: int = Field(ge=1)
    #: What the payoff expresses, not what any trade expects.
    directional_view: Direction
    single_position: bool = True
    aligns_to_events: bool = False
    dte_min: int = Field(ge=0)
    dte_max: int = Field(ge=0)


class ResearchClaim(ImmutableModel):
    """A catalyst, risk or invalidation condition, carried across as stated.

    ``support`` travels with it. A claim the research agent asserted without
    citing anything stays visible and stays labelled — the strategy stage does
    not get to launder it into a supported one by copying it.
    """

    summary: str = Field(min_length=1)
    support: ClaimSupport = ClaimSupport.UNSUPPORTED
    category: str | None = None


class ResearchEventSummary(ImmutableModel):
    """An event from the research report, as relative timing only.

    Deliberately carries **no date**. ``days_until`` and ``within_horizon`` are
    what distinguish an event-driven situation from a regime one, which is all
    this stage needs; a calendar date is what a later, deterministic stage
    aligns an expiration to, and it reads that from the research report itself.
    An agent shown no date cannot echo one into a rationale.

    The source, the announcement time and the snapshot stay on the research
    report. This stage neither re-cites nor re-derives them.
    """

    event_type: MarketEventType
    summary: str = Field(min_length=1)
    days_until: int | None = None
    within_horizon: bool = False
    expected_relevance: RelevanceLevel = RelevanceLevel.MEDIUM
    directional_uncertainty: bool = True


class ResearchQualitySnapshot(ImmutableModel):
    """What the data behind the outlook was like, as flags and counts.

    Carried through rather than recomputed. The strategy stage reads it to
    decide eligibility deterministically; it never re-judges the data layer.
    """

    research_usable: bool
    classification: DataQuality = DataQuality.OK
    evidence_count: int = Field(default=0, ge=0)
    supporting_count: int = Field(default=0, ge=0)
    contradicting_count: int = Field(default=0, ge=0)
    best_source_tier: SourceTier | None = None
    gaps: list[str] = Field(default_factory=list)


class ResearchSummary(ImmutableModel):
    """The research report as the strategy agent sees it.

    A projection, not the report: no evidence ids, no sources, no snapshots, no
    option context. The strategy stage does not re-do research and must not be
    able to — it reads a conclusion someone else is accountable for, and its
    only question is which configured strategy expresses that conclusion.
    """

    report_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    hypothesis: MarketHypothesis
    direction: Direction
    expected_magnitude: ExpectedMagnitude
    confidence: ConfidenceLevel
    horizon_days: int = Field(ge=1, le=365)
    thesis: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    explanation: str | None = None
    contradiction_resolution: str | None = None

    bullish_catalysts: list[ResearchClaim] = Field(default_factory=list)
    bearish_catalysts: list[ResearchClaim] = Field(default_factory=list)
    risks: list[ResearchClaim] = Field(default_factory=list)
    invalidation_conditions: list[ResearchClaim] = Field(default_factory=list)
    key_events: list[ResearchEventSummary] = Field(default_factory=list)
    data_quality: ResearchQualitySnapshot

    @property
    def events_in_horizon(self) -> list[ResearchEventSummary]:
        return [event for event in self.key_events if event.within_horizon]

    @property
    def has_material_event(self) -> bool:
        """A highly relevant event inside the horizon — what ``D`` rests on."""
        return any(
            event.expected_relevance is RelevanceLevel.HIGH for event in self.events_in_horizon
        )


class DataReadiness(ImmutableModel):
    """Whether the data a strategy needs was actually visible at ``as_of``.

    Established deterministically by the service, from the repository, *before*
    the agent is consulted. The agent never sees it and never sees the chain it
    describes: "is there an option chain" is a fact, and facts about data are
    not an AI decision.
    """

    option_chain_available: bool = False
    option_quotes_available: bool = False
    underlying_quote_available: bool = False
    expirations_visible: int = Field(default=0, ge=0)
    strikes_visible: int = Field(default=0, ge=0)
    chain_snapshot_id: str | None = None
    quote_snapshot_id: str | None = None
    underlying_snapshot_id: str | None = None
    security_type: SecurityType | None = None
    detail: str | None = None

    @property
    def snapshot_ids(self) -> list[str]:
        return sorted(
            {
                snapshot
                for snapshot in (
                    self.chain_snapshot_id,
                    self.quote_snapshot_id,
                    self.underlying_snapshot_id,
                )
                if snapshot
            }
        )


class StrategySelectionInput(ImmutableModel):
    """Everything the strategy agent is given about one underlying.

    If a fact is not here, the agent does not have it and must not claim it.
    There is deliberately no repository handle, no broker, no option chain, no
    account balance and no budget on this object — and no field in which one
    could appear.
    """

    run_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    schema_version: Identifier = STRATEGY_SCHEMA_VERSION

    research: ResearchSummary
    eligible_strategies: list[StrategyOption] = Field(min_length=1)

    research_run_id: Identifier | None = None
    universe_run_id: Identifier | None = None

    @model_validator(mode="after")
    def _only_eligible_strategies_are_expressible(self) -> StrategySelectionInput:
        """An ineligible strategy cannot reach the agent, structurally.

        Checked here rather than trusted from the caller: a strategy that does
        not answer this hypothesis but appeared in the input could be chosen,
        and the deterministic layer exists precisely so that it cannot.
        """
        ids = [option.strategy_id for option in self.eligible_strategies]
        duplicates = sorted({s.value for s in ids if ids.count(s) > 1})
        if duplicates:
            raise ValueError(f"duplicate strategy in selection input: {', '.join(duplicates)}")

        ineligible = sorted(
            option.strategy_id.value
            for option in self.eligible_strategies
            if self.research.hypothesis not in option.applicable_hypotheses
        )
        if ineligible:
            raise ValueError(
                f"strategies that do not answer hypothesis {self.research.hypothesis.value} "
                f"must never reach the agent: {', '.join(ineligible)}"
            )
        if self.research.symbol != self.symbol:
            raise ValueError(
                f"the research summary is about {self.research.symbol} but the input is "
                f"about {self.symbol}; strategy contexts are isolated per underlying"
            )
        return self

    @property
    def strategy_ids(self) -> frozenset[StrategyType]:
        return frozenset(option.strategy_id for option in self.eligible_strategies)

    def option(self, strategy_id: StrategyType) -> StrategyOption | None:
        return next(
            (o for o in self.eligible_strategies if o.strategy_id is strategy_id),
            None,
        )


# ---------------------------------------------------------------------------
# Agent output
# ---------------------------------------------------------------------------
class StrategyAgentOutput(ImmutableModel):
    """The agent's whole structured response for one underlying.

    Schema-valid is not the same as acceptable: this object can be well-formed
    and still be rejected by :mod:`trading_system.strategies.validation` for
    naming a strategy the input did not offer, for citing a reason its own
    research refutes, or for choosing a directional strategy against an outlook
    that states no direction.

    ``extra="forbid"`` is load-bearing here rather than tidy. A response that
    tried to add ``strike``, ``expiration``, ``contract_id``, ``quantity`` or
    ``limit_price`` does not have those fields quietly dropped — it fails to
    parse, and the decision fails closed.
    """

    run_id: Identifier
    symbol: Ticker
    action: StrategyAction
    selected_strategy: StrategyType | None = None
    confidence: ConfidenceLevel
    reasons: list[StrategySelectionReason] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _strategy_matches_action(self) -> StrategyAgentOutput:
        if self.action is StrategyAction.BUY and self.selected_strategy is None:
            raise ValueError("action BUY requires a selected_strategy")
        if self.action is StrategyAction.NO_TRADE and self.selected_strategy is not None:
            raise ValueError(
                "action NO_TRADE must not carry a strategy; declining to trade and "
                "proposing a trade are different answers"
            )
        return self

    @property
    def is_trade(self) -> bool:
        return self.action is StrategyAction.BUY


class StrategyAgentMetadata(ImmutableModel):
    """Which model produced a decision, precisely enough to reproduce the call."""

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


# ---------------------------------------------------------------------------
# The stored decision
# ---------------------------------------------------------------------------
class StrategyDecisionRecord(ImmutableModel):
    """The canonical, immutable strategy decision for one underlying.

    A decision is **not an order**. It says "for this underlying, given this
    research, this configured strategy is appropriate" — no contract, no
    quantity, no money, and no field in which any of those could be recorded.

    ``NO_TRADE`` with ``status=SUCCESS`` is the ordinary, expected shape of a
    considered refusal. A status other than ``SUCCESS`` means the stage could
    not reach a decision at all, and such a record names no strategy.
    """

    decision_id: Identifier
    run_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: StrategySelectionStatus
    schema_version: Identifier = STRATEGY_SCHEMA_VERSION

    action: StrategyAction = StrategyAction.NO_TRADE
    selected_strategy: StrategyType | None = None
    strategy_version: str | None = None
    decision_method: DecisionMethod = DecisionMethod.DETERMINISTIC_ONLY
    confidence: ConfidenceLevel | None = None
    reasons: list[StrategySelectionReason] = Field(default_factory=list)
    rationale: str | None = None

    hypothesis: MarketHypothesis | None = None
    research_confidence: ConfidenceLevel | None = None
    research_horizon_days: int | None = Field(default=None, ge=1, le=365)
    research_report_id: Identifier | None = None
    research_run_id: Identifier | None = None
    universe_run_id: Identifier | None = None

    #: Every strategy that was eligible for the hypothesis, chosen or not.
    #: "What else could it have picked" is part of explaining what it picked.
    eligible_strategies: list[StrategyType] = Field(default_factory=list)
    data_readiness: DataReadiness = Field(default_factory=DataReadiness)
    input_snapshot_ids: list[str] = Field(default_factory=list)

    agent_metadata: StrategyAgentMetadata | None = None
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _a_failure_names_no_strategy(self) -> StrategyDecisionRecord:
        """A stage that could not decide has not decided on anything."""
        if not self.status.produced_a_decision:
            if self.selected_strategy is not None or self.action is StrategyAction.BUY:
                raise ValueError(
                    f"status {self.status.value} must not carry a strategy; a failure is "
                    f"never a trade proposal"
                )
            return self

        if self.action is StrategyAction.BUY:
            missing = [
                name
                for name, value in (
                    ("selected_strategy", self.selected_strategy),
                    ("strategy_version", self.strategy_version),
                    ("rationale", self.rationale),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"a BUY decision must state {', '.join(missing)}")
        elif self.selected_strategy is not None:
            raise ValueError("a NO_TRADE decision must not carry a strategy")
        return self

    @model_validator(mode="after")
    def _no_eligible_strategy_reason_implies_no_trade(self) -> StrategyDecisionRecord:
        if (
            StrategySelectionReason.NO_ELIGIBLE_STRATEGY in self.reasons
            and self.action is StrategyAction.BUY
        ):
            raise ValueError(
                "NO_ELIGIBLE_STRATEGY cannot accompany a BUY: a strategy was evidently "
                "eligible, since one was chosen"
            )
        return self

    @property
    def succeeded(self) -> bool:
        return self.status is StrategySelectionStatus.SUCCESS

    @property
    def proposes_a_trade(self) -> bool:
        return self.succeeded and self.action is StrategyAction.BUY

    def to_strategy_decision(self) -> StrategyDecision:
        """Project onto the Milestone 1 workflow boundary.

        The full record is the audit artifact; this is the narrow contract the
        next stage was built against (``schemas/strategy_decision.json``),
        exactly as a research report projects onto ``research_report.json``.

        Raises :class:`ValueError` for a record that reached no decision: there
        is deliberately no way to hand a failed stage across the boundary as
        though it were a choice.
        """
        if not self.succeeded:
            raise ValueError(
                f"cannot project a {self.status.value} decision onto the contract boundary; "
                f"a stage that reached no decision has nothing to hand on"
            )
        if self.research_report_id is None:
            raise ValueError("a decision without a research report cannot cross the boundary")
        return StrategyDecision(
            decision_id=self.decision_id,
            ticker=self.symbol,
            research_report_id=self.research_report_id,
            as_of=self.as_of,
            action=self.action,
            strategy_type=self.selected_strategy,
            rationale=self.rationale or "no rationale recorded",
            versions=self.versions,
        )


class StrategyRunCounts(ImmutableModel):
    """How many underlyings reached each outcome.

    Derived from the run's own decisions, which is why elapsed time is
    deliberately absent: a measured duration is a fact about the machine, not
    about the decision, and storing it in a content-addressed record would make
    two otherwise identical runs differ.
    """

    researched_assets: int = Field(default=0, ge=0)
    considered: int = Field(default=0, ge=0)
    proposed: int = Field(default=0, ge=0)
    no_trade: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class StrategyRunResult(ImmutableModel):
    """The immutable record of one strategy run: one decision per underlying."""

    run_id: Identifier
    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: StrategySelectionStatus
    schema_version: Identifier = STRATEGY_SCHEMA_VERSION

    research_run_id: Identifier | None = None
    universe_run_id: Identifier | None = None
    decisions: list[StrategyDecisionRecord] = Field(default_factory=list)
    counts: StrategyRunCounts = Field(default_factory=StrategyRunCounts)
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _decisions_are_one_per_symbol(self) -> StrategyRunResult:
        symbols = [decision.symbol for decision in self.decisions]
        duplicates = sorted({s for s in symbols if symbols.count(s) > 1})
        if duplicates:
            raise ValueError(f"one decision per underlying per run; duplicated: {duplicates}")
        wrong_run = sorted({d.symbol for d in self.decisions if d.run_id != self.run_id})
        if wrong_run:
            raise ValueError(f"decisions from another run: {wrong_run}")
        return self

    @property
    def proposals(self) -> list[StrategyDecisionRecord]:
        """Decisions a contract selector may act on, in a stable order."""
        return sorted(
            (d for d in self.decisions if d.proposes_a_trade),
            key=lambda d: d.symbol,
        )

    @property
    def symbols(self) -> list[str]:
        return [decision.symbol for decision in self.decisions]

    def decision(self, symbol: str) -> StrategyDecisionRecord | None:
        wanted = symbol.strip().upper()
        return next((d for d in self.decisions if d.symbol == wanted), None)


# ---------------------------------------------------------------------------
# Contract selection
# ---------------------------------------------------------------------------
class SelectedLeg(ImmutableModel):
    """One concrete contract, chosen deterministically.

    Every broker identifier is carried verbatim. ``trading_class`` in
    particular is copied from the chain the contract came from and never
    derived from the symbol: IBKR reports SPY options under ``SMART/SPY`` *and*
    ``SMART/2SPY``, and a contract rebuilt on the assumption that the class
    matches the ticker does not exist at the broker.

    Every measurement is optional and ``None`` means *not available*. A leg with
    no implied volatility says so; it does not report zero.
    """

    leg_index: int = Field(ge=0)
    action: LegAction
    right: OptionRight
    ratio: int = Field(default=1, ge=1)

    underlying: Ticker
    expiration: date
    dte: int = Field(ge=0)
    strike: Money = Field(gt=0)
    multiplier: int = Field(ge=1)
    trading_class: str = Field(min_length=1)
    contract_id: int
    exchange: str | None = None
    local_symbol: str | None = None
    currency: str | None = None

    strike_policy: StrikeSelectionPolicy
    #: What the policy asked for, so "why this strike" is answerable without
    #: re-deriving it: the target, and how far the listed strike sits from it.
    target_strike: Money | None = None
    strike_distance_pct: float | None = None
    reference_price: Money | None = None
    selection_reason: str = Field(min_length=1)

    bid: Money | None = None
    ask: Money | None = None
    last: Money | None = None
    implied_volatility: Money | None = None
    delta: Money | None = None
    gamma: Money | None = None
    theta: Money | None = None
    vega: Money | None = None
    volume: Money | None = None
    open_interest: Money | None = None

    chain_snapshot_id: Identifier
    quote_snapshot_id: str | None = None
    quote_as_of: UtcDatetime | None = None

    @property
    def has_quote(self) -> bool:
        return self.bid is not None or self.ask is not None or self.last is not None

    @property
    def mid(self) -> Decimal | None:
        """Midpoint, or ``None`` when either side is missing.

        Never derived from one side: a midpoint computed from a bid alone is
        not a midpoint, and a later stage must be able to tell the difference.
        """
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread_pct(self) -> Decimal | None:
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid * Decimal(100)

    def to_option_leg(self) -> OptionLeg:
        """Project onto the Milestone 1 leg the purchase card carries."""
        return OptionLeg(
            underlying=self.underlying,
            right=self.right,
            strike=self.strike,
            expiration=self.expiration,
            action=self.action,
            ratio=self.ratio,
            multiplier=self.multiplier,
            occ_symbol=self.local_symbol,
            broker_contract_id=self.contract_id,
        )


class RejectedContract(ImmutableModel):
    """A candidate that was considered and not selected, with a reason.

    Preserved so "why not the 185 call" is answerable from the stored record.
    Rejections are what make a deterministic selection auditable rather than
    merely reproducible.
    """

    reason: ContractRejectionReason
    leg_index: int | None = Field(default=None, ge=0)
    contract_id: int | None = None
    expiration: date | None = None
    strike: Money | None = None
    right: OptionRight | None = None
    detail: str | None = None


class ContractCostEstimate(ImmutableModel):
    """What the selected structure would cost, when the data supports saying.

    Decimal throughout. Never a float, and never a number invented to fill a
    gap: with no ask on some leg, ``available`` is ``False``, every figure is
    ``None`` and ``unavailable_reason`` says which leg and why.

    This is a *cost of one unit of the structure*, not a position size. There is
    no quantity here and no allocation — those belong to the risk and allocation
    engines, and a field for them here would be an invitation to pre-empt them.
    """

    available: bool = False
    currency: str | None = None
    #: Sum over legs of ask x multiplier x ratio: the honest cost of buying.
    estimated_debit: Money | None = Field(default=None, ge=0)
    #: The same at the midpoint, where both sides of every leg were quoted.
    estimated_mid_debit: Money | None = Field(default=None, ge=0)
    max_leg_spread_pct: float | None = Field(default=None, ge=0)
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _unavailable_carries_no_figures(self) -> ContractCostEstimate:
        if self.available:
            if self.estimated_debit is None:
                raise ValueError("an available cost estimate must state an estimated_debit")
            return self
        stated = [
            name
            for name, value in (
                ("estimated_debit", self.estimated_debit),
                ("estimated_mid_debit", self.estimated_mid_debit),
            )
            if value is not None
        ]
        if stated:
            raise ValueError(
                f"an unavailable cost estimate must carry no figures, but states "
                f"{', '.join(stated)}"
            )
        if not (self.unavailable_reason or "").strip():
            raise ValueError("an unavailable cost estimate must say why")
        return self


class ContractSelectionResult(ImmutableModel):
    """The immutable record of one deterministic contract selection.

    No contract is a valid outcome and a common one. A result whose status is
    not ``SUCCESS`` carries no legs, states why, and lists what it rejected —
    the closest match is never returned as a consolation, because a contract
    that nearly satisfies the policy satisfies no policy at all.
    """

    selection_id: Identifier
    run_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    generated_at: UtcDatetime
    selection_status: ContractSelectionStatus
    schema_version: Identifier = STRATEGY_SCHEMA_VERSION

    strategy: StrategyType | None = None
    strategy_version: str | None = None
    strategy_run_id: Identifier | None = None
    strategy_decision_id: Identifier | None = None
    research_report_id: Identifier | None = None

    legs: list[SelectedLeg] = Field(default_factory=list)
    expiration: date | None = None
    dte: int | None = Field(default=None, ge=0)
    expiration_policy: ExpirationSelectionPolicy | None = None
    expiration_reason: str | None = None
    reference_price: Money | None = None
    reference_price_field: str | None = None
    cost: ContractCostEstimate | None = None

    selection_policy_version: Identifier
    input_snapshot_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    rejected_candidates: list[RejectedContract] = Field(default_factory=list)
    candidates_considered: int = Field(default=0, ge=0)
    rejections_recorded_truncated: bool = False

    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _a_failure_selects_nothing(self) -> ContractSelectionResult:
        if not self.selection_status.produced_contracts:
            if self.legs:
                raise ValueError(
                    f"status {self.selection_status.value} must not carry legs; a selection "
                    f"that failed selected nothing"
                )
            return self

        if not self.legs:
            raise ValueError("a SUCCESS selection must carry at least one leg")
        if self.strategy is None:
            raise ValueError("a SUCCESS selection must name the strategy it satisfies")
        if self.expiration is None or self.dte is None:
            raise ValueError("a SUCCESS selection must state its expiration and DTE")
        return self

    @model_validator(mode="after")
    def _legs_are_mutually_consistent(self) -> ContractSelectionResult:
        """Multi-leg integrity: one underlying, one expiration, distinct legs.

        Enforced on the artifact as well as during selection, so a record that
        somehow came to disagree with itself cannot be stored and later read as
        a valid position.
        """
        if not self.legs:
            return self
        underlyings = {leg.underlying for leg in self.legs}
        if underlyings != {self.symbol}:
            raise ValueError(f"every leg must reference {self.symbol}, got {sorted(underlyings)}")
        if self.expiration is not None and any(
            leg.expiration != self.expiration for leg in self.legs
        ):
            raise ValueError("every leg must share the selection's expiration")
        if len({leg.multiplier for leg in self.legs}) > 1:
            raise ValueError("legs with different multipliers are not one position")
        if len({leg.trading_class for leg in self.legs}) > 1:
            raise ValueError("legs from different trading classes are not one position")
        identities = [(leg.right, leg.strike, leg.expiration) for leg in self.legs]
        if len(set(identities)) != len(identities):
            raise ValueError("the same contract appears in more than one leg")
        return self

    @property
    def succeeded(self) -> bool:
        return self.selection_status is ContractSelectionStatus.SUCCESS

    def to_contract_selection(self) -> ContractSelection:
        """Project onto the Milestone 1 boundary the purchase card carries."""
        if not self.succeeded:
            raise ValueError(
                f"cannot project a {self.selection_status.value} selection onto the purchase "
                f"card boundary; no contract was selected"
            )
        assert self.strategy is not None  # narrowing; guaranteed by the validator
        assert self.dte is not None
        deltas = [leg.delta for leg in self.legs if leg.delta is not None]
        return ContractSelection(
            underlying=self.symbol,
            strategy_type=self.strategy,
            as_of=self.as_of,
            legs=[leg.to_option_leg() for leg in self.legs],
            dte=self.dte,
            # A single-leg strategy's delta is the position's delta; for a
            # multi-leg one it is not a meaningful single number, so nothing is
            # invented to fill the field.
            target_delta=float(deltas[0]) if len(self.legs) == 1 and deltas else None,
            selection_rules_version=self.selection_policy_version,
        )


class ContractRunCounts(ImmutableModel):
    """How many decisions reached each contract-selection outcome."""

    decisions_considered: int = Field(default=0, ge=0)
    selected: int = Field(default=0, ge=0)
    no_contract: int = Field(default=0, ge=0)
    no_trade: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class ContractSelectionRunResult(ImmutableModel):
    """The immutable record of one contract-selection run.

    Deterministic end to end: no model is consulted, and given the same
    decisions, the same stored chain and quotes, the same configuration and the
    same ``as_of``, this record is byte-identical apart from its own generation
    clock.
    """

    run_id: Identifier
    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: ContractSelectionStatus
    schema_version: Identifier = STRATEGY_SCHEMA_VERSION

    strategy_run_id: Identifier | None = None
    research_run_id: Identifier | None = None
    selection_policy_version: Identifier
    selections: list[ContractSelectionResult] = Field(default_factory=list)
    counts: ContractRunCounts = Field(default_factory=ContractRunCounts)
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _selections_are_one_per_symbol(self) -> ContractSelectionRunResult:
        symbols = [selection.symbol for selection in self.selections]
        duplicates = sorted({s for s in symbols if symbols.count(s) > 1})
        if duplicates:
            raise ValueError(f"one selection per underlying per run; duplicated: {duplicates}")
        wrong_run = sorted({s.symbol for s in self.selections if s.run_id != self.run_id})
        if wrong_run:
            raise ValueError(f"selections from another run: {wrong_run}")
        return self

    @property
    def successful(self) -> list[ContractSelectionResult]:
        return [selection for selection in self.selections if selection.succeeded]

    @property
    def symbols(self) -> list[str]:
        return [selection.symbol for selection in self.selections]

    def selection(self, symbol: str) -> ContractSelectionResult | None:
        wanted = symbol.strip().upper()
        return next((s for s in self.selections if s.symbol == wanted), None)


# ---------------------------------------------------------------------------
# Derived identifiers
# ---------------------------------------------------------------------------
def decision_identifier(
    *,
    run_id: str,
    symbol: str,
    as_of: datetime,
    status: StrategySelectionStatus,
    action: StrategyAction,
    strategy: StrategyType | None,
    schema_version: str = STRATEGY_SCHEMA_VERSION,
) -> str:
    """Derive a decision's identifier from its content.

    Deliberately excludes every runtime measurement — latency, token counts,
    wall-clock duration — so two runs over identical inputs reaching an
    identical decision produce an identical id. The same reasoning keeps
    observation clocks out of the data layer's payload hashes.
    """
    digest = stable_hash(
        [
            "STRATEGY_DECISION",
            schema_version,
            run_id,
            symbol.upper(),
            as_of.isoformat(),
            status.value,
            action.value,
            strategy.value if strategy else "",
        ]
    )
    return f"strategy-{symbol.upper()}-{digest[:16]}"


def selection_identifier(
    *,
    run_id: str,
    symbol: str,
    as_of: datetime,
    status: ContractSelectionStatus,
    strategy: StrategyType | None,
    contract_ids: list[int],
    schema_version: str = STRATEGY_SCHEMA_VERSION,
) -> str:
    """Derive a contract selection's identifier from the contracts it chose."""
    digest = stable_hash(
        [
            "CONTRACT_SELECTION",
            schema_version,
            run_id,
            symbol.upper(),
            as_of.isoformat(),
            status.value,
            strategy.value if strategy else "",
            sorted(contract_ids),
        ]
    )
    return f"contract-{symbol.upper()}-{digest[:16]}"
