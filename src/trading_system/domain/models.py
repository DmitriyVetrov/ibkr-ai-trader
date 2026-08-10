"""Domain models for every workflow boundary.

Each model here has a matching JSON Schema in ``schemas/``; the contract tests
in ``tests/contract/`` assert that the two stay in step and that each stage's
output is consumable by the next stage.

Two invariants are enforced structurally rather than by convention:

* **Money is decimal-safe.** Monetary and price fields reject ``float`` input
  outright (specification section 21). Pass ``Decimal`` or ``str``.
* **Time is timezone-aware UTC.** Naive datetimes are rejected, aware
  datetimes are normalised to UTC (specification section 22). Exchange-local
  time belongs at the scheduling/presentation boundary only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from trading_system.domain.enums import (
    DataQuality,
    Direction,
    ExitAction,
    ExitReason,
    ExpectedMagnitude,
    LegAction,
    MarketHypothesis,
    OptionRight,
    OrderStatus,
    OrderType,
    PositionState,
    RiskOutcome,
    RiskReasonCode,
    SourceTier,
    StrategyAction,
    StrategyType,
    ThesisStatus,
    TimeInForce,
    TradingMode,
)

__all__ = [
    "AllocationDecision",
    "AllocationEntry",
    "ContractSelection",
    "DomainModel",
    "ExecutionResult",
    "ExitDecision",
    "Fill",
    "ImmutableModel",
    "Money",
    "OptionLeg",
    "OrderIntent",
    "PositionSnapshot",
    "PurchaseCard",
    "ResearchReport",
    "RiskDecision",
    "SourceReference",
    "StrategyDecision",
    "SystemVersions",
    "TradeSnapshot",
    "UniverseCandidate",
    "UniverseSelection",
    "UtcDatetime",
]


# ---------------------------------------------------------------------------
# Custom field types
# ---------------------------------------------------------------------------
def _reject_binary_float(value: Any) -> Any:
    """Refuse ``float`` input for monetary and price fields.

    Accepting a float would silently import binary floating-point error into
    accounting. ``Decimal("0.1")`` and ``"0.1"`` are exact; ``0.1`` is not.
    """
    if isinstance(value, float):
        raise ValueError(
            "monetary/price values must not be built from binary floating point; "
            "pass a Decimal or a string instead"
        )
    return value


def _to_utc(value: Any) -> Any:
    """Require an aware datetime and normalise it to UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "naive datetime rejected: all timestamps must be timezone-aware "
                "and are stored normalised to UTC"
            )
        return value.astimezone(UTC)
    return value


#: Exact decimal quantity for money, prices and strikes.
Money = Annotated[Decimal, BeforeValidator(_reject_binary_float)]

#: Timezone-aware datetime, always stored as UTC.
UtcDatetime = Annotated[datetime, BeforeValidator(_to_utc)]

#: Probability-like score in [0, 1].
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

#: Non-empty identifier string.
Identifier = Annotated[str, Field(min_length=1, max_length=200)]

#: Underlying ticker symbol.
Ticker = Annotated[str, Field(min_length=1, max_length=16, pattern=r"^[A-Z0-9.\-]+$")]


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------
class DomainModel(BaseModel):
    """Base for all domain models.

    ``extra="forbid"`` matters: an agent returning an unexpected field is a
    contract violation that should fail loudly, not be silently dropped.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ImmutableModel(DomainModel):
    """Base for artifacts that are part of the permanent trade record.

    Once written, these are never edited. New evidence is appended as a new
    artifact (specification section 18: the original thesis stays immutable).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class SystemVersions(ImmutableModel):
    """Version stamps required on every persisted trade artifact (section 41)."""

    application_version: Identifier
    config_version: Identifier
    strategy_spec_version: Identifier | None = None
    agent_version: Identifier | None = None
    model_id: Identifier | None = None
    data_source_versions: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. Universe selection
# ---------------------------------------------------------------------------
class UniverseCandidate(ImmutableModel):
    """A single underlying proposed by the Universe Selector agent."""

    ticker: Ticker
    rank: int = Field(ge=1)
    selection_score: float = Field(ge=0.0, le=100.0)
    rationale: str | None = None


class UniverseSelection(ImmutableModel):
    """Ranked list of candidate underlyings. Contains no option contracts."""

    universe_id: Identifier
    as_of: UtcDatetime
    candidates: list[UniverseCandidate] = Field(default_factory=list)
    versions: SystemVersions

    @model_validator(mode="after")
    def _check_unique_and_ranked(self) -> UniverseSelection:
        tickers = [c.ticker for c in self.candidates]
        if len(set(tickers)) != len(tickers):
            raise ValueError("duplicate ticker in universe selection")
        ranks = sorted(c.rank for c in self.candidates)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("candidate ranks must be unique and contiguous from 1")
        return self


# ---------------------------------------------------------------------------
# 2. Research
# ---------------------------------------------------------------------------
class SourceReference(ImmutableModel):
    """Provenance for a piece of research evidence (specification section 37).

    A source may only appear here if it was actually retrieved.
    """

    source_name: Identifier
    source_url_or_identifier: Identifier
    source_tier: SourceTier
    published_at: UtcDatetime | None = None
    retrieved_at: UtcDatetime
    relevance: Confidence = 1.0


class ResearchReport(ImmutableModel):
    """Structured output of the Market Researcher agent (specification section 6)."""

    report_id: Identifier
    ticker: Ticker
    as_of: UtcDatetime
    hypothesis: MarketHypothesis
    direction: Direction
    expected_magnitude: ExpectedMagnitude
    confidence: Confidence
    expected_horizon_days: int = Field(ge=1, le=365)
    catalysts: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    explanation: str | None = None
    versions: SystemVersions

    @model_validator(mode="after")
    def _hypothesis_e_requires_explanation(self) -> ResearchReport:
        if self.hypothesis is MarketHypothesis.E and not (self.explanation or "").strip():
            raise ValueError("hypothesis E requires a free-text explanation")
        return self


# ---------------------------------------------------------------------------
# 3. Strategy selection
# ---------------------------------------------------------------------------
class StrategyDecision(ImmutableModel):
    """Output of the Options Strategy agent: BUY with a strategy, or NO_TRADE."""

    decision_id: Identifier
    ticker: Ticker
    research_report_id: Identifier
    as_of: UtcDatetime
    action: StrategyAction
    strategy_type: StrategyType | None = None
    rationale: str = Field(min_length=1)
    versions: SystemVersions

    @model_validator(mode="after")
    def _strategy_matches_action(self) -> StrategyDecision:
        if self.action is StrategyAction.BUY and self.strategy_type is None:
            raise ValueError("action BUY requires a strategy_type")
        if self.action is StrategyAction.NO_TRADE and self.strategy_type is not None:
            raise ValueError("action NO_TRADE must not carry a strategy_type")
        return self


# ---------------------------------------------------------------------------
# 4. Contract selection (deterministic — specification section 8)
# ---------------------------------------------------------------------------
class OptionLeg(ImmutableModel):
    """One leg of an option strategy."""

    underlying: Ticker
    right: OptionRight
    strike: Money = Field(gt=0)
    expiration: date
    action: LegAction
    ratio: int = Field(default=1, ge=1)
    multiplier: int = Field(default=100, ge=1)
    occ_symbol: str | None = None
    broker_contract_id: int | None = None


class ContractSelection(ImmutableModel):
    """Concrete contract(s) chosen deterministically for a strategy."""

    underlying: Ticker
    strategy_type: StrategyType
    as_of: UtcDatetime
    legs: list[OptionLeg] = Field(min_length=1)
    dte: int = Field(ge=0)
    target_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    selection_rules_version: Identifier | None = None

    @model_validator(mode="after")
    def _legs_share_underlying(self) -> ContractSelection:
        if any(leg.underlying != self.underlying for leg in self.legs):
            raise ValueError("all legs must reference the selection's underlying")
        return self


# ---------------------------------------------------------------------------
# 5. Purchase card (specification section 12)
# ---------------------------------------------------------------------------
class PurchaseCard(ImmutableModel):
    """Immutable statement of why the system intends to trade.

    This is the authoritative explanation for a trade and is replayed during
    evaluation. It is written once and never edited.
    """

    card_id: Identifier
    created_at: UtcDatetime
    underlying: Ticker
    strategy_type: StrategyType
    contract: ContractSelection

    # why
    hypothesis: MarketHypothesis
    confidence: Confidence
    expected_magnitude: ExpectedMagnitude
    expected_horizon_days: int = Field(ge=1, le=365)

    # how much
    quantity: int = Field(ge=1)
    requested_allocation_eur: Money = Field(ge=0)

    # guard rails
    risk_limits: dict[str, str] = Field(default_factory=dict)
    entry_conditions: list[str] = Field(default_factory=list)
    exit_policy: dict[str, str] = Field(default_factory=dict)
    thesis_invalidation_conditions: list[str] = Field(min_length=1)

    # provenance
    research_report_id: Identifier
    strategy_decision_id: Identifier
    sources: list[SourceReference] = Field(default_factory=list)
    versions: SystemVersions


# ---------------------------------------------------------------------------
# 6. Allocation (specification section 10)
# ---------------------------------------------------------------------------
class AllocationEntry(ImmutableModel):
    """Budget granted to one opportunity. Zero is a valid, expected outcome."""

    opportunity_id: Identifier
    ticker: Ticker
    rank: int = Field(ge=1)
    opportunity_score: float = Field(ge=0.0, le=100.0)
    allocated_eur: Money = Field(ge=0)


class AllocationDecision(ImmutableModel):
    """Deterministic split of the campaign budget across opportunities.

    Identical inputs must produce an identical decision.
    """

    allocation_id: Identifier
    campaign_id: Identifier
    as_of: UtcDatetime
    total_budget_eur: Money = Field(ge=0)
    allocated_eur: Money = Field(ge=0)
    reserve_eur: Money = Field(ge=0)
    entries: list[AllocationEntry] = Field(default_factory=list)
    versions: SystemVersions

    @model_validator(mode="after")
    def _budget_balances(self) -> AllocationDecision:
        entry_total = sum((e.allocated_eur for e in self.entries), Decimal("0"))
        if entry_total != self.allocated_eur:
            raise ValueError(
                f"entry allocations ({entry_total}) do not sum to allocated_eur "
                f"({self.allocated_eur})"
            )
        if self.allocated_eur + self.reserve_eur != self.total_budget_eur:
            raise ValueError("allocated_eur + reserve_eur must equal total_budget_eur")
        return self


# ---------------------------------------------------------------------------
# 7. Risk (specification section 11)
# ---------------------------------------------------------------------------
class RiskDecision(ImmutableModel):
    """Deterministic risk verdict. No AI agent may override a rejection."""

    decision_id: Identifier
    purchase_card_id: Identifier
    as_of: UtcDatetime
    outcome: RiskOutcome
    reason_codes: list[RiskReasonCode] = Field(min_length=1)
    evaluated_limits: dict[str, str] = Field(default_factory=dict)
    trading_mode: TradingMode
    versions: SystemVersions

    @model_validator(mode="after")
    def _codes_match_outcome(self) -> RiskDecision:
        has_ok = RiskReasonCode.OK in self.reason_codes
        if self.outcome is RiskOutcome.APPROVED and self.reason_codes != [RiskReasonCode.OK]:
            raise ValueError("APPROVED must carry exactly one reason code: OK")
        if self.outcome is RiskOutcome.REJECTED and has_ok:
            raise ValueError("REJECTED must not carry the OK reason code")
        return self


# ---------------------------------------------------------------------------
# 8. Order intent and execution
# ---------------------------------------------------------------------------
class OrderIntent(ImmutableModel):
    """A risk-approved instruction handed to the execution engine.

    Only produced downstream of an APPROVED :class:`RiskDecision`.
    """

    intent_id: Identifier
    purchase_card_id: Identifier
    risk_decision_id: Identifier
    created_at: UtcDatetime
    underlying: Ticker
    strategy_type: StrategyType
    legs: list[OptionLeg] = Field(min_length=1)
    quantity: int = Field(ge=1)
    order_type: OrderType
    limit_price: Money | None = Field(default=None, gt=0)
    time_in_force: TimeInForce = TimeInForce.DAY
    max_slippage_bps: int = Field(default=0, ge=0)
    trading_mode: TradingMode
    versions: SystemVersions

    @model_validator(mode="after")
    def _limit_price_matches_order_type(self) -> OrderIntent:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT order requires a limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET order must not carry a limit_price")
        return self


class Fill(ImmutableModel):
    """A single execution report from the broker."""

    fill_id: Identifier
    leg_index: int = Field(ge=0)
    quantity: int = Field(ge=1)
    price: Money = Field(gt=0)
    commission: Money = Field(default=Decimal("0"), ge=0)
    filled_at: UtcDatetime


class ExecutionResult(ImmutableModel):
    """Outcome of submitting an :class:`OrderIntent`.

    ``orders_submitted`` is explicit so that read-only broker tests can assert
    ``orders_submitted == 0`` (specification section 30).
    """

    intent_id: Identifier
    broker: Identifier
    broker_order_id: str | None = None
    status: OrderStatus
    orders_submitted: int = Field(default=0, ge=0)
    filled_quantity: int = Field(default=0, ge=0)
    average_fill_price: Money | None = Field(default=None, gt=0)
    fills: list[Fill] = Field(default_factory=list)
    submitted_at: UtcDatetime | None = None
    last_update_at: UtcDatetime
    message: str | None = None
    trading_mode: TradingMode


# ---------------------------------------------------------------------------
# 9. Position monitoring
# ---------------------------------------------------------------------------
class PositionSnapshot(ImmutableModel):
    """Point-in-time view of a position.

    ``source`` records where the numbers came from; broker-sourced snapshots
    outrank internally derived ones during reconciliation.
    """

    position_id: Identifier
    purchase_card_id: Identifier | None = None
    as_of: UtcDatetime
    state: PositionState
    underlying: Ticker
    strategy_type: StrategyType
    legs: list[OptionLeg] = Field(min_length=1)
    quantity: int
    average_entry_price: Money | None = Field(default=None, gt=0)
    market_value_eur: Money | None = None
    unrealized_pnl_eur: Money | None = None
    realized_pnl_eur: Money | None = None
    days_to_expiration: int | None = None
    thesis_status: ThesisStatus = ThesisStatus.UNKNOWN
    source: Identifier
    data_quality: DataQuality = DataQuality.OK


# ---------------------------------------------------------------------------
# 10. Exit
# ---------------------------------------------------------------------------
class ExitDecision(ImmutableModel):
    """Exit engine verdict for a whole strategy (specification section 19).

    Multi-leg strategies are closed as a unit unless the strategy
    specification explicitly permits independent leg exits.
    """

    decision_id: Identifier
    position_id: Identifier
    as_of: UtcDatetime
    decision: ExitAction
    reason: ExitReason | None = None
    detail: str | None = None
    close_whole_strategy: bool = True
    versions: SystemVersions

    @model_validator(mode="after")
    def _reason_matches_decision(self) -> ExitDecision:
        if self.decision is ExitAction.SELL and self.reason is None:
            raise ValueError("SELL requires an exit reason")
        if self.decision is ExitAction.HOLD and self.reason is not None:
            raise ValueError("HOLD must not carry an exit reason")
        return self


# ---------------------------------------------------------------------------
# 11. Trade snapshot (specification sections 40-41)
# ---------------------------------------------------------------------------
class TradeSnapshot(ImmutableModel):
    """Immutable end-of-life record enabling full reconstruction of a trade."""

    trade_id: Identifier
    underlying: Ticker
    strategy_type: StrategyType
    opened_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    final_state: PositionState

    purchase_card_id: Identifier
    research_report_id: Identifier
    strategy_decision_id: Identifier
    allocation_id: Identifier
    risk_decision_id: Identifier
    order_intent_id: Identifier | None = None
    exit_decision_id: Identifier | None = None

    realized_pnl_eur: Money | None = None
    r_multiple: float | None = None
    max_favorable_excursion_eur: Money | None = None
    max_adverse_excursion_eur: Money | None = None
    exit_reason: ExitReason | None = None
    versions: SystemVersions
