"""Canonical enumerations for the trading system.

Every enum is a :class:`~enum.StrEnum` so that values serialise to plain JSON
strings and round-trip through the workflow-boundary schemas in ``schemas/``.

These values are part of the persisted trade record. Renaming a member is a
breaking change to historical snapshots — add a new member instead.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = [
    "DataQuality",
    "Direction",
    "ExitAction",
    "ExitReason",
    "ExpectedMagnitude",
    "LegAction",
    "MarketHypothesis",
    "OptionRight",
    "OrderStatus",
    "OrderType",
    "PositionState",
    "ReconciliationStatus",
    "RiskOutcome",
    "RiskReasonCode",
    "SourceTier",
    "StrategyAction",
    "StrategyType",
    "ThesisStatus",
    "TimeInForce",
    "TradingMode",
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
