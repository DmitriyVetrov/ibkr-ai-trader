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
    BrokerConnectionState,
    DataQuality,
    Direction,
    DiscrepancyType,
    ExecutionIntent,
    ExitAction,
    ExitReason,
    ExpectedMagnitude,
    LegAction,
    MarketDataOrigin,
    MarketHypothesis,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionState,
    ReconciliationStatus,
    RiskOutcome,
    RiskReasonCode,
    SecurityType,
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
    "BrokerAccount",
    "BrokerContract",
    "BrokerExecution",
    "BrokerHealth",
    "BrokerOrder",
    "BrokerPosition",
    "ContractSelection",
    "DomainModel",
    "ExecutionResult",
    "ExitDecision",
    "Fill",
    "ImmutableModel",
    "MarketDataSnapshot",
    "Money",
    "OptionChainSnapshot",
    "OptionLeg",
    "OptionQuoteSnapshot",
    "OrderIntent",
    "PositionSnapshot",
    "PurchaseCard",
    "ReconciliationDiscrepancy",
    "ReconciliationReport",
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
    #: Capital committed for this trade, in :attr:`currency` - the currency the
    #: contract is quoted and settled in, which is not necessarily the currency
    #: the operator's capital is held in. The conversion from one to the other
    #: happened upstream, at allocation, and is recorded on that artifact.
    requested_allocation: Money = Field(ge=0)
    #: Stated, never implied. A monetary figure whose currency has to be
    #: inferred from a field name is the defect this field exists to remove.
    currency: str = Field(min_length=3, max_length=8)

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
    """Budget granted to one opportunity. Zero is a valid, expected outcome.

    ``allocated`` is in the parent decision's ``currency``, which is the
    currency the campaign trades in rather than the one its budget is declared
    in. The two coincide only when no conversion was needed.
    """

    opportunity_id: Identifier
    ticker: Ticker
    rank: int = Field(ge=1)
    opportunity_score: float = Field(ge=0.0, le=100.0)
    allocated: Money = Field(ge=0)


class AllocationDecision(ImmutableModel):
    """Deterministic split of the campaign budget across opportunities.

    Identical inputs must produce an identical decision.

    Every figure here is in :attr:`currency` - the currency this campaign
    *trades* in. Where the operator's capital is held in a different currency,
    the budget below is the converted equivalent and the declared original is
    on the allocation run that produced this, together with the rate. Both are
    kept: the converted figure is what a position is sized against, and the
    declared one is what the operator actually has.
    """

    allocation_id: Identifier
    campaign_id: Identifier
    as_of: UtcDatetime
    #: What all three figures below are denominated in. Stated rather than
    #: encoded in the field names, so redenominating a campaign changes a value
    #: and not a schema.
    currency: str = Field(min_length=3, max_length=8)
    total_budget: Money = Field(ge=0)
    allocated: Money = Field(ge=0)
    reserve: Money = Field(ge=0)
    entries: list[AllocationEntry] = Field(default_factory=list)
    versions: SystemVersions

    @model_validator(mode="after")
    def _budget_balances(self) -> AllocationDecision:
        entry_total = sum((e.allocated for e in self.entries), Decimal("0"))
        if entry_total != self.allocated:
            raise ValueError(
                f"entry allocations ({entry_total}) do not sum to allocated ({self.allocated})"
            )
        if self.allocated + self.reserve != self.total_budget:
            raise ValueError("allocated + reserve must equal total_budget")
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

    Only produced downstream of an APPROVED :class:`RiskDecision` — with one
    deliberate exception named by ``intent``. An ``ExecutionIntent.CLEANUP``
    order closes a pre-existing broker holding this system never opened, so
    there *is* no purchase card, risk decision or strategy behind it, and the
    three fields are refused rather than defaulted. Every other intent still
    requires all three, which is enforced here rather than assumed: before this
    field existed nothing stopped an intent from omitting them, and the
    validator below is what keeps the cleanup shape from widening the ordinary
    one.
    """

    intent_id: Identifier
    #: What this order is for. Defaults to ``OPEN`` so every intent built
    #: before the field existed keeps its meaning unchanged.
    intent: ExecutionIntent = ExecutionIntent.OPEN
    #: Required for every intent but ``CLEANUP``, where it must be absent.
    purchase_card_id: Identifier | None = None
    #: Required for every intent but ``CLEANUP``, where it must be absent.
    risk_decision_id: Identifier | None = None
    created_at: UtcDatetime
    underlying: Ticker
    #: Required for every intent but ``CLEANUP``. An orphan holding has no
    #: strategy: naming one would assert this system chose the structure.
    strategy_type: StrategyType | None = None
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

    @model_validator(mode="after")
    def _authorisation_matches_the_intent(self) -> OrderIntent:
        """An authorised order names its authorisation; a cleanup names none.

        Both directions are refused. An ordinary order missing its card or risk
        decision would be an order nobody approved wearing the shape of one; a
        cleanup carrying them would claim provenance for a holding whose
        acquisition is, and stays, unknown.
        """
        authorisation = {
            "purchase_card_id": self.purchase_card_id,
            "risk_decision_id": self.risk_decision_id,
            "strategy_type": self.strategy_type,
        }
        if self.intent.carries_an_authorisation:
            missing = sorted(name for name, value in authorisation.items() if value is None)
            if missing:
                raise ValueError(
                    f"an {self.intent.value} order intent must carry {', '.join(missing)}. "
                    f"Only an ExecutionIntent.CLEANUP order — one closing a pre-existing broker "
                    f"holding this system never opened — has no authorisation behind it"
                )
            return self

        present = sorted(name for name, value in authorisation.items() if value is not None)
        if present:
            raise ValueError(
                f"a CLEANUP order intent carries {', '.join(present)}, but no allocation, "
                f"purchase card, risk decision or strategy exists for a holding this system "
                f"never opened. Pointing these at something would fabricate the provenance "
                f"the cleanup exists to avoid fabricating"
            )
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
    #: Broker-reported valuation, in :attr:`currency` - the currency the
    #: contract is quoted in. Not the account's base currency, and not
    #: converted into it: this is what the broker said about the position, and
    #: converting a reported figure would put a rate in a record of an
    #: observation.
    market_value: Money | None = None
    unrealized_pnl: Money | None = None
    realized_pnl: Money | None = None
    #: Stated, never inferred from a field name.
    currency: str | None = Field(default=None, min_length=3, max_length=8)
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

    #: The trade's result, in :attr:`currency` - the currency it settled in.
    realized_pnl: Money | None = None
    r_multiple: float | None = None
    max_favorable_excursion: Money | None = None
    max_adverse_excursion: Money | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=8)
    exit_reason: ExitReason | None = None
    versions: SystemVersions


# ---------------------------------------------------------------------------
# Broker state (Milestone 2)
#
# These models are our normalised view of what the broker says is true. They
# are deliberately separate from the workflow artifacts above: those record
# what the system decided, these record what the broker reports. When the two
# disagree, the broker wins.
#
# Every field the broker may omit is Optional. An absent value is represented
# as ``None`` and never as a zero — "no margin data" and "zero margin" are
# different facts, and conflating them would let a missing field read as a
# safe number.
# ---------------------------------------------------------------------------

#: Broker-supplied symbols are accepted as-is. Broker reality is authoritative,
#: so an unexpected symbol format must not cause us to reject the response.
BrokerSymbol = Annotated[str, Field(min_length=1, max_length=64)]


class BrokerContract(ImmutableModel):
    """A tradeable instrument as identified by the broker.

    Resolving a symbol to a broker contract id is a prerequisite for quotes and
    option chains, so it is part of the read-only surface.
    """

    symbol: BrokerSymbol
    security_type: SecurityType
    as_of: UtcDatetime
    source: Identifier

    contract_id: int | None = None
    exchange: str | None = None
    primary_exchange: str | None = None
    currency: str | None = None
    local_symbol: str | None = None
    trading_class: str | None = None
    multiplier: int | None = Field(default=None, ge=1)

    expiration: date | None = None
    strike: Money | None = Field(default=None, gt=0)
    right: OptionRight | None = None


class BrokerAccount(ImmutableModel):
    """Account-level state as reported by the broker.

    ``currency`` is the account's **base** currency - what the broker reports
    account-level figures in - and every unsuffixed monetary field below is in
    it. It is emphatically not the currency the account trades in: an account
    based in EUR can hold USD cash and USD-denominated options, which is the
    ordinary case for a European account trading US listings.

    Two fields keep that distinction visible rather than implied:

    ``cash_by_currency``
        What the account actually holds, per currency, unconverted. An account
        with EUR 5,000 and USD 0 says exactly that; nothing here converts one
        into the other or presents a total that quietly did.
    ``exchange_rates``
        How many units of the **base** currency one unit of each other currency
        buys, as the broker itself reported it. Present so a figure in one
        currency can be compared with a figure in another *explicitly*, and
        absent when the broker said nothing - in which case no comparison is
        made at all.
    """

    account_id: BrokerSymbol
    currency: str = Field(min_length=3, max_length=8)
    as_of: UtcDatetime
    source: Identifier

    cash: Money | None = None
    net_liquidation: Money | None = None
    buying_power: Money | None = None
    available_funds: Money | None = None
    initial_margin: Money | None = None
    maintenance_margin: Money | None = None
    excess_liquidity: Money | None = None
    unrealized_pnl: Money | None = None
    realized_pnl: Money | None = None

    #: Cash held, keyed by currency code. Never summed across currencies.
    cash_by_currency: dict[str, Money] = Field(default_factory=dict)
    #: ``1 <key> = <value> <base currency>``, as the broker reported it. A
    #: currency the broker did not quote is simply absent: there is no entry
    #: standing for "we assumed parity", because no such entry would be true.
    exchange_rates: dict[str, Money] = Field(default_factory=dict)

    #: Every tag the broker returned, unparsed, for audit and later use.
    #: Per-currency ledger rows are keyed ``TAG:CCY`` so that a tag reported
    #: once per currency does not overwrite itself in arrival order.
    raw_tags: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _the_base_currency_is_not_quoted_against_itself(self) -> BrokerAccount:
        base = self.currency.upper()
        if base in {code.upper() for code in self.exchange_rates}:
            raise ValueError(
                f"{base} is the account's base currency, so an exchange rate into itself is "
                f"not an observation. Storing one would put an editable factor where the "
                f"identity belongs"
            )
        bad = sorted(code for code, rate in self.exchange_rates.items() if rate <= 0)
        if bad:
            raise ValueError(
                f"exchange rate(s) for {', '.join(bad)} are not positive. A broker that "
                f"reported no rate must be recorded as having reported none, never as zero"
            )
        return self


class BrokerPosition(ImmutableModel):
    """A single position as reported by the broker.

    Supports stock and option positions; option-specific fields are ``None``
    for non-option instruments.
    """

    account_id: BrokerSymbol
    symbol: BrokerSymbol
    security_type: SecurityType
    as_of: UtcDatetime
    source: Identifier

    contract_id: int | None = None
    local_symbol: str | None = None
    currency: str | None = None
    multiplier: int | None = Field(default=None, ge=1)

    #: Signed: negative means short. Fractional quantities are possible for
    #: some instruments, so this is a Decimal rather than an int.
    quantity: Money
    average_cost: Money | None = None

    # Option-specific.
    expiration: date | None = None
    strike: Money | None = Field(default=None, gt=0)
    right: OptionRight | None = None

    market_price: Money | None = None
    market_value: Money | None = None
    unrealized_pnl: Money | None = None
    realized_pnl: Money | None = None

    @model_validator(mode="after")
    def _option_fields_match_security_type(self) -> BrokerPosition:
        if self.security_type in (SecurityType.OPTION, SecurityType.FUTURE_OPTION):
            missing = [
                name
                for name, value in (
                    ("expiration", self.expiration),
                    ("strike", self.strike),
                    ("right", self.right),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"option position {self.symbol} is missing: {', '.join(missing)}")
        return self

    @property
    def is_option(self) -> bool:
        return self.security_type in (SecurityType.OPTION, SecurityType.FUTURE_OPTION)


class BrokerOrder(ImmutableModel):
    """An order known to the broker.

    An order is not a position and a submitted order is not a filled order;
    both distinctions are load-bearing during reconciliation.
    """

    broker_order_id: BrokerSymbol
    account_id: BrokerSymbol | None = None
    as_of: UtcDatetime
    source: Identifier

    client_order_id: str | None = None
    perm_id: int | None = None
    contract_id: int | None = None
    symbol: BrokerSymbol
    security_type: SecurityType
    local_symbol: str | None = None

    side: OrderSide
    quantity: Money = Field(gt=0)
    order_type: str
    limit_price: Money | None = None
    stop_price: Money | None = None
    time_in_force: str | None = None

    status: OrderStatus
    filled_quantity: Money = Field(default=Decimal("0"), ge=0)
    remaining_quantity: Money | None = Field(default=None, ge=0)
    average_fill_price: Money | None = None

    submitted_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class BrokerExecution(ImmutableModel):
    """A fill reported by the broker.

    Positions are reconstructed from executions, never from submitted orders.
    """

    execution_id: BrokerSymbol
    broker_order_id: BrokerSymbol | None = None
    account_id: BrokerSymbol | None = None
    as_of: UtcDatetime
    source: Identifier

    contract_id: int | None = None
    symbol: BrokerSymbol
    security_type: SecurityType
    side: OrderSide

    quantity: Money = Field(gt=0)
    price: Money = Field(gt=0)
    executed_at: UtcDatetime
    commission: Money | None = None
    currency: str | None = None


class MarketDataSnapshot(ImmutableModel):
    """A quote, with its provenance attached.

    ``origin`` is required: a consumer must always be able to tell a live
    broker quote from delayed, cached or simulated data. When no real data is
    available the origin is ``UNAVAILABLE`` and every price field is ``None``
    — no price is ever invented.
    """

    symbol: BrokerSymbol
    security_type: SecurityType
    as_of: UtcDatetime
    source: Identifier
    origin: MarketDataOrigin
    data_quality: DataQuality = DataQuality.OK

    contract_id: int | None = None
    currency: str | None = None
    bid: Money | None = None
    ask: Money | None = None
    last: Money | None = None
    close: Money | None = None
    #: Current-session cumulative share volume, exactly as the broker reported
    #: it. From IBKR this is tick 8 (``VOLUME``) or tick 74
    #: (``DELAYED_VOLUME``). **Tick 74 is known to arrive corrupted** — see the
    #: IBKR market-data translation module for the wire capture — so the value
    #: is stored verbatim, flagged by the quality engine, and never rescaled.
    #: Do not use it as a liquidity measure; use :attr:`average_daily_volume`.
    volume: Money | None = None
    #: Average daily share volume over IBKR's trailing 90-day window: tick 21
    #: (``avVolume``), requested through generic tick 165. A *separate*
    #: observation from :attr:`volume`, with different semantics — it is not a
    #: repaired or rescaled form of it, and neither field ever falls back to
    #: the other.
    average_daily_volume: Money | None = None

    @model_validator(mode="after")
    def _unavailable_carries_no_prices(self) -> MarketDataSnapshot:
        if self.origin is MarketDataOrigin.UNAVAILABLE:
            priced = [
                name
                for name, value in (
                    ("bid", self.bid),
                    ("ask", self.ask),
                    ("last", self.last),
                    ("close", self.close),
                )
                if value is not None
            ]
            if priced:
                raise ValueError(
                    f"origin UNAVAILABLE must carry no prices, but got: {', '.join(priced)}"
                )
        return self

    @property
    def has_quote(self) -> bool:
        return self.origin is not MarketDataOrigin.UNAVAILABLE and (
            self.bid is not None or self.ask is not None or self.last is not None
        )


class OptionChainSnapshot(ImmutableModel):
    """Normalised option chain metadata for one underlying.

    Milestone 2 proves the chain can be requested and normalised. It does not
    select a contract, rank anything, or recommend a trade.
    """

    underlying: BrokerSymbol
    as_of: UtcDatetime
    source: Identifier
    origin: MarketDataOrigin

    underlying_contract_id: int | None = None
    exchange: str | None = None
    trading_class: str | None = None
    multiplier: int | None = Field(default=None, ge=1)

    expirations: list[date] = Field(default_factory=list)
    strikes: list[Money] = Field(default_factory=list)
    rights: list[OptionRight] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sorted_and_unique(self) -> OptionChainSnapshot:
        """Deterministic ordering: the same chain must normalise identically."""
        if list(self.expirations) != sorted(set(self.expirations)):
            raise ValueError("expirations must be sorted and unique")
        if list(self.strikes) != sorted(set(self.strikes)):
            raise ValueError("strikes must be sorted and unique")
        return self


class OptionQuoteSnapshot(ImmutableModel):
    """A quote for ONE option contract, with its provenance attached.

    Deliberately not a :class:`MarketDataSnapshot`. An option quote carries
    Greeks and an underlying price, and its *contract* — strike, expiry, right
    — is part of the observation rather than a symbol: two SPY options with
    different strikes are different instruments, not two quotes for SPY.

    Every measured field is optional and ``None`` means **not available**,
    which is load-bearing here in a way it is not for a stock. IBKR reports an
    unavailable option price as ``-1`` and an uncomputed Greek as ``-2``
    (observed on the delayed feed with the market closed; see the IBKR
    market-data translation module). Reading either at face value would put a
    negative price, or a fabricated sensitivity, into a decision about money.
    A missing bid is a missing bid — never the ask, never the last, never a
    midpoint conjured from one side.
    """

    contract: BrokerContract
    as_of: UtcDatetime
    source: Identifier
    origin: MarketDataOrigin
    data_quality: DataQuality = DataQuality.OK

    bid: Money | None = Field(default=None, gt=0)
    ask: Money | None = Field(default=None, gt=0)
    last: Money | None = Field(default=None, gt=0)
    close: Money | None = Field(default=None, gt=0)
    #: Contract volume for the session, exactly as reported. Never rescaled.
    volume: Money | None = Field(default=None, ge=0)
    open_interest: Money | None = Field(default=None, ge=0)

    #: Greeks, unconstrained in sign: delta is negative for a put and theta is
    #: normally negative for a long option, so a ``ge=0`` here would reject
    #: correct data.
    implied_volatility: Money | None = Field(default=None, ge=0)
    delta: Money | None = None
    gamma: Money | None = None
    theta: Money | None = None
    vega: Money | None = None
    underlying_price: Money | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _option_contract_only(self) -> OptionQuoteSnapshot:
        """The contract must actually be an option, and identify itself.

        A quote whose contract has no strike, expiry or right cannot be matched
        to anything downstream. Storing it would put an unaddressable record in
        a store whose whole purpose is to answer "what did this contract cost".
        """
        if self.contract.security_type is not SecurityType.OPTION:
            raise ValueError(
                f"an option quote needs an OPTION contract, got {self.contract.security_type.value}"
            )
        missing = [
            name
            for name, value in (
                ("expiration", self.contract.expiration),
                ("strike", self.contract.strike),
                ("right", self.contract.right),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"an option quote cannot identify its contract without: {', '.join(missing)}"
            )
        return self

    @model_validator(mode="after")
    def _unavailable_carries_no_prices(self) -> OptionQuoteSnapshot:
        if self.origin is MarketDataOrigin.UNAVAILABLE:
            priced = [
                name
                for name, value in (
                    ("bid", self.bid),
                    ("ask", self.ask),
                    ("last", self.last),
                    ("close", self.close),
                )
                if value is not None
            ]
            if priced:
                raise ValueError(
                    f"origin UNAVAILABLE must carry no prices, but got: {', '.join(priced)}"
                )
        return self

    @property
    def has_quote(self) -> bool:
        """Whether any price at all was reported."""
        return self.origin is not MarketDataOrigin.UNAVAILABLE and any(
            value is not None for value in (self.bid, self.ask, self.last, self.close)
        )

    @property
    def has_two_sided_quote(self) -> bool:
        """Whether BOTH sides were reported.

        The distinction matters: a cost is taken from the ask and a spread
        needs both sides, so a one-sided quote is not a priced contract.
        """
        return self.bid is not None and self.ask is not None


class BrokerHealth(ImmutableModel):
    """Diagnostic result of a broker health check.

    Carries no credentials: host and port are operational facts, usernames and
    passwords are not, and this object is logged and printed.
    """

    broker: Identifier
    state: BrokerConnectionState
    as_of: UtcDatetime
    trading_mode: TradingMode

    host: str | None = None
    port: int | None = None
    account_id: BrokerSymbol | None = None
    read_only: bool = True
    latency_ms: float | None = Field(default=None, ge=0)
    last_successful_communication: UtcDatetime | None = None
    server_version: int | None = None
    error: str | None = None

    @property
    def is_usable(self) -> bool:
        """Whether it is safe to rely on this broker right now."""
        return self.state is BrokerConnectionState.CONNECTED


class ReconciliationDiscrepancy(ImmutableModel):
    """One specific disagreement between internal state and broker state.

    Both sides are recorded verbatim. No resolution is proposed: the system
    reports the discrepancy and stops, it does not guess which side is stale.
    """

    discrepancy_type: DiscrepancyType
    identifier: str
    description: str
    internal_value: str | None = None
    broker_value: str | None = None


class ReconciliationReport(ImmutableModel):
    """Outcome of comparing internal state against broker state.

    A ``MISMATCH`` blocks new executions until it is resolved or explicitly
    classified as safe (specification section 20).
    """

    as_of: UtcDatetime
    broker: Identifier
    status: ReconciliationStatus
    discrepancies: list[ReconciliationDiscrepancy] = Field(default_factory=list)

    positions_compared: int = Field(default=0, ge=0)
    orders_compared: int = Field(default=0, ge=0)
    executions_compared: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _status_matches_discrepancies(self) -> ReconciliationReport:
        if self.status is ReconciliationStatus.MATCHED and self.discrepancies:
            raise ValueError("MATCHED cannot carry discrepancies")
        if self.status is ReconciliationStatus.MISMATCH and not self.discrepancies:
            raise ValueError("MISMATCH must name at least one discrepancy")
        return self

    @property
    def blocks_new_executions(self) -> bool:
        """Fail safe: anything other than a clean match stops new trading.

        An unreachable broker blocks too — unknown broker state is not a safe
        state to open a position from.
        """
        return self.status is not ReconciliationStatus.MATCHED
