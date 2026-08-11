"""Canonical enumerations for the trading system.

Every enum is a :class:`~enum.StrEnum` so that values serialise to plain JSON
strings and round-trip through the workflow-boundary schemas in ``schemas/``.

These values are part of the persisted trade record. Renaming a member is a
breaking change to historical snapshots — add a new member instead.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = [
    "BarInterval",
    "BrokerConnectionState",
    "CollectionOutcome",
    "ConfidenceLevel",
    "CorporateEventType",
    "DataGapStatus",
    "DataQuality",
    "DataQualityIssue",
    "DataType",
    "Direction",
    "DiscrepancyType",
    "ExitAction",
    "ExitReason",
    "ExpectedMagnitude",
    "LegAction",
    "MarketDataOrigin",
    "MarketHypothesis",
    "OptionRight",
    "Optionability",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PositionState",
    "ReconciliationStatus",
    "RegulatoryFormType",
    "RiskOutcome",
    "RiskReasonCode",
    "SecurityType",
    "SelectionMethod",
    "SourceTier",
    "StrategyAction",
    "StrategyType",
    "ThesisStatus",
    "TimeInForce",
    "TradingMode",
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

    Exactly one hypothesis per underlying (specification section 6).
    """

    A = "A"  # Strong move expected in either direction; direction uncertain
    B = "B"  # Predominantly bullish
    C = "C"  # Predominantly bearish
    D = "D"  # Sharp move expected after a specific event; direction uncertain
    E = "E"  # Other; free-text explanation required


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
    """Why an exit was triggered (specification section 19)."""

    TRAILING_STOP = "TRAILING_STOP"
    EXPIRATION_POLICY = "EXPIRATION_POLICY"
    THESIS_INVALIDATION = "THESIS_INVALIDATION"
    RISK_LIMIT = "RISK_LIMIT"
    BROKER_SAFETY = "BROKER_SAFETY"
    EMERGENCY = "EMERGENCY"


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
