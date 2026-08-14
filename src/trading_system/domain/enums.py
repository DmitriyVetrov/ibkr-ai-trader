"""Canonical enumerations for the trading system.

Every enum is a :class:`~enum.StrEnum` so that values serialise to plain JSON
strings and round-trip through the workflow-boundary schemas in ``schemas/``.

These values are part of the persisted trade record. Renaming a member is a
breaking change to historical snapshots — add a new member instead.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = [
    "AcquisitionProvenance",
    "AllocationOutcome",
    "AllocationPolicy",
    "AllocationReason",
    "AllocationRunStatus",
    "BarInterval",
    "BrokerConnectionState",
    "BrokerReadStatus",
    "BudgetSource",
    "ClaimSupport",
    "CollectionOutcome",
    "ConfidenceLevel",
    "ContractRejectionReason",
    "ContractSelectionStatus",
    "CorporateEventType",
    "DataGapStatus",
    "DataQuality",
    "DataQualityIssue",
    "DataType",
    "DecisionMethod",
    "Direction",
    "DiscrepancyType",
    "EvidenceDirection",
    "EvidenceKind",
    "EvidenceStance",
    "ExecutionEventType",
    "ExecutionIntent",
    "ExecutionReasonCode",
    "ExecutionRunStatus",
    "ExecutionState",
    "ExitAction",
    "ExitDecisionType",
    "ExitPolicyKind",
    "ExitQuoteField",
    "ExitReason",
    "ExitReasonCode",
    "ExitRunStatus",
    "ExpectedMagnitude",
    "ExpirationSelectionPolicy",
    "LegAction",
    "MarketDataOrigin",
    "MarketEventType",
    "MarketHypothesis",
    "MaxLossBasis",
    "OptionDataField",
    "OptionRight",
    "Optionability",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PositionLifecycleEventType",
    "PositionLifecycleState",
    "PositionState",
    "PriceSource",
    "ReconciliationEventType",
    "ReconciliationFindingType",
    "ReconciliationRunStatus",
    "ReconciliationSeverity",
    "ReconciliationStatus",
    "RegulatoryFormType",
    "RelevanceLevel",
    "ResearchDataGap",
    "ResearchStatus",
    "ReservationEventType",
    "ReservationReasonCode",
    "ReservationState",
    "RiskCategory",
    "RiskCheckOutcome",
    "RiskLimitScope",
    "RiskOutcome",
    "RiskReasonCode",
    "SecurityType",
    "SelectionMethod",
    "SourceTier",
    "StrategyAction",
    "StrategySelectionReason",
    "StrategySelectionStatus",
    "StrategyType",
    "StrikeSelectionPolicy",
    "StructureStatus",
    "ThesisConditionOutcome",
    "ThesisStatus",
    "TimeInForce",
    "TradingMode",
    "TrailingStopState",
    "UniverseEligibility",
    "UniverseRejectionReason",
    "UniverseSelectionReason",
    "UniverseSelectionStatus",
    "UniverseSourceKind",
]


@unique
class TradingMode(StrEnum):
    """Execution mode of the runtime.

    ``PAPER`` is the default. ``LIVE`` additionally requires explicit safety
    configuration; see ``infrastructure.settings.Settings``.
    """

    DRY_RUN = "DRY_RUN"
    PAPER = "PAPER"
    LIVE = "LIVE"


@unique
class PositionState(StrEnum):
    """Lifecycle state of a trade candidate / position.

    Ordering of the members follows the happy path through the workflow. The
    legal transitions are defined in :mod:`trading_system.domain.state_machine`,
    not here.
    """

    # --- progression -------------------------------------------------------
    DISCOVERED = "DISCOVERED"
    RESEARCHED = "RESEARCHED"
    STRATEGY_SELECTED = "STRATEGY_SELECTED"
    CONTRACT_SELECTED = "CONTRACT_SELECTED"
    ALLOCATED = "ALLOCATED"
    RISK_APPROVED = "RISK_APPROVED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    OPEN = "OPEN"
    MONITORING = "MONITORING"
    EXIT_TRIGGERED = "EXIT_TRIGGERED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"

    # --- terminal / rejection ---------------------------------------------
    NO_TRADE = "NO_TRADE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


@unique
class MarketHypothesis(StrEnum):
    """Primary market hypothesis assigned by the Market Researcher agent.

    Exactly one hypothesis per underlying (specification section 6). The
    vocabulary is closed: an outlook that fits none of A-D is ``E`` with a
    structured explanation, never a sixth category.

    ``A`` and ``D`` are **not** the same claim, and collapsing them would lose
    the only thing that distinguishes them operationally — whether there is a
    dated catalyst to position around:

    ``A``
        A large move is likely and no specific catalyst is required. The
        evidence is about the *regime*: elevated volatility, conflicting
        pressures, market structure. Direction cannot be established.
    ``D``
        A specific, identifiable, dated future event is expected to produce a
        sharp move. Without a named event that the supplied data actually
        contains, ``D`` is invalid — see
        :mod:`trading_system.research.validation`.
    """

    A = "A"  # Strong move expected, no specific catalyst; direction uncertain
    B = "B"  # Predominantly bullish
    C = "C"  # Predominantly bearish
    D = "D"  # Sharp move expected after a specific event; direction uncertain
    E = "E"  # Other; structured explanation required


@unique
class Direction(StrEnum):
    """Directional bias of a research conclusion."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    UNCERTAIN = "UNCERTAIN"


@unique
class ExpectedMagnitude(StrEnum):
    """Coarse magnitude bucket for an expected move.

    Deliberately categorical: a precise percentage forecast from an LLM would
    imply more accuracy than the evidence supports. Numeric thresholds for each
    bucket belong in configuration, not in the agent output.
    """

    SMALL = "SMALL"
    MODERATE = "MODERATE"
    LARGE = "LARGE"
    EXTREME = "EXTREME"


@unique
class StrategyType(StrEnum):
    """Option strategies the system is allowed to select.

    The authoritative allow-list is ``config/strategies/*.yaml``. A member here
    without a corresponding config file is not tradeable.
    """

    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    LONG_STRADDLE = "LONG_STRADDLE"
    LONG_STRANGLE = "LONG_STRANGLE"


@unique
class StrategyAction(StrEnum):
    """Outcome of the Options Strategy agent (specification section 7)."""

    BUY = "BUY"
    NO_TRADE = "NO_TRADE"


@unique
class OptionRight(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


@unique
class LegAction(StrEnum):
    """Direction of an individual leg within a strategy."""

    BUY = "BUY"
    SELL = "SELL"


@unique
class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@unique
class TimeInForce(StrEnum):
    DAY = "DAY"
    GTC = "GTC"


@unique
class OrderStatus(StrEnum):
    """Order status as reported by the broker.

    This mirrors broker reality, never internal intent: it is only ever set
    from a broker response or from the simulator.
    """

    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@unique
class RiskOutcome(StrEnum):
    """Deterministic risk engine verdict. An AI agent cannot override this."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@unique
class RiskReasonCode(StrEnum):
    """Machine-readable reasons for a risk verdict (specification section 11).

    Rejections must always carry at least one code so that downstream
    evaluation can aggregate *why* trades were blocked.
    """

    OK = "OK"
    CAMPAIGN_BUDGET_EXCEEDED = "CAMPAIGN_BUDGET_EXCEEDED"
    MAX_ALLOCATION_PER_TRADE_EXCEEDED = "MAX_ALLOCATION_PER_TRADE_EXCEEDED"
    MAX_POSITIONS_EXCEEDED = "MAX_POSITIONS_EXCEEDED"
    MAX_TOTAL_OPEN_RISK_EXCEEDED = "MAX_TOTAL_OPEN_RISK_EXCEEDED"
    UNDERLYING_CONCENTRATION_EXCEEDED = "UNDERLYING_CONCENTRATION_EXCEEDED"
    STRATEGY_CONCENTRATION_EXCEEDED = "STRATEGY_CONCENTRATION_EXCEEDED"
    DIRECTIONAL_EXPOSURE_EXCEEDED = "DIRECTIONAL_EXPOSURE_EXCEEDED"
    DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    OPTION_PRICE_OUT_OF_RANGE = "OPTION_PRICE_OUT_OF_RANGE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    DTE_OUT_OF_RANGE = "DTE_OUT_OF_RANGE"
    BROKER_STATE_UNKNOWN = "BROKER_STATE_UNKNOWN"
    RECONCILIATION_ERROR = "RECONCILIATION_ERROR"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    TRADING_MODE_NOT_PERMITTED = "TRADING_MODE_NOT_PERMITTED"
    LIVE_MODE_GUARD_NOT_SATISFIED = "LIVE_MODE_GUARD_NOT_SATISFIED"

    # --- Milestone 7 -------------------------------------------------------
    #
    # Added rather than duplicated into a parallel vocabulary: one authoritative
    # list of reasons a trade was refused keeps evaluation able to aggregate
    # across milestones. ``schemas/risk_decision.json`` enumerates the same set.

    #: The campaign has capital left, but not enough for even one unit.
    INSUFFICIENT_CAMPAIGN_BUDGET = "INSUFFICIENT_CAMPAIGN_BUDGET"
    #: The broker reports less available capital than the campaign would allow.
    #: The most restrictive limit wins; the campaign envelope is not permission
    #: to spend money the account does not have.
    INSUFFICIENT_BUYING_POWER = "INSUFFICIENT_BUYING_POWER"
    MAX_RISK_PER_TRADE_EXCEEDED = "MAX_RISK_PER_TRADE_EXCEEDED"
    MAX_CONTRACT_QUANTITY_EXCEEDED = "MAX_CONTRACT_QUANTITY_EXCEEDED"
    MAX_POSITIONS_PER_UNDERLYING_EXCEEDED = "MAX_POSITIONS_PER_UNDERLYING_EXCEEDED"
    MAX_NEW_POSITIONS_PER_RUN_REACHED = "MAX_NEW_POSITIONS_PER_RUN_REACHED"
    #: A whole number of contracts would cost less than the configured floor.
    #: Deliberately distinct from ZERO_QUANTITY: one says the position would be
    #: too small to be worth holding, the other that none was affordable.
    MIN_ALLOCATION_NOT_MET = "MIN_ALLOCATION_NOT_MET"
    #: Every limit passed but the floor of the quantity calculation is zero.
    ZERO_QUANTITY = "ZERO_QUANTITY"
    #: The reserve fraction is not spendable capital, by policy.
    CAMPAIGN_RESERVE_PROTECTED = "CAMPAIGN_RESERVE_PROTECTED"
    #: This exact opportunity already holds an approved capital reservation.
    DUPLICATE_OPPORTUNITY = "DUPLICATE_OPPORTUNITY"
    #: A price was present but unusable: zero, negative, or not attributable to
    #: the selected contract.
    INVALID_PRICE = "INVALID_PRICE"
    #: No price at all. Never replaced with zero, a midpoint from one side, or
    #: the last thing we happened to see.
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    INVALID_MULTIPLIER = "INVALID_MULTIPLIER"
    #: The strategy's maximum loss is not bounded by a model this engine knows
    #: how to compute. Fail closed: an unquantified loss is not a small one.
    MAX_LOSS_UNDEFINED = "MAX_LOSS_UNDEFINED"
    #: The upstream data layer judged the inputs unusable. Never re-graded here.
    DATA_QUALITY_FAILED = "DATA_QUALITY_FAILED"
    #: A record that was not knowable at the decision instant reached the
    #: engine. A correctness bug, never a market outcome.
    POINT_IN_TIME_ERROR = "POINT_IN_TIME_ERROR"
    INVALID_ACCOUNT_SNAPSHOT = "INVALID_ACCOUNT_SNAPSHOT"
    ACCOUNT_SNAPSHOT_STALE = "ACCOUNT_SNAPSHOT_STALE"
    ACCOUNT_SNAPSHOT_UNAVAILABLE = "ACCOUNT_SNAPSHOT_UNAVAILABLE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    #: A conversion was required and no deterministic rate is configured. An
    #: arbitrary rate would be an invented price.
    FX_CONVERSION_UNAVAILABLE = "FX_CONVERSION_UNAVAILABLE"
    #: Ranked, but below the configured floor for deserving capital.
    BELOW_MIN_OPPORTUNITY_SCORE = "BELOW_MIN_OPPORTUNITY_SCORE"
    #: Realised profit and loss for the day is not tracked yet, so the daily
    #: loss limit could not be evaluated. Recorded rather than assumed passed.
    DAILY_LOSS_NOT_TRACKED = "DAILY_LOSS_NOT_TRACKED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


@unique
class ThesisStatus(StrEnum):
    """Thesis Monitor verdict (specification section 18)."""

    VALID = "VALID"
    WEAKENING = "WEAKENING"
    INVALIDATED = "INVALIDATED"
    UNKNOWN = "UNKNOWN"


@unique
class ExitAction(StrEnum):
    """Exit engine decision. ``HOLD`` is the default, no-action outcome."""

    HOLD = "HOLD"
    SELL = "SELL"


@unique
class ExitReason(StrEnum):
    """Why an exit was triggered (specification section 19).

    The **narrow** Milestone 1 boundary vocabulary.
    :class:`ExitReasonCode` is Milestone 10's wide audit vocabulary and
    projects onto this one, exactly as research, strategy, execution and
    reconciliation project onto their Milestone 1 shapes.
    """

    TRAILING_STOP = "TRAILING_STOP"
    EXPIRATION_POLICY = "EXPIRATION_POLICY"
    THESIS_INVALIDATION = "THESIS_INVALIDATION"
    RISK_LIMIT = "RISK_LIMIT"
    BROKER_SAFETY = "BROKER_SAFETY"
    EMERGENCY = "EMERGENCY"

    # --- Milestone 10 ------------------------------------------------------
    #
    # Added rather than forced onto an existing member. Milestone 10 exits a
    # position that reached its profit target, and the closest existing member
    # is ``RISK_LIMIT`` — which would record every successful trade as having
    # breached a risk limit. Adding a member is additive and keeps *one*
    # authoritative list evaluation can aggregate across milestones;
    # ``schemas/exit_decision.json`` enumerates the same set.
    TAKE_PROFIT = "TAKE_PROFIT"


@unique
class SourceTier(StrEnum):
    """Trust tier of a research source (specification section 6).

    A low-tier source is never authoritative merely because it ranked highly in
    a search result.
    """

    TIER_1 = "TIER_1"  # regulatory filings, IR, exchanges, official announcements
    TIER_2 = "TIER_2"  # established financial news wires
    TIER_3 = "TIER_3"  # specialised financial / industry publications
    TIER_4 = "TIER_4"  # general web


@unique
class DataQuality(StrEnum):
    """Quality classification attached to every persisted snapshot."""

    OK = "OK"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNUSABLE = "UNUSABLE"


@unique
class ReconciliationStatus(StrEnum):
    """Result of comparing internal state against broker state.

    ``MISMATCH`` blocks new executions until resolved or explicitly classified
    as safe (specification section 20).
    """

    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"


@unique
class SecurityType(StrEnum):
    """Instrument class as reported by the broker.

    The system trades options, but a broker account may hold stock — assigned
    shares, for instance — so an accurate picture of broker state needs both.
    """

    STOCK = "STOCK"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    FUTURE_OPTION = "FUTURE_OPTION"
    CASH = "CASH"
    INDEX = "INDEX"
    OTHER = "OTHER"


@unique
class OrderSide(StrEnum):
    """Side of a broker order."""

    BUY = "BUY"
    SELL = "SELL"


@unique
class BrokerConnectionState(StrEnum):
    """Connection state reported by a broker health check.

    ``UNKNOWN`` is distinct from ``DISCONNECTED`` on purpose: not knowing the
    broker's state is itself a reason not to trade, and must not be quietly
    collapsed into a definite answer.
    """

    CONNECTED = "CONNECTED"
    CONNECTING = "CONNECTING"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


@unique
class MarketDataOrigin(StrEnum):
    """Where a quote actually came from.

    Recorded on every snapshot so that simulated or delayed data can never be
    mistaken for a live broker quote. There is no silent fallback: if real data
    is unavailable the origin is ``UNAVAILABLE`` and no price is invented.

    The ``PROVIDER_*`` and ``HISTORICAL`` members were added in Milestone 3 for
    non-broker data sources. ``CACHED`` and ``HISTORICAL`` are deliberately
    distinct from the live members: data served from a cache or from the
    historical store must never be relabelled as realtime, however recent it is
    (specification section 8 of the Milestone 3 brief).
    """

    BROKER_REALTIME = "BROKER_REALTIME"
    BROKER_DELAYED = "BROKER_DELAYED"
    BROKER_FROZEN = "BROKER_FROZEN"
    PROVIDER_REALTIME = "PROVIDER_REALTIME"
    PROVIDER_DELAYED = "PROVIDER_DELAYED"
    HISTORICAL = "HISTORICAL"
    CACHED = "CACHED"
    SIMULATED = "SIMULATED"
    UNAVAILABLE = "UNAVAILABLE"


@unique
class DiscrepancyType(StrEnum):
    """Kind of disagreement found between internal state and broker state."""

    POSITION_MISMATCH = "POSITION_MISMATCH"
    ORDER_MISMATCH = "ORDER_MISMATCH"
    EXECUTION_MISMATCH = "EXECUTION_MISMATCH"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Data layer (Milestone 3)
# ---------------------------------------------------------------------------
@unique
class DataType(StrEnum):
    """Kind of data a provider returns and the repository stores.

    Part of every snapshot identifier, so renaming a member orphans previously
    stored history. Add a member instead.
    """

    MARKET_QUOTE = "MARKET_QUOTE"
    MARKET_BAR = "MARKET_BAR"
    OPTION_CHAIN = "OPTION_CHAIN"
    OPTION_QUOTE = "OPTION_QUOTE"
    OPTION_SNAPSHOT = "OPTION_SNAPSHOT"
    NEWS_ARTICLE = "NEWS_ARTICLE"
    CORPORATE_EVENT = "CORPORATE_EVENT"
    FUNDAMENTAL_SNAPSHOT = "FUNDAMENTAL_SNAPSHOT"
    REGULATORY_EVENT = "REGULATORY_EVENT"


@unique
class BarInterval(StrEnum):
    """Aggregation interval of a historical bar."""

    MINUTE_1 = "MINUTE_1"
    MINUTE_5 = "MINUTE_5"
    MINUTE_15 = "MINUTE_15"
    HOUR_1 = "HOUR_1"
    DAY_1 = "DAY_1"
    WEEK_1 = "WEEK_1"


@unique
class DataQualityIssue(StrEnum):
    """Machine-readable data quality findings.

    An issue is a *finding about* a record, never a modification of it. The raw
    value that triggered the issue is always preserved (Milestone 3 brief
    sections 2.3 and 43).
    """

    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNEXPECTED_NULL = "UNEXPECTED_NULL"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    STALE_DATA = "STALE_DATA"
    NEGATIVE_PRICE = "NEGATIVE_PRICE"
    ZERO_PRICE = "ZERO_PRICE"
    CROSSED_BID_ASK = "CROSSED_BID_ASK"
    WIDE_SPREAD = "WIDE_SPREAD"
    IMPLAUSIBLE_PRICE = "IMPLAUSIBLE_PRICE"
    SUSPICIOUS_VOLUME = "SUSPICIOUS_VOLUME"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    IMPLAUSIBLE_STRIKE = "IMPLAUSIBLE_STRIKE"
    INVALID_MULTIPLIER = "INVALID_MULTIPLIER"
    INVALID_OPTION_RIGHT = "INVALID_OPTION_RIGHT"
    INVALID_EXPIRATION = "INVALID_EXPIRATION"
    EXPIRED_CONTRACT = "EXPIRED_CONTRACT"
    IMPLAUSIBLE_IMPLIED_VOLATILITY = "IMPLAUSIBLE_IMPLIED_VOLATILITY"
    IMPLAUSIBLE_GREEK = "IMPLAUSIBLE_GREEK"
    MISSING_OPEN_INTEREST = "MISSING_OPEN_INTEREST"
    MISSING_IMPLIED_VOLATILITY = "MISSING_IMPLIED_VOLATILITY"
    DUPLICATE_RECORD = "DUPLICATE_RECORD"
    CONTRADICTORY_FIELDS = "CONTRADICTORY_FIELDS"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    ORIGIN_MISREPRESENTED = "ORIGIN_MISREPRESENTED"
    EMPTY_PAYLOAD = "EMPTY_PAYLOAD"


@unique
class CollectionOutcome(StrEnum):
    """Result of one collection attempt (Milestone 3 brief section 55).

    Distinguishing these matters operationally: ``NO_DATA`` from a healthy
    provider is a fact about the market, ``PROVIDER_UNAVAILABLE`` is a fact
    about our plumbing, and only the first should ever be read as evidence.
    """

    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    NO_DATA = "NO_DATA"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    INVALID_DATA = "INVALID_DATA"
    QUALITY_REJECTED = "QUALITY_REJECTED"
    SKIPPED_UNCHANGED = "SKIPPED_UNCHANGED"


@unique
class DataGapStatus(StrEnum):
    """Whether the historical store has the coverage a consumer expects.

    ``NO_COVERAGE`` and ``GAP_DETECTED`` are distinct: never having collected a
    symbol is a different situation from having collected it and then missed a
    stretch, and only the second suggests something broke.
    """

    NO_GAP = "NO_GAP"
    GAP_DETECTED = "GAP_DETECTED"
    NO_COVERAGE = "NO_COVERAGE"
    UNKNOWN = "UNKNOWN"


@unique
class CorporateEventType(StrEnum):
    """Kind of corporate event (Milestone 3 brief section 26)."""

    EARNINGS = "EARNINGS"
    INVESTOR_DAY = "INVESTOR_DAY"
    DIVIDEND = "DIVIDEND"
    SPLIT = "SPLIT"
    MERGER_ACQUISITION = "MERGER_ACQUISITION"
    REGULATORY = "REGULATORY"
    GUIDANCE = "GUIDANCE"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    OTHER = "OTHER"


@unique
class RegulatoryFormType(StrEnum):
    """Regulatory filing form type.

    ``OTHER`` covers every form we do not model explicitly; the provider always
    preserves the exact form string alongside it, so nothing is lost by mapping
    an unrecognised form here rather than guessing at it.
    """

    FORM_10K = "10-K"
    FORM_10Q = "10-Q"
    FORM_8K = "8-K"
    FORM_S1 = "S-1"
    FORM_4 = "4"
    FORM_13F = "13F"
    FORM_DEF14A = "DEF 14A"
    OTHER = "OTHER"


# ---------------------------------------------------------------------------
# Universe selection (Milestone 4)
# ---------------------------------------------------------------------------
@unique
class UniverseSourceKind(StrEnum):
    """Where the candidate pool comes from.

    Explicit and versioned rather than implicit: "which assets were even
    considered on date T" is part of reconstructing a decision, and a universe
    that silently changed shape is not reproducible. Only ``STATIC`` and
    ``FILE`` are implemented; the rest are named so the configuration can
    express them and so a request for one fails loudly instead of being
    approximated. Fabricating an index constituent list from memory would be
    inventing data.
    """

    #: Symbols listed inline in ``config/universe.yaml``.
    STATIC = "STATIC"
    #: Symbols read from a newline-delimited file.
    FILE = "FILE"
    #: A hand-curated list distinct from STATIC only by intent.
    CUSTOM = "CUSTOM"
    SP500 = "SP500"
    NASDAQ100 = "NASDAQ100"
    ETF_UNIVERSE = "ETF_UNIVERSE"
    #: Discovered by scanning the broker. Needs a scanner API, not a quote.
    IBKR_DISCOVERED = "IBKR_DISCOVERED"


@unique
class Optionability(StrEnum):
    """Whether an underlying has listed options.

    ``UNKNOWN`` is a real answer and is never collapsed into ``FALSE``: "we have
    not established that this underlying has options" and "this underlying has
    no options" are different claims, and only the second is evidence.
    """

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


@unique
class UniverseEligibility(StrEnum):
    """Verdict of the deterministic pre-filter. An AI agent cannot change it."""

    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"


@unique
class UniverseRejectionReason(StrEnum):
    """Machine-readable reason a candidate did not reach the agent.

    Every rejection carries one, so "why was AAPL not researched on 10 August"
    is answerable from the stored run without re-deriving anything.
    """

    EXCLUDED_BY_CONFIGURATION = "EXCLUDED_BY_CONFIGURATION"
    DUPLICATE_SYMBOL = "DUPLICATE_SYMBOL"
    SECURITY_TYPE_NOT_ALLOWED = "SECURITY_TYPE_NOT_ALLOWED"
    CURRENCY_NOT_ALLOWED = "CURRENCY_NOT_ALLOWED"
    EXCHANGE_NOT_ALLOWED = "EXCHANGE_NOT_ALLOWED"
    #: No market data was visible at the requested instant at all.
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    #: Data exists but the quality engine says it must not inform research.
    DATA_NOT_RESEARCH_USABLE = "DATA_NOT_RESEARCH_USABLE"
    DATA_STALE = "DATA_STALE"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    PRICE_BELOW_MINIMUM = "PRICE_BELOW_MINIMUM"
    VOLUME_UNAVAILABLE = "VOLUME_UNAVAILABLE"
    VOLUME_BELOW_MINIMUM = "VOLUME_BELOW_MINIMUM"
    OPTIONABILITY_FALSE = "OPTIONABILITY_FALSE"
    OPTIONABILITY_UNKNOWN = "OPTIONABILITY_UNKNOWN"
    #: More candidates passed than ``max_candidates`` allows. Deterministic
    #: truncation, applied by an explicit configured policy, never silently.
    CANDIDATE_LIMIT_EXCEEDED = "CANDIDATE_LIMIT_EXCEEDED"
    #: Deterministically eligible, ranked, and not chosen. Not a data fault.
    NOT_SELECTED_BY_RANKING = "NOT_SELECTED_BY_RANKING"


@unique
class UniverseSelectionReason(StrEnum):
    """Evidence codes a ranking may cite.

    A closed vocabulary, not free text: an agent that could invent a reason
    could justify anything. Each code is checkable against the candidate record
    that was supplied, and :mod:`trading_system.universe.validation` rejects a
    ranking whose own evidence contradicts the code it claims.

    Deliberately absent: anything directional, any option-level property, and
    anything about position size. Those belong to later milestones.
    """

    HIGH_UNDERLYING_LIQUIDITY = "HIGH_UNDERLYING_LIQUIDITY"
    MODERATE_UNDERLYING_LIQUIDITY = "MODERATE_UNDERLYING_LIQUIDITY"
    LOWER_UNDERLYING_LIQUIDITY = "LOWER_UNDERLYING_LIQUIDITY"
    OPTIONS_AVAILABLE = "OPTIONS_AVAILABLE"
    OPTIONABILITY_NOT_ESTABLISHED = "OPTIONABILITY_NOT_ESTABLISHED"
    SUFFICIENT_DATA_QUALITY = "SUFFICIENT_DATA_QUALITY"
    FRESH_MARKET_DATA = "FRESH_MARKET_DATA"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    PRICE_IN_RANGE = "PRICE_IN_RANGE"
    LIMITED_DATA_HISTORY = "LIMITED_DATA_HISTORY"
    #: Eligible and adequate, but ranked below the size limit.
    UNIVERSE_SIZE_LIMIT = "UNIVERSE_SIZE_LIMIT"


@unique
class ConfidenceLevel(StrEnum):
    """Coarse confidence band on a ranking.

    Categorical rather than a probability: a decimal from an LLM implies a
    calibration nobody has measured.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@unique
class SelectionMethod(StrEnum):
    """How the final ordering was produced.

    Recorded on every run so a stored universe can never be mistaken about
    whether a model was involved.
    """

    AI_RANKED = "AI_RANKED"
    #: Deterministic ordering, used only when configuration explicitly allows
    #: it. Never a silent fallback from a failed agent call.
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"


@unique
class UniverseSelectionStatus(StrEnum):
    """Outcome of a universe run.

    ``NO_CANDIDATES`` is a success of the process and a valid final answer:
    nothing was research-ready. The failure states are distinct because they
    demand different responses — bad data, an unreachable model, and a model
    that answered unusably are three different problems.
    """

    SUCCESS = "SUCCESS"
    NO_CANDIDATES = "NO_CANDIDATES"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_INVALID_OUTPUT = "AI_INVALID_OUTPUT"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"

    @property
    def produced_a_universe(self) -> bool:
        """Whether a downstream stage may consume this run's selection."""
        return self is UniverseSelectionStatus.SUCCESS


# ---------------------------------------------------------------------------
# Research (Milestone 5)
# ---------------------------------------------------------------------------
@unique
class ResearchStatus(StrEnum):
    """Outcome of researching one underlying, and of a whole research run.

    The failure states are separate because they demand different responses,
    and because none of them may be read as a market view. A run that could
    report ``AI_UNAVAILABLE`` while still carrying a hypothesis would have
    turned an outage into a trading opinion.

    ``INSUFFICIENT_EVIDENCE`` is a success of the process and a valid final
    answer: the data available at the instant did not support a defensible
    outlook. It is not a bearish signal, not a bullish one, and not a reason to
    relax anything.
    """

    SUCCESS = "SUCCESS"
    #: Nothing about this underlying was visible at the requested instant.
    NO_DATA = "NO_DATA"
    #: Data existed but was too thin, too stale or too degraded to reason from.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    #: Malformed, unparseable, or contract-violating model output.
    AI_INVALID_OUTPUT = "AI_INVALID_OUTPUT"
    #: Well-formed output that the deterministic semantic rules reject —
    #: an unsupported hypothesis, a fabricated evidence id, a confidence the
    #: evidence does not license.
    SEMANTIC_VALIDATION_FAILED = "SEMANTIC_VALIDATION_FAILED"
    #: A record that was not knowable at ``as_of`` reached the pipeline. A
    #: correctness bug in storage or gathering, never a market outcome.
    POINT_IN_TIME_ERROR = "POINT_IN_TIME_ERROR"
    #: Beyond the seven states the Milestone 5 brief requires, because the two
    #: below are real outcomes that would otherwise have to be misreported as
    #: one of the above.
    #:
    #: The configuration or the upstream universe could not be resolved. Not a
    #: fact about the market, and distinct from NO_DATA for that reason.
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    #: Selected by the universe but not researched, because the run's
    #: configured cost ceiling was already reached. Recorded rather than
    #: silently omitted: "we did not look" and "we looked and found nothing"
    #: are different.
    SKIPPED_COST_LIMIT = "SKIPPED_COST_LIMIT"

    @property
    def produced_an_outlook(self) -> bool:
        """Whether a downstream stage may consume this report's hypothesis."""
        return self is ResearchStatus.SUCCESS


@unique
class EvidenceKind(StrEnum):
    """What sort of fact a piece of evidence is.

    Load-bearing for the A/D distinction: hypothesis ``A`` claims an elevated
    move with *no* required catalyst, so it must rest on evidence that is not
    itself a dated event.
    """

    NEWS = "NEWS"
    CORPORATE_EVENT = "CORPORATE_EVENT"
    MACRO_EVENT = "MACRO_EVENT"
    REGULATORY_FILING = "REGULATORY_FILING"
    FUNDAMENTAL = "FUNDAMENTAL"
    MARKET_DATA = "MARKET_DATA"
    HISTORICAL_PRICE = "HISTORICAL_PRICE"
    OPTION_MARKET = "OPTION_MARKET"
    MARKET_CONTEXT = "MARKET_CONTEXT"

    @property
    def is_dated_event(self) -> bool:
        """Whether this kind names a specific scheduled or announced event."""
        return self in (EvidenceKind.CORPORATE_EVENT, EvidenceKind.MACRO_EVENT)


@unique
class EvidenceDirection(StrEnum):
    """What a piece of evidence implies about the underlying.

    Separate from :class:`EvidenceStance` on purpose. This says what the fact
    points at; the stance says how it relates to the thesis actually stated.
    An item can point up and still contradict a bearish thesis, and both facts
    have to survive into the record.
    """

    SUPPORTS_UP = "SUPPORTS_UP"
    SUPPORTS_DOWN = "SUPPORTS_DOWN"
    #: Implies an elevated magnitude of movement without implying a direction.
    SUPPORTS_LARGE_MOVE = "SUPPORTS_LARGE_MOVE"
    NEUTRAL = "NEUTRAL"


@unique
class EvidenceStance(StrEnum):
    """How a piece of evidence relates to the thesis the report states.

    ``CONTRADICTS`` items are kept, never dropped. A report that hid its
    disagreements would look more confident than the evidence warrants, and the
    confidence policy in :mod:`trading_system.research.validation` reads this
    field directly.
    """

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    NEUTRAL = "NEUTRAL"


@unique
class RelevanceLevel(StrEnum):
    """How much a fact bears on the question, as a coarse band.

    Distinct from a provider-supplied numeric relevance, which is a fact about
    the source and is carried through unchanged.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@unique
class ClaimSupport(StrEnum):
    """Whether a stated claim is backed by evidence the input actually carried.

    Derived deterministically from whether the claim names at least one real
    evidence id — never asserted by the model. An ``UNSUPPORTED`` claim is
    preserved and labelled rather than deleted: what the agent asserted without
    backing is itself worth seeing.
    """

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"


@unique
class MarketEventType(StrEnum):
    """Kind of event that could move an underlying within the horizon.

    Wider than :class:`CorporateEventType` because a rate decision or an
    inflation print is not a corporate action, and narrowing it to the issuer's
    own calendar would make the macro half of hypothesis ``D`` inexpressible.
    """

    EARNINGS = "EARNINGS"
    GUIDANCE = "GUIDANCE"
    INVESTOR_DAY = "INVESTOR_DAY"
    PRODUCT_LAUNCH = "PRODUCT_LAUNCH"
    DIVIDEND = "DIVIDEND"
    SPLIT = "SPLIT"
    MERGER_ACQUISITION = "MERGER_ACQUISITION"
    REGULATORY_DECISION = "REGULATORY_DECISION"
    COURT_DECISION = "COURT_DECISION"
    REGULATORY_FILING = "REGULATORY_FILING"
    CENTRAL_BANK_DECISION = "CENTRAL_BANK_DECISION"
    INFLATION_RELEASE = "INFLATION_RELEASE"
    MACRO_RELEASE = "MACRO_RELEASE"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    OTHER = "OTHER"


@unique
class RiskCategory(StrEnum):
    """Risk dimensions a research report must address (brief section 20).

    Not every category will have a finding, but an absent finding is stated
    explicitly rather than left as a silence a reader could mistake for "no
    risk".
    """

    DIRECTIONAL_RISK = "DIRECTIONAL_RISK"
    EVENT_RISK = "EVENT_RISK"
    MACRO_RISK = "MACRO_RISK"
    COMPANY_SPECIFIC_RISK = "COMPANY_SPECIFIC_RISK"
    DATA_RISK = "DATA_RISK"


@unique
class ResearchDataGap(StrEnum):
    """Something the research input did not have.

    Recorded explicitly so a report can say "there was no implied volatility"
    rather than implying zero, and so the confidence policy can see what was
    missing. ``*_UNAVAILABLE`` never means the value is zero or false.
    """

    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    MARKET_DATA_NOT_RESEARCH_USABLE = "MARKET_DATA_NOT_RESEARCH_USABLE"
    HISTORICAL_CONTEXT_UNAVAILABLE = "HISTORICAL_CONTEXT_UNAVAILABLE"
    REALIZED_VOLATILITY_UNAVAILABLE = "REALIZED_VOLATILITY_UNAVAILABLE"
    NEWS_UNAVAILABLE = "NEWS_UNAVAILABLE"
    EVENTS_UNAVAILABLE = "EVENTS_UNAVAILABLE"
    FUNDAMENTALS_UNAVAILABLE = "FUNDAMENTALS_UNAVAILABLE"
    REGULATORY_UNAVAILABLE = "REGULATORY_UNAVAILABLE"
    OPTION_CONTEXT_UNAVAILABLE = "OPTION_CONTEXT_UNAVAILABLE"
    IMPLIED_VOLATILITY_UNAVAILABLE = "IMPLIED_VOLATILITY_UNAVAILABLE"
    OPTION_VOLUME_UNAVAILABLE = "OPTION_VOLUME_UNAVAILABLE"
    OPEN_INTEREST_UNAVAILABLE = "OPEN_INTEREST_UNAVAILABLE"
    MARKET_CONTEXT_UNAVAILABLE = "MARKET_CONTEXT_UNAVAILABLE"
    SUSPICIOUS_VALUES_PRESENT = "SUSPICIOUS_VALUES_PRESENT"


# ---------------------------------------------------------------------------
# Strategy selection and contract selection (Milestone 6)
#
# Two stages, deliberately separated, with a hard boundary between them:
# the AI selects a *strategy*, deterministic code selects the *contract*.
# ---------------------------------------------------------------------------
@unique
class StrategySelectionStatus(StrEnum):
    """Outcome of choosing a strategy for one underlying, and of a whole run.

    ``SUCCESS`` covers both a ``BUY`` and a ``NO_TRADE`` decision: declining to
    trade is a correct answer, not a failure, and conflating the two would make
    "the system chose not to trade" indistinguishable from "the system broke".

    The failure states are separate because they demand different responses,
    and because none of them may be read as a strategy. A run that could report
    ``AI_UNAVAILABLE`` while still naming a strategy would have turned an outage
    into a trade proposal.
    """

    SUCCESS = "SUCCESS"
    #: No research outlook was available to select from.
    NO_RESEARCH = "NO_RESEARCH"
    #: The data a configured strategy requires was not visible at ``as_of``.
    REQUIRED_DATA_UNAVAILABLE = "REQUIRED_DATA_UNAVAILABLE"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    #: Malformed, unparseable, or contract-violating model output.
    AI_INVALID_OUTPUT = "AI_INVALID_OUTPUT"
    #: Well-formed output the deterministic rules reject — an unknown strategy,
    #: one ineligible for the hypothesis, or a reason its own research refutes.
    SEMANTIC_VALIDATION_FAILED = "SEMANTIC_VALIDATION_FAILED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    #: Researched but not put to a strategy, because the run's configured cost
    #: ceiling was already reached. "We did not look" is not "we found nothing".
    SKIPPED_COST_LIMIT = "SKIPPED_COST_LIMIT"

    @property
    def produced_a_decision(self) -> bool:
        """Whether a downstream stage may consume this decision."""
        return self is StrategySelectionStatus.SUCCESS


@unique
class DecisionMethod(StrEnum):
    """How a strategy decision was reached.

    Recorded on every decision so a stored record can never be mistaken about
    whether a model was involved. ``DETERMINISTIC_ONLY`` is not a fallback for a
    failed model call — it names decisions the deterministic layer reached on
    its own, such as ``NO_TRADE`` when no configured strategy answers the
    hypothesis at all.
    """

    AI_SELECTED = "AI_SELECTED"
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"


@unique
class StrategySelectionReason(StrEnum):
    """Evidence codes a strategy decision may cite.

    A closed vocabulary, not free text: an agent that could invent a reason
    could justify anything. Each code is checkable against the research report
    that was supplied, and :mod:`trading_system.strategies.validation` rejects a
    decision whose own research contradicts the code it claims.

    Deliberately absent: anything naming a strike, an expiry, a contract, a
    quantity or an amount of money. Those belong to later stages.
    """

    #: The chosen strategy declares this hypothesis in its configuration.
    HYPOTHESIS_MATCH = "HYPOTHESIS_MATCH"
    DIRECTIONAL_VIEW_SUPPORTED = "DIRECTIONAL_VIEW_SUPPORTED"
    DIRECTION_UNCERTAIN = "DIRECTION_UNCERTAIN"
    LARGE_MOVE_EXPECTED = "LARGE_MOVE_EXPECTED"
    MODERATE_MOVE_EXPECTED = "MODERATE_MOVE_EXPECTED"
    EVENT_IN_HORIZON = "EVENT_IN_HORIZON"
    NO_EVENT_IN_HORIZON = "NO_EVENT_IN_HORIZON"
    HORIZON_COMPATIBLE = "HORIZON_COMPATIBLE"
    HORIZON_INCOMPATIBLE = "HORIZON_INCOMPATIBLE"
    CONFIDENCE_SUFFICIENT = "CONFIDENCE_SUFFICIENT"
    CONFIDENCE_INSUFFICIENT = "CONFIDENCE_INSUFFICIENT"
    EVIDENCE_SUFFICIENT = "EVIDENCE_SUFFICIENT"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    CONTRADICTING_EVIDENCE = "CONTRADICTING_EVIDENCE"
    DATA_QUALITY_INSUFFICIENT = "DATA_QUALITY_INSUFFICIENT"
    #: No configured strategy answers this hypothesis. Only ever a NO_TRADE.
    NO_ELIGIBLE_STRATEGY = "NO_ELIGIBLE_STRATEGY"
    #: The outlook does not fit any eligible strategy. A judgement, not a fact,
    #: and therefore never contradicted by the report.
    RESEARCH_INCOMPATIBLE = "RESEARCH_INCOMPATIBLE"


@unique
class StrikeSelectionPolicy(StrEnum):
    """How one leg's strike is chosen. Deterministic, never the model's choice.

    Which policy a leg uses comes from ``config/strategies/*.yaml``; the code
    implements the policies and nothing else. A policy the configuration does
    not name is not applied, and no policy is inferred from the strategy's name.
    """

    #: Nearest listed strike to the reference price.
    ATM = "ATM"
    #: Nearest listed strike to a configured absolute delta. Requires delta:
    #: without it the selection is unavailable, never approximated.
    TARGET_DELTA = "TARGET_DELTA"
    #: Nearest listed strike to reference x (1 +/- offset), on the side the
    #: leg's right implies: calls above the reference, puts below it.
    OTM_PERCENT = "OTM_PERCENT"


@unique
class ExpirationSelectionPolicy(StrEnum):
    """How the expiration is chosen from the chain. Deterministic.

    ``EVENT_ALIGNED`` may only be requested by a strategy whose configuration
    says so, and only using an event the research report actually named. An
    expiration is never inferred from prose.
    """

    #: The valid expiration with the smallest days-to-expiration.
    NEAREST_VALID = "NEAREST_VALID"
    #: The valid expiration closest to a configured target DTE.
    TARGET_DTE = "TARGET_DTE"
    #: The first valid expiration on or after the research report's event.
    EVENT_ALIGNED = "EVENT_ALIGNED"


@unique
class OptionDataField(StrEnum):
    """A field a strategy may require before a contract can be selected.

    Listed per strategy in configuration. A required field that is absent is a
    rejection with a named reason — never a value filled in from a model, a
    memory or an approximation.
    """

    CONTRACT_ID = "CONTRACT_ID"
    TRADING_CLASS = "TRADING_CLASS"
    MULTIPLIER = "MULTIPLIER"
    BID = "BID"
    ASK = "ASK"
    LAST = "LAST"
    IMPLIED_VOLATILITY = "IMPLIED_VOLATILITY"
    DELTA = "DELTA"
    GAMMA = "GAMMA"
    THETA = "THETA"
    VEGA = "VEGA"
    VOLUME = "VOLUME"
    OPEN_INTEREST = "OPEN_INTEREST"


@unique
class ContractSelectionStatus(StrEnum):
    """Outcome of selecting contracts for one strategy decision.

    "No contract" is a valid, expected result. The states are distinct because
    an absent chain, a chain with no expiration in range, and a chain whose
    quotes never arrived are three different problems with three different
    fixes, and collapsing them would hide which one occurred.
    """

    SUCCESS = "SUCCESS"
    #: The chain was visible but no expiration satisfied the DTE policy.
    NO_VALID_EXPIRATION = "NO_VALID_EXPIRATION"
    #: An expiration was chosen but no strike satisfied the strike policy.
    NO_VALID_STRIKE = "NO_VALID_STRIKE"
    #: Legs were found but could not be combined into the strategy.
    NO_VALID_CONTRACT = "NO_VALID_CONTRACT"
    #: A field the strategy requires — a quote, a delta, an id — was absent.
    REQUIRED_DATA_UNAVAILABLE = "REQUIRED_DATA_UNAVAILABLE"
    OPTION_CHAIN_UNAVAILABLE = "OPTION_CHAIN_UNAVAILABLE"
    #: The stored chain contradicts itself or the underlying it claims.
    INVALID_CHAIN = "INVALID_CHAIN"
    #: A record that was not knowable at ``as_of`` reached the selector. A
    #: correctness bug in storage, never a market outcome.
    POINT_IN_TIME_ERROR = "POINT_IN_TIME_ERROR"
    #: The decision named a strategy that is not eligible, or not configured.
    STRATEGY_NOT_ELIGIBLE = "STRATEGY_NOT_ELIGIBLE"
    #: The upstream decision was NO_TRADE. Nothing to select, and correctly so.
    NO_TRADE = "NO_TRADE"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"

    @property
    def produced_contracts(self) -> bool:
        """Whether a downstream stage may consume this selection."""
        return self is ContractSelectionStatus.SUCCESS


@unique
class ContractRejectionReason(StrEnum):
    """Machine-readable reason a candidate contract was not selected.

    Every rejection carries one, so "why was the 180 call not chosen on 12
    August" is answerable from the stored record without re-deriving anything.
    """

    DTE_OUT_OF_RANGE = "DTE_OUT_OF_RANGE"
    WRONG_RIGHT = "WRONG_RIGHT"
    WRONG_UNDERLYING = "WRONG_UNDERLYING"
    WRONG_EXPIRATION = "WRONG_EXPIRATION"
    MISSING_CONTRACT_ID = "MISSING_CONTRACT_ID"
    MISSING_STRIKE = "MISSING_STRIKE"
    MISSING_EXPIRATION = "MISSING_EXPIRATION"
    MISSING_QUOTE = "MISSING_QUOTE"
    MISSING_DELTA = "MISSING_DELTA"
    MISSING_IMPLIED_VOLATILITY = "MISSING_IMPLIED_VOLATILITY"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_TRADING_CLASS = "INVALID_TRADING_CLASS"
    INVALID_MULTIPLIER = "INVALID_MULTIPLIER"
    #: Option-level liquidity below the strategy's floor, or not established at
    #: all. Underlying liquidity is never accepted as a substitute.
    LOW_OPTION_LIQUIDITY = "LOW_OPTION_LIQUIDITY"
    OPTION_LIQUIDITY_UNKNOWN = "OPTION_LIQUIDITY_UNKNOWN"
    OPTION_PRICE_OUT_OF_RANGE = "OPTION_PRICE_OUT_OF_RANGE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    IMPLIED_VOLATILITY_OUT_OF_RANGE = "IMPLIED_VOLATILITY_OUT_OF_RANGE"
    STRIKE_POLICY_NOT_SATISFIED = "STRIKE_POLICY_NOT_SATISFIED"
    #: The candidate is not part of the chain snapshot the selection used.
    NOT_IN_CHAIN_SNAPSHOT = "NOT_IN_CHAIN_SNAPSHOT"
    POINT_IN_TIME_VIOLATION = "POINT_IN_TIME_VIOLATION"
    QUOTE_NOT_RESEARCH_USABLE = "QUOTE_NOT_RESEARCH_USABLE"
    QUOTE_STALE = "QUOTE_STALE"
    EXPIRATION_NOT_A_TRADING_DAY = "EXPIRATION_NOT_A_TRADING_DAY"
    #: Valid in itself, but another candidate matched the policy better, or the
    #: legs could not be combined. Not a data fault.
    INCOMPATIBLE_LEG = "INCOMPATIBLE_LEG"
    NOT_SELECTED_BY_POLICY = "NOT_SELECTED_BY_POLICY"


# ---------------------------------------------------------------------------
# Allocation and risk (Milestone 7)
#
# The milestone that decides how much. Every enum here belongs to a
# deterministic module: no agent produces one, and no prompt can widen one.
# ---------------------------------------------------------------------------
@unique
class MaxLossBasis(StrEnum):
    """How a strategy's maximum loss is bounded.

    Declared by the strategy's own structure rather than assumed by the risk
    engine, because "max loss is the premium" is true of the four long-debit
    strategies shipped today and false of the first credit spread anyone adds.
    An engine that assumed it would size that spread as though it could only
    lose what it paid — which is exactly backwards.
    """

    #: Long-debit structures: the most that can be lost is what was paid for
    #: one unit, times the number of units. Nothing else is at risk.
    NET_DEBIT_PAID = "NET_DEBIT_PAID"
    #: The loss is bounded by something the engine cannot compute from the
    #: candidate alone — margin, an assigned short leg, an undefined tail. Fail
    #: closed: this is a rejection, not a number.
    NOT_DEFINED = "NOT_DEFINED"


@unique
class PriceSource(StrEnum):
    """Which figure a candidate's unit cost was taken from.

    Recorded, never assumed: an ask-based debit and a midpoint debit are
    different claims about what a structure costs, and a later evaluation must
    be able to tell which one authorised the capital.
    """

    #: Sum over legs of ask x multiplier x ratio — the honest cost of buying.
    ASK_DEBIT = "ASK_DEBIT"
    #: The same at the midpoint, only where both sides of every leg were quoted.
    MID_DEBIT = "MID_DEBIT"


@unique
class BudgetSource(StrEnum):
    """Where the campaign budget in force actually came from."""

    CONFIG = "CONFIG"
    #: ``CAMPAIGN_BUDGET_EUR`` in the environment. Recorded so a stored decision
    #: never implies the committed configuration authorised the figure.
    ENVIRONMENT = "ENVIRONMENT"


@unique
class RiskLimitScope(StrEnum):
    """Which layer of the hierarchy owns a limit.

    Global limits bound campaign limits, which bound strategy limits. A child
    may narrow a parent and may never widen one; configuration loading refuses
    rather than clamping, because a clamped limit is a limit nobody can see.
    """

    GLOBAL = "GLOBAL"
    CAMPAIGN = "CAMPAIGN"
    STRATEGY = "STRATEGY"
    #: Bounds on one position and on the quantity within it — the innermost
    #: layer, and the only one derived from the candidate rather than declared
    #: in configuration.
    POSITION = "POSITION"


@unique
class RiskCheckOutcome(StrEnum):
    """Result of one individual risk check.

    ``NOT_EVALUATED`` is deliberately distinct from ``PASS``. A limit that could
    not be checked — because the input it needs is not tracked yet — has not
    been satisfied, and recording it as passed would be the same lie as reading
    a missing measurement as zero.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


@unique
class AllocationOutcome(StrEnum):
    """What happened to one candidate at the allocation stage.

    ``NO_TRADE`` is a first-class outcome and the expected one whenever the
    campaign has run out of room: a valid strategy and a valid contract are not
    an entitlement to capital.
    """

    APPROVED = "APPROVED"
    #: A hard limit refused it. The reason codes say which.
    REJECTED = "REJECTED"
    #: Permitted in principle, but no whole contract fits what is left.
    NO_TRADE = "NO_TRADE"
    #: This exact opportunity already holds an approved reservation. Running
    #: the stage twice must not reserve the capital twice.
    ALREADY_ALLOCATED = "ALREADY_ALLOCATED"

    @property
    def commits_capital(self) -> bool:
        return self is AllocationOutcome.APPROVED


@unique
class AllocationRunStatus(StrEnum):
    """Outcome of a whole allocation run.

    A run that authorised nothing is not a failure — it is the usual answer
    when the budget is committed. The states below distinguish *declined* from
    *could not look*, which are different facts about the world.
    """

    SUCCESS = "SUCCESS"
    #: Ran to completion and authorised no capital. A considered refusal.
    NO_ALLOCATION = "NO_ALLOCATION"
    #: The contract stage produced no purchase candidate to consider.
    NO_CANDIDATES = "NO_CANDIDATES"
    #: No contract-selection run exists to allocate against.
    NO_CONTRACT_RUN = "NO_CONTRACT_RUN"
    #: No account snapshot was available, or the newest one is too old. The
    #: campaign envelope alone is not enough to authorise capital.
    ACCOUNT_SNAPSHOT_UNAVAILABLE = "ACCOUNT_SNAPSHOT_UNAVAILABLE"
    #: A record that was not knowable at the decision instant reached the run.
    POINT_IN_TIME_ERROR = "POINT_IN_TIME_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"

    @property
    def produced_authorizations(self) -> bool:
        """Whether a downstream stage may consume this run."""
        return self is AllocationRunStatus.SUCCESS


@unique
class AllocationPolicy(StrEnum):
    """How a finite campaign budget is distributed across many candidates.

    One member today, deliberately. The first version has to be transparent,
    deterministic, explainable and testable; a portfolio optimiser is none of
    those and is not what this milestone is for.
    """

    #: Order by deterministic priority, then fund each candidate in turn with
    #: whatever the limits still permit, stopping when the budget or the
    #: position count is exhausted.
    PRIORITY_FIRST_FIT = "PRIORITY_FIRST_FIT"


@unique
class AllocationReason(StrEnum):
    """Why a candidate reached its outcome, beyond the risk reason codes.

    These describe the *allocation* step rather than a limit breach: how the
    quantity came to be what it is. A rejection always carries a
    :class:`RiskReasonCode`; an approval carries one of these.
    """

    #: Every applicable limit permitted the quantity that was requested.
    FULL_ALLOCATION = "FULL_ALLOCATION"
    #: The quantity was reduced to fit the remaining campaign budget.
    LIMITED_BY_BUDGET = "LIMITED_BY_BUDGET"
    #: Reduced to fit the remaining risk budget.
    LIMITED_BY_RISK = "LIMITED_BY_RISK"
    #: Reduced by a per-trade allocation ceiling.
    LIMITED_BY_TRADE_CAP = "LIMITED_BY_TRADE_CAP"
    #: Reduced by an underlying or strategy concentration ceiling.
    LIMITED_BY_CONCENTRATION = "LIMITED_BY_CONCENTRATION"
    #: Reduced by the contract-count ceiling.
    LIMITED_BY_CONTRACT_CAP = "LIMITED_BY_CONTRACT_CAP"
    #: Reduced by what the broker says is actually available.
    LIMITED_BY_BUYING_POWER = "LIMITED_BY_BUYING_POWER"


# ---------------------------------------------------------------------------
# Milestone 8 — execution
#
# The vocabulary of the first stage allowed to submit an order. Two
# distinctions are load-bearing here and both have tests:
#
# * a broker *acknowledgement* is not a *fill*. ``SUBMITTED`` says IBKR
#   accepted the order; only an execution report may produce ``FILLED`` or
#   ``PARTIALLY_FILLED``;
# * "we did not send it" and "we do not know whether we sent it" are different
#   facts. ``FAILED`` is the first, ``UNKNOWN`` the second, and only the first
#   is safe to act on.
# ---------------------------------------------------------------------------
@unique
class ExecutionIntent(StrEnum):
    """What one submission is *for*: establishing a position, or ending one.

    Added by Milestone 10 and defaulted to ``OPEN``, so every execution written
    before it keeps its meaning unchanged. It is not decoration: three ledgers
    read an execution record and two of them must treat the two intents
    differently.

    ``OPEN``
        Milestone 7 authorised capital and Milestone 8 spent it. This is the
        execution a reservation is resolved against, and the one that
        establishes a logical strategy position.
    ``CLOSE``
        Milestone 10 decided an existing position should end. It commits no new
        capital, carries no new maximum loss, authorises no new structure, and
        is deliberately excluded from reservation accounting — a reservation
        answers "how much of the campaign is committed", and returning the
        proceeds of a sale to a campaign budget requires realised profit and
        loss, which is Milestone 11's. Its *fills* still net into the expected
        position ledger, which is precisely how a position becomes closed.
    """

    OPEN = "OPEN"
    CLOSE = "CLOSE"

    @property
    def establishes_position(self) -> bool:
        return self is ExecutionIntent.OPEN


@unique
class ExecutionState(StrEnum):
    """Lifecycle of one execution request.

    Distinct from :class:`OrderStatus`, which mirrors what the broker says
    about an order. This is what *we* know about a submission attempt, and it
    has states a broker has no opinion about: an order we never sent, and an
    order we may or may not have sent.

    The legal transitions live in
    :mod:`trading_system.execution.state_machine`, not here.
    """

    #: The request exists. Nothing has been validated and nothing sent.
    CREATED = "CREATED"
    #: Every deterministic precondition passed. Still nothing sent.
    VALIDATED = "VALIDATED"
    #: Recorded immediately *before* the broker call, so a process that dies
    #: mid-submission leaves evidence that a submission may be in flight.
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    #: The broker acknowledged the order. This is **not** a fill.
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    #: A cancellation was requested and the broker has not confirmed it. A
    #: cancel can lose the race with a fill, so this is not a terminal state.
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    #: The submission's outcome is genuinely unknown — typically a client
    #: timeout after the order may already have reached the broker. Never
    #: treated as "safe to retry"; it is resolved by looking at the broker,
    #: never by sending a second order.
    UNKNOWN = "UNKNOWN"
    #: The attempt provably did not reach the broker, or was refused before
    #: any submission was made.
    FAILED = "FAILED"


@unique
class ExecutionEventType(StrEnum):
    """One observation appended to an execution's history.

    History is append-only: a later fill never edits an earlier record, it adds
    an event. The current state is derived from the events, so "what did we
    know, and when" survives however the record is later summarised.
    """

    EXECUTION_CREATED = "EXECUTION_CREATED"
    EXECUTION_VALIDATED = "EXECUTION_VALIDATED"
    EXECUTION_SUBMISSION_PENDING = "EXECUTION_SUBMISSION_PENDING"
    EXECUTION_SUBMITTED = "EXECUTION_SUBMITTED"
    EXECUTION_PARTIAL_FILL = "EXECUTION_PARTIAL_FILL"
    EXECUTION_FILLED = "EXECUTION_FILLED"
    EXECUTION_CANCEL_REQUESTED = "EXECUTION_CANCEL_REQUESTED"
    EXECUTION_CANCELLED = "EXECUTION_CANCELLED"
    EXECUTION_REJECTED = "EXECUTION_REJECTED"
    EXECUTION_EXPIRED = "EXECUTION_EXPIRED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    #: The broker's answer is unknown. Recorded rather than assumed either way.
    EXECUTION_STATE_UNKNOWN = "EXECUTION_STATE_UNKNOWN"
    #: A broker state was observed without changing our state — a poll that
    #: confirmed what we already recorded. Kept so an audit shows we looked.
    EXECUTION_OBSERVED = "EXECUTION_OBSERVED"


@unique
class ExecutionReasonCode(StrEnum):
    """Machine-readable reason for an execution outcome.

    Deliberately separate from :class:`RiskReasonCode`: that vocabulary answers
    *may we trade this?* and is owned by Milestone 7. This one answers *what
    happened when we tried to send it?* — a question the risk engine has no
    opinion about. Codes that look similar across the two mean different
    things: ``CURRENCY_MISMATCH`` there refused to size a position, here it
    refuses to place an order for one that was somehow sized anyway.
    """

    #: The submission was accepted by the broker.
    OK = "OK"

    # --- authorisation -----------------------------------------------------
    #: The allocation's outcome is not APPROVED. REJECTED, NO_TRADE and
    #: ALREADY_ALLOCATED are all inexecutable, and none of them is a near miss.
    ALLOCATION_NOT_APPROVED = "ALLOCATION_NOT_APPROVED"
    #: No deliberate execution authorisation accompanied the request. An
    #: allocation id alone never means "send it".
    EXECUTION_NOT_AUTHORIZED = "EXECUTION_NOT_AUTHORIZED"
    #: Execution submission is switched off in configuration.
    EXECUTION_DISABLED = "EXECUTION_DISABLED"
    #: The allocation was produced by a dry run and is diagnostic, not an
    #: authorisation.
    ALLOCATION_IS_DRY_RUN = "ALLOCATION_IS_DRY_RUN"

    # --- identity and idempotency ------------------------------------------
    #: This exact execution identity already reached, or may have reached, the
    #: broker. Never a reason to send a second order.
    ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
    #: We sent something and never learned whether it arrived. Fail closed:
    #: resolve by looking at the broker, never by retrying.
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"

    # --- broker ------------------------------------------------------------
    BROKER_TIMEOUT = "BROKER_TIMEOUT"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    BROKER_REJECTED = "BROKER_REJECTED"
    #: The broker answered, but with a state this system does not recognise.
    UNKNOWN_BROKER_STATE = "UNKNOWN_BROKER_STATE"
    #: The broker acknowledged an order without an identifier, so it cannot be
    #: tracked or cancelled. Ambiguous, and treated as such.
    BROKER_ORDER_ID_MISSING = "BROKER_ORDER_ID_MISSING"
    #: The connected session is not demonstrably the expected paper account.
    PAPER_ACCOUNT_MISMATCH = "PAPER_ACCOUNT_MISMATCH"
    #: A read-only broker cannot submit. Structural, not incidental.
    BROKER_READ_ONLY = "BROKER_READ_ONLY"

    # --- the contract ------------------------------------------------------
    #: A leg lacks the broker contract id chosen upstream. Never re-derived
    #: from symbol, strike and expiration: an invented contract id is an order
    #: for something nobody selected.
    CONTRACT_ID_MISSING = "CONTRACT_ID_MISSING"
    #: The contract identity is structurally incomplete or self-contradictory.
    CONTRACT_INVALID = "CONTRACT_INVALID"
    #: No multiplier on the selected contract. Never assumed to be 100.
    MULTIPLIER_MISSING = "MULTIPLIER_MISSING"

    # --- the order ---------------------------------------------------------
    INVALID_QUANTITY = "INVALID_QUANTITY"
    INVALID_PRICE = "INVALID_PRICE"
    #: No usable reference price on the authorisation, so no limit price can be
    #: derived from one. Never conjured from a market we did not read.
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    ORDER_BUILD_FAILED = "ORDER_BUILD_FAILED"
    ORDER_TYPE_NOT_PERMITTED = "ORDER_TYPE_NOT_PERMITTED"
    #: A multi-leg structure the execution layer cannot represent as one order.
    #: Refused rather than approximated with independent single-leg orders.
    MULTI_LEG_UNSUPPORTED = "MULTI_LEG_UNSUPPORTED"
    #: The structure implies a short or uncovered leg, which the shipped
    #: strategy vocabulary does not authorise.
    SHORT_LEG_NOT_SUPPORTED = "SHORT_LEG_NOT_SUPPORTED"

    # --- time and price validity -------------------------------------------
    #: The authorisation is older than the configured execution window. Not
    #: silently extended; a changed trade needs a new authorisation.
    EXECUTION_WINDOW_EXPIRED = "EXECUTION_WINDOW_EXPIRED"
    #: The price the authorisation rests on is older than policy permits.
    PRICE_REFERENCE_STALE = "PRICE_REFERENCE_STALE"
    #: An observed price differs from the authorisation's reference by more
    #: than policy permits. Execution does not chase the market.
    PRICE_DRIFT = "PRICE_DRIFT"
    #: A timestamp that could not have been known at the execution instant
    #: reached the engine. A correctness bug, never a market outcome.
    POINT_IN_TIME_ERROR = "POINT_IN_TIME_ERROR"
    MARKET_CLOSED = "MARKET_CLOSED"

    # --- money -------------------------------------------------------------
    #: The contract is not quoted in the campaign currency and no deterministic
    #: FX mechanism exists. Milestone 7's refusal, preserved rather than undone.
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"

    # --- mode --------------------------------------------------------------
    PAPER_MODE_REQUIRED = "PAPER_MODE_REQUIRED"
    LIVE_GUARD_FAILED = "LIVE_GUARD_FAILED"
    TRADING_MODE_NOT_PERMITTED = "TRADING_MODE_NOT_PERMITTED"

    # --- provenance and configuration --------------------------------------
    #: An upstream artifact the purchase card is built from could not be read.
    #: "We could not look" is deliberately distinct from "we declined".
    PROVENANCE_UNAVAILABLE = "PROVENANCE_UNAVAILABLE"
    EXECUTION_CONFIGURATION_ERROR = "EXECUTION_CONFIGURATION_ERROR"

    # --- cancellation ------------------------------------------------------
    CANCEL_FAILED = "CANCEL_FAILED"
    #: There is nothing live to cancel.
    NOT_CANCELLABLE = "NOT_CANCELLABLE"

    #: The request was built and validated but deliberately not sent.
    DRY_RUN = "DRY_RUN"


@unique
class ExecutionRunStatus(StrEnum):
    """Outcome of one ``execution run`` invocation."""

    #: Every requested authorisation was submitted and acknowledged.
    SUCCESS = "SUCCESS"
    #: Some submitted, some refused. Each record says which and why.
    PARTIAL = "PARTIAL"
    #: Built and validated, nothing sent. The only status a ``--dry-run``
    #: can produce.
    DRY_RUN = "DRY_RUN"
    #: No approved authorisation was found to execute. The ordinary answer
    #: when a campaign has nothing outstanding, not a failure.
    NOTHING_TO_EXECUTE = "NOTHING_TO_EXECUTE"
    #: No allocation run exists to execute against.
    NO_ALLOCATION_RUN = "NO_ALLOCATION_RUN"
    #: Nothing was submitted because every candidate was refused.
    NOTHING_SUBMITTED = "NOTHING_SUBMITTED"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    EXECUTION_DISABLED = "EXECUTION_DISABLED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


# ---------------------------------------------------------------------------
# Positions, reservations and reconciliation (Milestone 9)
#
# Three vocabularies, deliberately separate, because they answer three
# different questions:
#
#   BrokerReadStatus     did we manage to look, and what did we see?
#   ReservationState     what has become of the capital we committed?
#   ReconciliationFinding what disagrees between our records and the broker?
#
# The distinction that governs all three is the Milestone 8 invariant carried
# forward: *not knowing is not the same as knowing nothing happened*. A failed
# broker read is not an empty portfolio, an unresolved submission is not a
# released reservation, and a missing position is not a position that never
# existed.
# ---------------------------------------------------------------------------
@unique
class BrokerReadStatus(StrEnum):
    """What happened when broker state was read.

    ``EMPTY`` and ``UNAVAILABLE`` are the two members this enum exists for. A
    broker that answered with no positions is a fact about the account; a
    broker that did not answer is a fact about the connection. Reconciling
    against the second as though it were the first would report every position
    the system believes in as missing, and every real broker position as gone.
    """

    #: The broker answered and the answer had content.
    OK = "OK"
    #: The broker answered, and the answer was genuinely empty. Valid data.
    EMPTY = "EMPTY"
    #: The broker could not be reached or refused. **Not** zero positions.
    UNAVAILABLE = "UNAVAILABLE"
    #: The request was not answered inside the configured timeout. Also not
    #: zero positions — the account is simply unknown right now.
    TIMEOUT = "TIMEOUT"
    #: The broker answered with something this system could not normalise.
    MALFORMED = "MALFORMED"
    #: The read was not attempted, typically because policy did not ask for it.
    NOT_REQUESTED = "NOT_REQUESTED"

    @property
    def usable(self) -> bool:
        """Whether the answer may be reconciled against."""
        return self in (BrokerReadStatus.OK, BrokerReadStatus.EMPTY)


@unique
class AcquisitionProvenance(StrEnum):
    """How a position came to be held, as far as this system can tell.

    A broker position with no internal execution history is ``UNKNOWN`` and
    stays ``UNKNOWN``. Inventing an allocation, an execution, a strategy or a
    purchase date for a pre-existing holding would fabricate the audit trail
    the whole system exists to keep honest.
    """

    #: Traceable to an execution this system recorded.
    SYSTEM_EXECUTION = "SYSTEM_EXECUTION"
    #: Held at the broker with no execution of ours behind it. Never adopted
    #: automatically into the internal ledger.
    UNKNOWN = "UNKNOWN"


@unique
class StructureStatus(StrEnum):
    """Whether a multi-leg structure is actually present at the broker.

    ``PARTIAL`` is the member that matters. A straddle with its call filled and
    its put missing is neither a straddle nor an absence of one — it is a naked
    long call against limits nobody checked for one, and it must be reportable
    as exactly that.
    """

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    #: Broker data was unusable, so no claim is made in either direction.
    UNKNOWN = "UNKNOWN"


@unique
class ReservationState(StrEnum):
    """What has become of capital this campaign committed.

    ``RESERVED`` is not ``INVESTED``: Milestone 7 authorises capital and only a
    confirmed fill spends it. ``UNKNOWN`` is not ``RELEASED``: an execution
    whose outcome was never learned may be a live order right now, and freeing
    its capital is how one intention becomes two positions.
    """

    #: Capital is committed to an authorisation and nothing has consumed it.
    RESERVED = "RESERVED"
    #: Some of it was spent by confirmed fills; the rest is still committed.
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    #: Spent. The position exists, and the capital is in it.
    CONSUMED = "CONSUMED"
    #: Provably not spent and returned to the campaign.
    RELEASED = "RELEASED"
    #: The execution behind it is unresolved. The capital stays locked.
    UNKNOWN = "UNKNOWN"


@unique
class ReservationEventType(StrEnum):
    """One appended observation about a reservation.

    Append-only, exactly like an execution's history: the current state is a
    fold of these, so "when did this capital stop being available, and why" is
    answerable after the fact.
    """

    RESERVATION_CREATED = "RESERVATION_CREATED"
    RESERVATION_INCREASED = "RESERVATION_INCREASED"
    RESERVATION_PARTIALLY_CONSUMED = "RESERVATION_PARTIALLY_CONSUMED"
    RESERVATION_CONSUMED = "RESERVATION_CONSUMED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    #: The execution is UNKNOWN, so the reservation was deliberately kept.
    #: Recorded rather than implied: a decision not to release is a decision.
    RESERVATION_RETAINED_UNKNOWN = "RESERVATION_RETAINED_UNKNOWN"
    #: A broker correction moved consumed capital past what was authorised.
    RESERVATION_CORRECTED = "RESERVATION_CORRECTED"
    #: Observed without change. Kept so an audit shows we looked.
    RESERVATION_OBSERVED = "RESERVATION_OBSERVED"


@unique
class ReservationReasonCode(StrEnum):
    """Why a reservation is in the state it is in."""

    #: Authorised by Milestone 7 and not yet acted on.
    AUTHORIZED = "AUTHORIZED"
    #: No execution attempt exists for this authorisation at all.
    NOT_EXECUTED = "NOT_EXECUTED"
    #: An execution attempt provably never left the process.
    EXECUTION_FAILED_BEFORE_SUBMISSION = "EXECUTION_FAILED_BEFORE_SUBMISSION"
    BROKER_REJECTED = "BROKER_REJECTED"
    CANCELLED_WITHOUT_FILL = "CANCELLED_WITHOUT_FILL"
    CANCELLED_AFTER_PARTIAL_FILL = "CANCELLED_AFTER_PARTIAL_FILL"
    EXPIRED_WITHOUT_FILL = "EXPIRED_WITHOUT_FILL"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    #: The order is working at the broker. Capital stays committed.
    ORDER_WORKING = "ORDER_WORKING"
    #: The submission's outcome is unknown. Capital stays committed — this is
    #: the single most important reason code in the vocabulary.
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
    #: A release was requested for an execution that may still be live.
    RELEASE_REFUSED_UNKNOWN = "RELEASE_REFUSED_UNKNOWN"
    #: Broker state could not be read, so nothing was resolved either way.
    BROKER_DATA_UNAVAILABLE = "BROKER_DATA_UNAVAILABLE"
    #: The fill economics are quoted in a currency the campaign does not hold,
    #: and no deterministic FX mechanism exists. Milestone 7's refusal,
    #: preserved rather than undone.
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    #: Broker evidence puts consumed capital above what was authorised.
    BROKER_CORRECTION = "BROKER_CORRECTION"


@unique
class ReconciliationFindingType(StrEnum):
    """One specific thing reconciliation found.

    Deliberately finer than :class:`DiscrepancyType`, which is the Milestone 1
    boundary vocabulary this projects onto. "Positions differ" is not an
    actionable statement; "the broker holds four option positions no internal
    execution accounts for" is.
    """

    # --- agreement ---------------------------------------------------------
    #: Internal expectation and broker reality agree, exactly.
    POSITION_MATCH = "POSITION_MATCH"
    ORDER_MATCH = "ORDER_MATCH"
    FILL_MATCH = "FILL_MATCH"
    RESERVATION_MATCH = "RESERVATION_MATCH"

    # --- positions ---------------------------------------------------------
    #: We expect a position the broker does not report. Never replaced by a
    #: compensating order: this is a finding, not an instruction.
    EXPECTED_POSITION_MISSING = "EXPECTED_POSITION_MISSING"
    #: The broker holds something no internal execution accounts for. Real,
    #: and never sold, adopted or assigned to a campaign automatically.
    ORPHAN_BROKER_POSITION = "ORPHAN_BROKER_POSITION"
    POSITION_QUANTITY_MISMATCH = "POSITION_QUANTITY_MISMATCH"
    #: The same instrument appears under a different broker contract id.
    POSITION_CONTRACT_MISMATCH = "POSITION_CONTRACT_MISMATCH"
    #: A multi-leg structure is present in part. Not complete, not absent.
    PARTIAL_STRUCTURE = "PARTIAL_STRUCTURE"

    # --- orders ------------------------------------------------------------
    ORDER_STATE_MISMATCH = "ORDER_STATE_MISMATCH"
    #: An order at the broker that no internal execution record names.
    ORPHAN_BROKER_ORDER = "ORPHAN_BROKER_ORDER"
    #: We believe an order is working and the broker does not report it.
    EXPECTED_ORDER_MISSING = "EXPECTED_ORDER_MISSING"
    #: A FAILED execution has an order at the broker. FAILED means the attempt
    #: provably never left the process, so this is a serious consistency
    #: violation and never a reason to relabel the execution.
    FAILED_EXECUTION_HAS_BROKER_ORDER = "FAILED_EXECUTION_HAS_BROKER_ORDER"

    # --- executions --------------------------------------------------------
    #: Broker evidence settled an ambiguous submission.
    UNKNOWN_EXECUTION_RESOLVED = "UNKNOWN_EXECUTION_RESOLVED"
    #: It did not, and the execution stays UNKNOWN. Capital stays committed.
    UNKNOWN_EXECUTION_UNRESOLVED = "UNKNOWN_EXECUTION_UNRESOLVED"

    # --- fills -------------------------------------------------------------
    FILL_MISMATCH = "FILL_MISMATCH"
    #: A fill at the broker that no internal execution accounts for.
    ORPHAN_BROKER_FILL = "ORPHAN_BROKER_FILL"

    # --- reservations ------------------------------------------------------
    RESERVATION_MISMATCH = "RESERVATION_MISMATCH"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    RESERVATION_CONSUMED = "RESERVATION_CONSUMED"
    RESERVATION_RETAINED_UNKNOWN = "RESERVATION_RETAINED_UNKNOWN"

    # --- data availability -------------------------------------------------
    #: Broker state could not be read. Distinct from "the broker holds
    #: nothing", which is :data:`BROKER_RETURNED_EMPTY`.
    BROKER_DATA_UNAVAILABLE = "BROKER_DATA_UNAVAILABLE"
    #: The broker answered with nothing, and that is a valid answer.
    BROKER_RETURNED_EMPTY = "BROKER_RETURNED_EMPTY"
    #: An internal ledger could not be read, so nothing was compared.
    INTERNAL_DATA_UNAVAILABLE = "INTERNAL_DATA_UNAVAILABLE"
    #: An amount is denominated in a currency the campaign does not hold and
    #: no FX mechanism exists. Reported, never converted at an invented rate.
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


@unique
class ReconciliationSeverity(StrEnum):
    """How much attention a finding needs.

    Every finding type's severity comes from ``config/reconciliation.yaml``,
    not from code: how alarming a missing position is depends on how the
    account is operated, and a severity policy invented in a module would be a
    financial judgement nobody could see or change.
    """

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@unique
class ReconciliationRunStatus(StrEnum):
    """Outcome of one reconciliation run.

    ``MATCH`` requires that everything relevant was actually compared. A run
    that could not read the broker is ``BROKER_DATA_UNAVAILABLE`` and never
    ``MATCH``: agreeing with an empty set is not agreement.
    """

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    BROKER_DATA_UNAVAILABLE = "BROKER_DATA_UNAVAILABLE"
    INTERNAL_DATA_UNAVAILABLE = "INTERNAL_DATA_UNAVAILABLE"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


@unique
class ReconciliationEventType(StrEnum):
    """One appended step in a reconciliation's own history."""

    RECONCILIATION_STARTED = "RECONCILIATION_STARTED"
    BROKER_SNAPSHOT_CAPTURED = "BROKER_SNAPSHOT_CAPTURED"
    BROKER_READ_FAILED = "BROKER_READ_FAILED"
    INTERNAL_LEDGER_READ = "INTERNAL_LEDGER_READ"
    POSITION_MATCH = "POSITION_MATCH"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    ORDER_MISMATCH = "ORDER_MISMATCH"
    FILL_MISMATCH = "FILL_MISMATCH"
    EXECUTION_RESOLVED = "EXECUTION_RESOLVED"
    RESERVATION_CONSUMED = "RESERVATION_CONSUMED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    RESERVATION_RETAINED = "RESERVATION_RETAINED"
    #: The same broker content was reconciled again. Recorded as an
    #: observation rather than as a second, differently-named result.
    SNAPSHOT_REOBSERVED = "SNAPSHOT_REOBSERVED"
    RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"


# ---------------------------------------------------------------------------
# Exit management and position lifecycle (Milestone 10)
#
# The milestone that asks one question about a position that already exists:
# *should this be closed?* Three vocabularies, and the distinctions between
# them are what keep the answer honest:
#
#   PositionLifecycleState  what has become of this position, and of our
#                           attempt to end it;
#   ExitDecisionType        the closed answer: WAIT, EXIT or BLOCK;
#   ExitReasonCode          which policy said so, and on what evidence.
#
# Deliberately separate from :class:`ExitAction` and :class:`ExitReason`, which
# are the *narrow Milestone 1 boundary* (HOLD/SELL and six coarse reasons).
# These are the wide audit vocabulary and they project onto that boundary,
# exactly as research, strategy, execution and reconciliation project onto
# theirs. Merging them would either coarsen what an operator can act on or
# rewrite a completed milestone's contract.
#
# The rule that governs all three, carried forward from Milestone 8: *not
# knowing is not the same as knowing nothing happened*. An exit whose outcome
# was never learned is never re-sent, and no elapsed time turns it into a
# failure.
# ---------------------------------------------------------------------------
@unique
class PositionLifecycleState(StrEnum):
    """What has become of one already-open strategy position.

    Distinct from :class:`PositionState`, which spans the *whole* workflow from
    discovery to closure and is the Milestone 1 boundary vocabulary. This one
    starts where a position already exists and describes only what exit
    management knows about it — including two states the Milestone 1 vocabulary
    has no member for, and could not honestly be given one:

    ``EXIT_UNKNOWN``
        An exit was sent and its outcome was never learned. It is not
        ``CLOSING`` (that claims an order is working), not ``OPEN`` (that
        claims none is), and not ``CLOSED``.
    ``BLOCKED``
        Something is inconsistent or unreadable and no exit decision may be
        acted on until a person or a reconciliation resolves it.

    The legal transitions live in :mod:`trading_system.exit.lifecycle`.
    """

    #: Confirmed fills established it and exit management has not looked yet.
    OPEN = "OPEN"
    #: Under evaluation, with no exit policy currently triggered.
    MONITORING = "MONITORING"
    #: A trailing stop is armed or active. Still monitoring, but the level
    #: moves — kept distinct so "why did this exit" is answerable afterwards.
    TRAILING_ACTIVE = "TRAILING_ACTIVE"
    #: A policy triggered. Nothing has been sent.
    EXIT_REQUIRED = "EXIT_REQUIRED"
    #: An exit order reached, or may have reached, the broker.
    EXIT_SUBMITTED = "EXIT_SUBMITTED"
    #: The exit submission's outcome is unknown. Never re-sent.
    EXIT_UNKNOWN = "EXIT_UNKNOWN"
    #: Broker reality confirms the position is gone. Terminal.
    CLOSED = "CLOSED"
    #: Evaluation refused to proceed. Leaves only on explicit resolution.
    BLOCKED = "BLOCKED"


@unique
class PositionLifecycleEventType(StrEnum):
    """One appended observation about a position's lifecycle.

    Append-only, exactly like an execution's or a reservation's history: the
    current state is a fold of these, so "when did this position start
    trailing, from what price, and what observation triggered the exit" stays
    answerable after the position is closed and the configuration has moved on.
    """

    LIFECYCLE_OPENED = "LIFECYCLE_OPENED"
    LIFECYCLE_MONITORED = "LIFECYCLE_MONITORED"
    TRAILING_ARMED = "TRAILING_ARMED"
    TRAILING_ACTIVATED = "TRAILING_ACTIVATED"
    TRAILING_LEVEL_RAISED = "TRAILING_LEVEL_RAISED"
    TRAILING_TRIGGERED = "TRAILING_TRIGGERED"
    EXIT_REQUIRED = "EXIT_REQUIRED"
    EXIT_SUBMITTED = "EXIT_SUBMITTED"
    EXIT_STATE_UNKNOWN = "EXIT_STATE_UNKNOWN"
    EXIT_CONFIRMED_CLOSED = "EXIT_CONFIRMED_CLOSED"
    LIFECYCLE_BLOCKED = "LIFECYCLE_BLOCKED"
    #: A block was resolved by observation or by an operator. Recorded rather
    #: than implied: leaving a blocked state is a decision.
    LIFECYCLE_UNBLOCKED = "LIFECYCLE_UNBLOCKED"
    #: Looked at, nothing changed. Kept so an audit shows the monitor ran.
    LIFECYCLE_OBSERVED = "LIFECYCLE_OBSERVED"


@unique
class ExitDecisionType(StrEnum):
    """The closed vocabulary of exit verdicts.

    Three members, and the third is the one that matters. ``BLOCK`` is not a
    kind of ``WAIT``: waiting is a judgement about a market made from data we
    actually have, and blocking says the data, the records or the broker are
    not in a state where any judgement may be acted on. Collapsing them would
    let "we could not read the quote" look exactly like "the thesis still
    holds".
    """

    #: Keep the position. The ordinary answer.
    WAIT = "WAIT"
    #: Close the whole structure. Always names a policy and a reason.
    EXIT = "EXIT"
    #: Refuse to act. Never resolved by retrying the evaluation.
    BLOCK = "BLOCK"

    @property
    def requests_order(self) -> bool:
        """Whether this verdict may cause an exit order to be built."""
        return self is ExitDecisionType.EXIT


@unique
class ExitPolicyKind(StrEnum):
    """Which deterministic policy produced an outcome.

    The precedence between them is :data:`EXIT_POLICY_PRECEDENCE`, and it is
    explicit rather than emergent: safety conditions are evaluated before
    profit-taking, so a position that is both in profit and structurally
    unreadable blocks rather than sells.
    """

    #: Does this position exist, and does our lifecycle agree with M9?
    POSITION_CONSISTENCY = "POSITION_CONSISTENCY"
    #: Was the broker actually read, and is what it says usable?
    BROKER_OBSERVATION = "BROKER_OBSERVATION"
    #: Is an exit already submitted, working, or unresolved?
    EXECUTION_STATE = "EXECUTION_STATE"
    #: Do we have the contract metadata an exit order would need?
    CONTRACT_VALIDITY = "CONTRACT_VALIDITY"
    #: How long is left, on the exchange's own calendar?
    EXPIRATION = "EXPIRATION"
    #: Is the price this evaluation would rest on usable at all?
    DATA_QUALITY = "DATA_QUALITY"
    #: Has the position lost more than policy permits?
    MAX_LOSS = "MAX_LOSS"
    #: Has the research thesis been invalidated?
    THESIS = "THESIS"
    #: Has it made enough?
    TAKE_PROFIT = "TAKE_PROFIT"
    #: Has it given back enough of its peak?
    TRAILING_STOP = "TRAILING_STOP"


@unique
class ExitReasonCode(StrEnum):
    """Why an exit evaluation reached the verdict it did.

    A closed vocabulary, and deliberately fine-grained: an operator asking "why
    is this position still open" is owed the specific policy and the specific
    missing fact, not ``ERROR``. Every member belongs to exactly one verdict —
    :data:`EXIT_WAIT_REASONS`, :data:`EXIT_TRIGGER_REASONS` and
    :data:`EXIT_BLOCK_REASONS` partition this enum, and a test asserts it.
    """

    # --- WAIT: nothing triggered, and that is a real answer ----------------
    #: Every applicable policy was evaluated and none triggered.
    POLICY_SATISFIED = "POLICY_SATISFIED"
    #: Position is in profit but below the trailing activation threshold.
    TRAILING_NOT_ACTIVE = "TRAILING_NOT_ACTIVE"
    #: The activation threshold was crossed; the trail is set and holding.
    TRAILING_ABOVE_STOP = "TRAILING_ABOVE_STOP"
    THESIS_INTACT = "THESIS_INTACT"
    #: Inside the horizon, above the warning threshold.
    EXPIRATION_NOT_REACHED = "EXPIRATION_NOT_REACHED"
    #: Below the warning threshold, above the force-exit threshold. Not a
    #: trigger — a statement that this position is now near its deadline.
    EXPIRATION_WARNING = "EXPIRATION_WARNING"
    TAKE_PROFIT_NOT_REACHED = "TAKE_PROFIT_NOT_REACHED"
    MAX_LOSS_NOT_REACHED = "MAX_LOSS_NOT_REACHED"
    #: An exit order is working at the broker. Waiting for it is the whole
    #: correct behaviour; sending another would close the position twice.
    EXIT_ALREADY_SUBMITTED = "EXIT_ALREADY_SUBMITTED"
    #: Broker reality says this position is gone. Terminal, and a no-op.
    POSITION_CLOSED = "POSITION_CLOSED"
    #: The policy could not be evaluated but does not block on its own.
    #: Distinct from a pass, for the same reason ``NOT_EVALUATED`` is distinct
    #: from ``PASS`` in the risk engine.
    NOT_EVALUATED = "NOT_EVALUATED"

    # --- EXIT: a policy triggered ------------------------------------------
    TRAILING_STOP_TRIGGERED = "TRAILING_STOP_TRIGGERED"
    EXPIRATION_FORCE_EXIT = "EXPIRATION_FORCE_EXIT"
    MAX_LOSS_REACHED = "MAX_LOSS_REACHED"
    TAKE_PROFIT_REACHED = "TAKE_PROFIT_REACHED"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"

    # --- BLOCK: no judgement may be acted on -------------------------------
    #: No position with this id is known to the ledger.
    POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
    #: The broker's view of this position could not be established.
    POSITION_STATE_UNKNOWN = "POSITION_STATE_UNKNOWN"
    #: The broker holds a different quantity than confirmed fills imply.
    POSITION_QUANTITY_MISMATCH = "POSITION_QUANTITY_MISMATCH"
    #: A multi-leg structure is present in part. The risk of what is held is
    #: not the risk that was authorised, and no exit policy was written for it.
    PARTIAL_STRUCTURE = "PARTIAL_STRUCTURE"
    #: Our lifecycle record and broker reality contradict each other.
    LIFECYCLE_INCONSISTENT = "LIFECYCLE_INCONSISTENT"
    #: The broker could not be read at all. Not an empty account.
    BROKER_DATA_UNAVAILABLE = "BROKER_DATA_UNAVAILABLE"
    #: A reconciliation must run, or its findings be resolved, first.
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    #: An exit was sent and never confirmed. The single most important block
    #: in this vocabulary: it may be a live order right now.
    EXIT_OUTCOME_UNKNOWN = "EXIT_OUTCOME_UNKNOWN"
    #: A *prior* exit attempt is unresolved, so this evaluation may not act.
    EXIT_ALREADY_UNKNOWN = "EXIT_ALREADY_UNKNOWN"
    #: No quote at all for a leg. Never replaced by a last price, a close, or
    #: the price we paid.
    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
    #: A quote exists and is older than the configured window.
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    #: The data layer judged the quote unusable. Never re-graded here.
    MARKET_DATA_QUALITY_FAILED = "MARKET_DATA_QUALITY_FAILED"
    #: The configured quote field is absent on an otherwise valid quote. No
    #: other field is substituted for it.
    QUOTE_FIELD_UNAVAILABLE = "QUOTE_FIELD_UNAVAILABLE"
    #: The contract terms an exit order needs are missing or contradictory.
    CONTRACT_METADATA_UNAVAILABLE = "CONTRACT_METADATA_UNAVAILABLE"
    #: No multiplier. Never assumed to be 100.
    MULTIPLIER_UNAVAILABLE = "MULTIPLIER_UNAVAILABLE"
    #: No expiration on a leg, so no DTE can be computed for it.
    EXPIRATION_DATA_UNAVAILABLE = "EXPIRATION_DATA_UNAVAILABLE"
    #: The expiration falls in a year the market calendar does not cover.
    EXPIRATION_CALENDAR_UNKNOWN = "EXPIRATION_CALENDAR_UNKNOWN"
    #: The stored trailing state cannot be replayed or contradicts itself.
    TRAILING_STATE_CORRUPTED = "TRAILING_STATE_CORRUPTED"
    #: The research report this position rests on could not be read.
    THESIS_DATA_UNAVAILABLE = "THESIS_DATA_UNAVAILABLE"
    #: The strategy's declared maximum-loss basis is unavailable or is
    #: ``NOT_DEFINED``. Milestone 7's refusal, preserved rather than estimated.
    RISK_BASIS_UNAVAILABLE = "RISK_BASIS_UNAVAILABLE"
    #: The strategy metadata this position was opened under is unavailable.
    STRATEGY_METADATA_UNAVAILABLE = "STRATEGY_METADATA_UNAVAILABLE"
    #: A record that was not knowable at the evaluation instant reached the
    #: engine. A correctness bug, never a market outcome.
    POINT_IN_TIME_ERROR = "POINT_IN_TIME_ERROR"
    EXIT_CONFIGURATION_ERROR = "EXIT_CONFIGURATION_ERROR"


@unique
class ExitQuoteField(StrEnum):
    """Which quoted price an exit is valued against, stated rather than assumed.

    A long option is closed by *selling*, so the honest exit value is the
    **bid**: it is the price a seller can actually get. ``MID`` is a fair-value
    estimate that nobody is obliged to trade at, and ``LAST`` is a print that
    may be hours old and on the other side of the spread. The shipped default
    is ``BID`` and the field is recorded on every valuation, so a stored
    decision says what it was measured against.

    There is deliberately no ``AUTO``: substituting one field for another when
    the configured one is missing is exactly the fabrication this milestone
    refuses. A missing bid is ``QUOTE_FIELD_UNAVAILABLE``.
    """

    BID = "BID"
    ASK = "ASK"
    MID = "MID"
    LAST = "LAST"


@unique
class ThesisConditionOutcome(StrEnum):
    """What a deterministic check of one invalidation condition concluded.

    ``NOT_EVALUATED`` is the expected answer for most conditions and is not a
    failure. Research states invalidation conditions in prose; a condition that
    cannot be checked against a structured fact is *labelled* as unevaluated
    rather than interpreted, because reading an arbitrary sentence as a trading
    signal is precisely what a deterministic exit engine must not do.
    """

    #: Checked, and the thesis survives it.
    HOLDS = "HOLDS"
    #: Checked against a structured fact, and violated.
    VIOLATED = "VIOLATED"
    #: Prose only. No deterministic check exists, and none is invented.
    NOT_EVALUATED = "NOT_EVALUATED"


@unique
class TrailingStopState(StrEnum):
    """The trailing stop's own state machine.

    Four states rather than a mutable price, because "the trailing level is
    2.10" answers none of the questions an operator asks after an exit: when it
    became active, what activated it, what the peak was, and which observation
    crossed it. Each transition is an event, and the level only ever moves in
    the favourable direction.
    """

    #: Below the activation threshold. No level exists.
    INACTIVE = "INACTIVE"
    #: The threshold was reached in this evaluation; the first level is set.
    ARMED = "ARMED"
    #: Established, with a level that has been carried across evaluations.
    ACTIVE = "ACTIVE"
    #: The observed price fell to or below the level. Terminal.
    TRIGGERED = "TRIGGERED"


@unique
class ExitRunStatus(StrEnum):
    """Outcome of one ``exit evaluate`` or ``positions monitor`` invocation.

    A run that decided to keep every position is ``SUCCESS`` — exit management
    that exits nothing is exit management working. ``NO_POSITIONS`` and
    ``BROKER_DATA_UNAVAILABLE`` are distinct because "there is nothing to
    manage" and "we could not look" are different facts about the account.
    """

    SUCCESS = "SUCCESS"
    #: Evaluated, and at least one position could not be judged.
    PARTIAL = "PARTIAL"
    #: There is no open position to evaluate. The ordinary answer.
    NO_POSITIONS = "NO_POSITIONS"
    #: Broker position state could not be read. Nothing was compared.
    BROKER_DATA_UNAVAILABLE = "BROKER_DATA_UNAVAILABLE"
    #: An internal ledger could not be read.
    INTERNAL_DATA_UNAVAILABLE = "INTERNAL_DATA_UNAVAILABLE"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


#: The deterministic order in which exit policies are evaluated.
#:
#: Encoded here rather than emerging from the order of ``if`` statements, so it
#: is one reviewable list, printable by ``exit validate`` and assertable by a
#: test. Read it as: *does this position exist and can we see it* before *how
#: long is left* before *has it lost too much* before *has it made enough*.
#: Safety precedes profit-taking at every step — a position that is both at its
#: take-profit and structurally unreadable blocks rather than sells, because
#: the second fact says the first was measured against something untrustworthy.
EXIT_POLICY_PRECEDENCE: tuple[ExitPolicyKind, ...] = (
    ExitPolicyKind.POSITION_CONSISTENCY,
    ExitPolicyKind.BROKER_OBSERVATION,
    ExitPolicyKind.EXECUTION_STATE,
    ExitPolicyKind.CONTRACT_VALIDITY,
    ExitPolicyKind.EXPIRATION,
    ExitPolicyKind.DATA_QUALITY,
    ExitPolicyKind.MAX_LOSS,
    ExitPolicyKind.THESIS,
    ExitPolicyKind.TAKE_PROFIT,
    ExitPolicyKind.TRAILING_STOP,
)

#: Reason codes that accompany a ``WAIT``.
EXIT_WAIT_REASONS: frozenset[ExitReasonCode] = frozenset(
    {
        ExitReasonCode.POLICY_SATISFIED,
        ExitReasonCode.TRAILING_NOT_ACTIVE,
        ExitReasonCode.TRAILING_ABOVE_STOP,
        ExitReasonCode.THESIS_INTACT,
        ExitReasonCode.EXPIRATION_NOT_REACHED,
        ExitReasonCode.EXPIRATION_WARNING,
        ExitReasonCode.TAKE_PROFIT_NOT_REACHED,
        ExitReasonCode.MAX_LOSS_NOT_REACHED,
        ExitReasonCode.EXIT_ALREADY_SUBMITTED,
        ExitReasonCode.POSITION_CLOSED,
        ExitReasonCode.NOT_EVALUATED,
    }
)

#: Reason codes that accompany an ``EXIT``. Every one names a policy that
#: actually triggered; there is no generic "exit because".
EXIT_TRIGGER_REASONS: frozenset[ExitReasonCode] = frozenset(
    {
        ExitReasonCode.TRAILING_STOP_TRIGGERED,
        ExitReasonCode.EXPIRATION_FORCE_EXIT,
        ExitReasonCode.MAX_LOSS_REACHED,
        ExitReasonCode.TAKE_PROFIT_REACHED,
        ExitReasonCode.THESIS_INVALIDATED,
    }
)

#: Reason codes that accompany a ``BLOCK``. Everything left over, and that is
#: checked: a member added to :class:`ExitReasonCode` without being classified
#: fails ``tests/exit/test_models.py`` rather than silently becoming a block.
EXIT_BLOCK_REASONS: frozenset[ExitReasonCode] = frozenset(
    set(ExitReasonCode) - EXIT_WAIT_REASONS - EXIT_TRIGGER_REASONS
)

#: Lifecycle states from which no further transition is possible.
#:
#: ``EXIT_UNKNOWN`` is deliberately **not** here, for the same reason
#: ``ExecutionState.UNKNOWN`` is not in :data:`TERMINAL_EXECUTION_STATES`: an
#: unresolved exit is a question, and a question that could never be answered
#: would leave a position nobody can account for.
TERMINAL_LIFECYCLE_STATES: frozenset[PositionLifecycleState] = frozenset(
    {PositionLifecycleState.CLOSED}
)

#: Lifecycle states in which no new exit order may be built, whatever a policy
#: concludes.
#:
#: The set idempotency is judged against. ``EXIT_SUBMITTED`` and
#: ``EXIT_UNKNOWN`` are both here because both mean *an exit order may be live*,
#: and re-submitting over either is how a position is closed twice — once at a
#: price that was decided on and once at whatever the market does next.
#: ``CLOSED`` is here because there is nothing left to sell.
#:
#: ``BLOCKED`` is deliberately **not** here, and the reason is a safety property
#: rather than a convenience. A block is *re-derived on every evaluation* from
#: the conditions that caused it; it is not a memory. Including it would mean a
#: position blocked once — because a research file was unreadable, say — could
#: never afterwards be force-exited at its expiration deadline, since the stale
#: block would suppress the current judgement. What must never be retried is a
#: *submission whose outcome is unknown*, and that is exactly what the two
#: states above express.
EXIT_SUBMISSION_BLOCKED_STATES: frozenset[PositionLifecycleState] = frozenset(
    {
        PositionLifecycleState.EXIT_SUBMITTED,
        PositionLifecycleState.EXIT_UNKNOWN,
        PositionLifecycleState.CLOSED,
    }
)


#: Finding types that describe agreement rather than disagreement.
#:
#: Kept as a set rather than as a naming convention because the run status is
#: derived from it: a run whose findings are all in here is a ``MATCH``.
AGREEMENT_FINDINGS: frozenset[ReconciliationFindingType] = frozenset(
    {
        ReconciliationFindingType.POSITION_MATCH,
        ReconciliationFindingType.ORDER_MATCH,
        ReconciliationFindingType.FILL_MATCH,
        ReconciliationFindingType.RESERVATION_MATCH,
        ReconciliationFindingType.BROKER_RETURNED_EMPTY,
        ReconciliationFindingType.UNKNOWN_EXECUTION_RESOLVED,
        ReconciliationFindingType.RESERVATION_RELEASED,
        ReconciliationFindingType.RESERVATION_CONSUMED,
    }
)

#: Reservation states in which the capital is still committed to the campaign.
#:
#: ``UNKNOWN`` is here deliberately, and it is the whole point of the set: an
#: execution whose outcome was never learned may be a live order, so its
#: capital is not available however much time has passed.
COMMITTED_RESERVATION_STATES: frozenset[ReservationState] = frozenset(
    {
        ReservationState.RESERVED,
        ReservationState.PARTIALLY_CONSUMED,
        ReservationState.CONSUMED,
        ReservationState.UNKNOWN,
    }
)


#: States from which no further transition is possible.
TERMINAL_STATES: frozenset[PositionState] = frozenset(
    {
        PositionState.CLOSED,
        PositionState.NO_TRADE,
        PositionState.REJECTED,
        PositionState.CANCELLED,
        PositionState.FAILED,
        PositionState.EXPIRED,
    }
)

#: Execution states from which no further transition is possible.
#:
#: ``UNKNOWN`` is deliberately **not** here: an unresolved submission is a
#: question, and a question that could never be answered would leave capital
#: committed to an order nobody can account for. It is resolved by observing
#: the broker.
TERMINAL_EXECUTION_STATES: frozenset[ExecutionState] = frozenset(
    {
        ExecutionState.FILLED,
        ExecutionState.CANCELLED,
        ExecutionState.REJECTED,
        ExecutionState.EXPIRED,
        ExecutionState.FAILED,
    }
)

#: Execution states in which an order may exist at the broker.
#:
#: This is the set idempotency is judged against, and it deliberately includes
#: ``SUBMISSION_PENDING`` and ``UNKNOWN``: both mean *an order may be in
#: flight*, and re-submitting over either is how a system places the same trade
#: twice. Absence of an acknowledgement is not evidence of absence of an order.
LIVE_EXECUTION_STATES: frozenset[ExecutionState] = frozenset(
    {
        ExecutionState.SUBMISSION_PENDING,
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.FILLED,
        ExecutionState.CANCEL_PENDING,
        ExecutionState.UNKNOWN,
    }
)
