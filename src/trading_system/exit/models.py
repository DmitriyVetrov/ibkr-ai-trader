"""Canonical exit-management artifacts.

Milestone 10 answers exactly one question — *should this existing position be
closed?* — and the shapes here are what stop it answering another. There is no
field anywhere in this module for a budget, an allocation, a position size, a
new contract, a model name, a prompt or a rationale, and tests assert their
absence. Quantity is *copied* from what the broker actually holds; nothing here
computes one.

Seven artifacts, and what each one is for:

:class:`ExitPolicySnapshot`
    The effective policy for one position, with the layer that supplied every
    value recorded next to it. A stored decision must be explicable without
    the configuration that produced it, because the configuration will change.
:class:`TrailingStopRecord`
    The trailing stop's own state, persisted. Not a mutable price: the peak,
    the level, when each was set and what set it, so an exit can be explained
    after the fact rather than asserted.
:class:`PositionValuation`
    What the position is worth right now, against a *named* quote field, with
    the snapshot, the clock, the provider and the data layer's verdict attached
    per leg. A valuation that cannot say where its price came from is not
    evidence of anything.
:class:`ExitPolicyOutcome`
    One policy's verdict, in precedence order.
:class:`ExitEvaluation`
    Every outcome, the inputs they were computed from, and the content hash
    that makes a repeated evaluation recognisable as a repeat.
:class:`ExitDecisionRecord`
    The verdict: ``WAIT``, ``EXIT`` or ``BLOCK``, with reason codes. Projects
    onto the Milestone 1 :class:`~trading_system.domain.models.ExitDecision`.
:class:`PositionLifecycleSnapshot` / :class:`PositionLifecycleEvent`
    What has become of the position and of our attempt to end it. Append-only,
    folded on read, exactly like an execution's history.

**Units are the subtle part, again.** Three numbers describe the price of one
option structure and mixing any two is a factor-of-100 error:

``*_quote``
    The broker's quoted terms — 6.05. What a limit price and a fill price are
    in, and what a trailing level is compared against.
``*_value`` / ``*_cost``
    Money for one unit of the structure, multiplier included — 605.00.
``*_total``
    Money for the whole holding.

Nothing here converts silently between them, and a conversion needing a
multiplier nobody reported yields ``None`` rather than an assumed 100.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    EXIT_BLOCK_REASONS,
    EXIT_SUBMISSION_BLOCKED_STATES,
    EXIT_TRIGGER_REASONS,
    EXIT_WAIT_REASONS,
    DataQuality,
    ExitAction,
    ExitDecisionType,
    ExitPolicyKind,
    ExitQuoteField,
    ExitReason,
    ExitReasonCode,
    ExitRunStatus,
    MaxLossBasis,
    OptionRight,
    OrderType,
    PositionLifecycleEventType,
    PositionLifecycleState,
    RiskLimitScope,
    StrategyType,
    StructureStatus,
    ThesisConditionOutcome,
    TimeInForce,
    TradingMode,
    TrailingStopState,
)
from trading_system.domain.models import (
    ExitDecision,
    Identifier,
    ImmutableModel,
    Money,
    SystemVersions,
    Ticker,
    UtcDatetime,
)

__all__ = [
    "EXIT_SCHEMA_VERSION",
    "ExitDecisionRecord",
    "ExitEvaluation",
    "ExitLegValuation",
    "ExitPolicyOutcome",
    "ExitPolicySnapshot",
    "ExitRequest",
    "ExitRunCounts",
    "ExitRunResult",
    "PositionLifecycleEvent",
    "PositionLifecycleSnapshot",
    "PositionValuation",
    "ThesisConditionCheck",
    "TrailingStopRecord",
    "exit_decision_identifier",
    "exit_evaluation_identifier",
    "exit_request_identifier",
    "exit_run_identifier",
    "lifecycle_event_identifier",
    "lifecycle_snapshot_identifier",
    "trailing_state_identifier",
]

#: Bumped when a stored exit artifact changes shape. Folded into every derived
#: identifier, so records written under different shapes cannot collide.
EXIT_SCHEMA_VERSION = "1.0.0"

#: How Milestone 10's wide reason vocabulary projects onto the narrow Milestone
#: 1 :class:`ExitReason`.
#:
#: Only the five trigger reasons appear. A ``WAIT`` carries no Milestone 1
#: reason by construction (``HOLD`` must not), and a ``BLOCK`` has no Milestone
#: 1 shape at all — see :meth:`ExitDecisionRecord.to_exit_decision`.
_M1_EXIT_REASON: dict[ExitReasonCode, ExitReason] = {
    ExitReasonCode.TRAILING_STOP_TRIGGERED: ExitReason.TRAILING_STOP,
    ExitReasonCode.EXPIRATION_FORCE_EXIT: ExitReason.EXPIRATION_POLICY,
    ExitReasonCode.THESIS_INVALIDATED: ExitReason.THESIS_INVALIDATION,
    ExitReasonCode.MAX_LOSS_REACHED: ExitReason.RISK_LIMIT,
    ExitReasonCode.TAKE_PROFIT_REACHED: ExitReason.TAKE_PROFIT,
}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def exit_evaluation_identifier(
    *,
    position_id: str,
    as_of: datetime,
    content_digest: str,
    policy_version: str,
    schema_version: str = EXIT_SCHEMA_VERSION,
) -> str:
    """Derive an evaluation's identity from what it looked at and concluded.

    ``content_digest`` deliberately excludes every observation clock, exactly
    as the data layer's snapshot identity and Milestone 9's reconciliation
    identity do: evaluating unchanged state again is a *re-observation* of one
    judgement rather than a second judgement that happens to agree. The instant
    stays in the id so two genuinely separate evaluations of the same state
    remain distinguishable.
    """
    digest = stable_hash(
        [
            "EXIT_EVALUATION",
            schema_version,
            position_id,
            as_of.isoformat(),
            content_digest,
            policy_version,
        ]
    )
    return f"exiteval-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}"


def exit_decision_identifier(
    *, evaluation_id: str, schema_version: str = EXIT_SCHEMA_VERSION
) -> str:
    """One evaluation produces exactly one decision, so the id follows from it."""
    digest = stable_hash(["EXIT_DECISION", schema_version, evaluation_id])
    return f"exitdec-{digest[:20]}"


def trailing_state_identifier(
    *, position_id: str, schema_version: str = EXIT_SCHEMA_VERSION
) -> str:
    """A position has one trailing stop, for its whole life.

    Deliberately free of any clock: the record is *folded* from its events, so
    a stable id is what lets the fold find them. A per-observation id would
    turn one ratcheting level into a pile of unrelated snapshots.
    """
    digest = stable_hash(["TRAILING_STATE", schema_version, position_id])
    return f"trailing-{digest[:20]}"


def lifecycle_snapshot_identifier(
    *,
    position_id: str,
    as_of: datetime,
    content_digest: str,
    schema_version: str = EXIT_SCHEMA_VERSION,
) -> str:
    digest = stable_hash(
        ["POSITION_LIFECYCLE", schema_version, position_id, as_of.isoformat(), content_digest]
    )
    return f"lifecycle-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}"


def lifecycle_event_identifier(
    *,
    position_id: str,
    sequence: int,
    event_type: str,
    schema_version: str = EXIT_SCHEMA_VERSION,
) -> str:
    digest = stable_hash(
        ["POSITION_LIFECYCLE_EVENT", schema_version, position_id, sequence, event_type]
    )
    return f"lifeevt-{digest[:20]}"


def exit_request_identifier(
    *,
    position_id: str,
    entry_execution_id: str,
    trading_mode: TradingMode,
    order_type: OrderType,
    time_in_force: TimeInForce,
    policy_version: str,
    schema_version: str = EXIT_SCHEMA_VERSION,
) -> str:
    """Derive the identity of *this position, exited this way*.

    What makes an exit submission idempotent, and every term is load bearing
    for the same reasons the entry side's
    :func:`~trading_system.execution.models.execution_request_identifier` gives
    — with one addition and one deliberate omission.

    The addition is ``position_id``: an exit is against a position, not against
    an allocation, and two different positions descending from one allocation
    must not share an exit identity.

    The omission is the decision. An exit triggered by a trailing stop and the
    same exit triggered a minute later by the expiration policy are the *same
    order for the same position*, and giving them different identities would
    let the second one send a second order. Which policy triggered is recorded
    on the record; it does not participate in identity.

    Excludes the clock, exactly as the entry side does: an identity that
    changed with time would make every retry look new, which is precisely the
    duplicate-order bug this exists to prevent.
    """
    digest = stable_hash(
        [
            "EXIT_REQUEST",
            schema_version,
            position_id,
            entry_execution_id,
            trading_mode.value,
            order_type.value,
            time_in_force.value,
            policy_version,
        ]
    )
    return f"exit-req-{digest[:20]}"


def exit_run_identifier(
    *,
    as_of: datetime,
    position_ids: list[str],
    lifecycle_state: list[str] | None,
    policy_version: str,
    config_version: str,
    trading_mode: TradingMode,
    dry_run: bool,
    schema_version: str = EXIT_SCHEMA_VERSION,
) -> str:
    """Derive one monitoring run's identity from what it was asked to judge.

    ``lifecycle_state`` is what makes this correct rather than merely unique,
    and it is the same lesson the allocation and execution ledgers record: the
    same positions evaluated against a ledger that has since submitted an exit
    are a *different* decision reaching a different answer. Deriving the id from
    the positions alone would collide the two and the immutable store would
    refuse to write the second.
    """
    digest = stable_hash(
        [
            "EXIT_RUN",
            schema_version,
            as_of.isoformat(),
            sorted(position_ids),
            sorted(lifecycle_state or []),
            policy_version,
            config_version,
            trading_mode.value,
            dry_run,
        ]
    )
    return f"exitrun-{digest[:20]}"


# ---------------------------------------------------------------------------
# The effective policy
# ---------------------------------------------------------------------------
class ExitPolicySnapshot(ImmutableModel):
    """The policy actually in force for one position, and where each value came from.

    Copied onto every evaluation rather than looked up when a stored decision
    is read back. Configuration changes; a decision made under the old
    thresholds has to stay explicable under the new ones, and "the trailing
    stop was 30% and the strategy supplied it" is not reconstructible from a
    file that has since been edited.
    """

    policy_version: Identifier
    strategy: StrategyType

    expiration_warning_dte: int = Field(ge=0)
    expiration_force_exit_dte: int = Field(ge=0)
    trailing_enabled: bool = True
    trailing_activation_return_pct: float = Field(ge=0.0)
    trailing_distance_pct: float = Field(gt=0.0)
    trailing_min_improvement_pct: float = Field(ge=0.0)
    take_profit_enabled: bool = True
    take_profit_return_pct: float | None = Field(default=None, gt=0.0)
    max_loss_enabled: bool = True
    max_loss_pct: float = Field(gt=0.0, le=100.0)
    thesis_enabled: bool = True
    quote_field: ExitQuoteField = ExitQuoteField.BID
    max_quote_age_seconds: int = Field(ge=0)
    require_research_usable: bool = True

    #: Which layer supplied each effective value. ``GLOBAL`` means the strategy
    #: stated nothing and ``config/exit.yaml`` binds; ``STRATEGY`` means the
    #: strategy narrowed it. A strategy can never appear here having widened
    #: one — that fails at configuration load.
    scopes: dict[str, RiskLimitScope] = Field(default_factory=dict)

    #: Structural, and refused if ever true. A straddle exits whole.
    allow_independent_leg_exit: bool = False

    @model_validator(mode="after")
    def _a_structure_exits_whole(self) -> ExitPolicySnapshot:
        if self.allow_independent_leg_exit:
            raise ValueError(
                "an exit policy snapshot cannot permit an independent leg exit: closing one "
                "leg of a straddle leaves a naked long option against limits nobody checked "
                "for it"
            )
        return self


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------
class ExitLegValuation(ImmutableModel):
    """One leg's exit price, and everything needed to believe it.

    ``price`` is the *configured* quote field's value and nothing else. When it
    is absent the leg is unpriced and says so; no other field stands in for it,
    which is why ``bid``, ``ask``, ``last`` and ``mid`` are recorded separately
    and are never read as substitutes.
    """

    leg_index: int = Field(ge=0)
    contract_id: int | None = None
    key: Identifier
    right: OptionRight | None = None
    strike: Money | None = Field(default=None, gt=0)
    expiration: date | None = None
    ratio: int = Field(default=1, ge=1)
    multiplier: int | None = Field(default=None, ge=1)
    #: Contracts of this leg the broker actually reports holding.
    observed_quantity: Money | None = None

    # --- the price, and only the configured one ---------------------------
    quote_field: ExitQuoteField
    #: The configured field's value, in the broker's quoted terms. ``None``
    #: means this leg has no usable exit price, and nothing substitutes one.
    price: Money | None = Field(default=None, ge=0)

    bid: Money | None = Field(default=None, ge=0)
    ask: Money | None = Field(default=None, ge=0)
    last: Money | None = Field(default=None, ge=0)

    # --- provenance --------------------------------------------------------
    quote_snapshot_id: Identifier | None = None
    quote_as_of: UtcDatetime | None = None
    quote_retrieved_at: UtcDatetime | None = None
    provider: Identifier | None = None
    data_quality: DataQuality | None = None
    research_usable: bool | None = None
    quote_age_seconds: float | None = None
    detail: str | None = None

    @property
    def priced(self) -> bool:
        return self.price is not None


class PositionValuation(ImmutableModel):
    """What the whole structure is worth right now, and what it cost.

    One record, three units, named apart. The structure's exit *quote* is the
    sum over legs of the configured field times the leg ratio; its *value* is
    that quote times the shared multiplier; its *total* is that value times the
    open quantity.

    Every derived figure is ``None`` when any leg is unpriced. A structure
    valued from the legs that happened to have a quote would be a different
    structure — half a straddle is a directional bet — and reporting a number
    for it would be the fabrication this milestone exists to refuse.
    """

    as_of: UtcDatetime
    quote_field: ExitQuoteField
    multiplier: int | None = Field(default=None, ge=1)
    #: Units of the structure the broker reports holding, from the weakest leg.
    open_quantity: int = Field(ge=0)
    currency: str | None = None

    legs: list[ExitLegValuation] = Field(default_factory=list)

    # --- what it cost (from confirmed fills, never re-derived) -------------
    #: The entry price in the broker's quoted terms, as reported at the fill.
    entry_quote: Money | None = Field(default=None, gt=0)
    #: The same figure as money for one unit of the structure.
    entry_cost: Money | None = Field(default=None, gt=0)

    # --- what it is worth now ---------------------------------------------
    exit_quote: Money | None = Field(default=None, ge=0)
    exit_value: Money | None = Field(default=None, ge=0)

    #: The oldest contributing quote's age, which is the one freshness policy
    #: is judged against: a structure is only as fresh as its stalest leg.
    max_quote_age_seconds: float | None = None
    unpriced_legs: list[int] = Field(default_factory=list)
    detail: str | None = None

    @model_validator(mode="after")
    def _a_structure_is_priced_whole_or_not_at_all(self) -> PositionValuation:
        missing = [leg.leg_index for leg in self.legs if not leg.priced]
        if missing and self.exit_quote is not None:
            raise ValueError(
                f"legs {missing} carry no {self.quote_field.value} price, but the structure "
                f"reports an exit quote of {self.exit_quote}. A structure priced from the legs "
                f"that happened to be quoted is a different structure"
            )
        return self

    @property
    def priced(self) -> bool:
        return self.exit_quote is not None and not self.unpriced_legs

    @property
    def entry_total(self) -> Decimal | None:
        """Money the confirmed fills paid for what is still held."""
        if self.entry_cost is None or not self.open_quantity:
            return None
        return self.entry_cost * Decimal(self.open_quantity)

    @property
    def exit_total(self) -> Decimal | None:
        if self.exit_value is None or not self.open_quantity:
            return None
        return self.exit_value * Decimal(self.open_quantity)

    @property
    def unrealized_pnl(self) -> Decimal | None:
        """Money, for the whole holding. ``None`` when either side is unknown."""
        entry, current = self.entry_total, self.exit_total
        if entry is None or current is None:
            return None
        return current - entry

    @property
    def return_pct(self) -> Decimal | None:
        """Gain over what was paid, as a percentage. Identical in either unit.

        Both the entry and the exit figures scale by the same multiplier, so
        the ratio is the same whether taken in quoted terms or in money. It is
        computed from the money figures because those are what the maximum-loss
        policy is stated in, and using one basis throughout is what keeps the
        two policies comparable.
        """
        if self.entry_cost is None or self.exit_value is None or self.entry_cost <= 0:
            return None
        return (self.exit_value - self.entry_cost) / self.entry_cost * Decimal(100)


# ---------------------------------------------------------------------------
# Trailing
# ---------------------------------------------------------------------------
class TrailingStopRecord(ImmutableModel):
    """A position's trailing stop, with the history that explains it.

    The invariant this model enforces, rather than trusts: **the level never
    falls**. A trailing stop that follows a position down is not a stop, and
    the failure is silent — the position simply never sells, however much of
    its peak it has given back.

    Everything is in the broker's *quoted* terms, consistently, because that is
    what a market observation and a limit price are in and converting on every
    comparison would be a multiplier waiting to be forgotten.
    """

    trailing_state_id: Identifier
    position_id: Identifier
    schema_version: Identifier = EXIT_SCHEMA_VERSION
    state: TrailingStopState = TrailingStopState.INACTIVE

    quote_field: ExitQuoteField = ExitQuoteField.BID
    activation_return_pct: float = Field(ge=0.0)
    distance_pct: float = Field(gt=0.0)
    min_improvement_pct: float = Field(default=0.0, ge=0.0)

    #: The entry cost in quoted terms; the activation threshold is measured
    #: against it. Copied from confirmed fills, never re-derived.
    entry_quote: Money | None = Field(default=None, gt=0)
    #: The highest favourable observation so far, in quoted terms. Monotone.
    peak_quote: Money | None = Field(default=None, ge=0)
    #: The level the position exits at, in quoted terms. Monotone.
    stop_quote: Money | None = Field(default=None, ge=0)

    #: What actually activated the trail, kept verbatim.
    activation_quote: Money | None = Field(default=None, ge=0)
    activated_at: UtcDatetime | None = None
    peak_at: UtcDatetime | None = None
    level_updated_at: UtcDatetime | None = None
    triggered_at: UtcDatetime | None = None
    #: The observation that crossed the level. The answer to "why did this
    #: sell", recorded at the moment it stops being reconstructible.
    trigger_quote: Money | None = Field(default=None, ge=0)

    observations: int = Field(default=0, ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime
    detail: str | None = None

    @model_validator(mode="after")
    def _an_active_trail_has_a_level(self) -> TrailingStopRecord:
        if self.state in (TrailingStopState.ARMED, TrailingStopState.ACTIVE) and (
            self.stop_quote is None or self.peak_quote is None
        ):
            raise ValueError(
                f"trailing stop {self.trailing_state_id} is {self.state.value} without a "
                f"peak and a level; an active trail with no level is not a stop"
            )
        if self.state is TrailingStopState.TRIGGERED and self.trigger_quote is None:
            raise ValueError(
                f"trailing stop {self.trailing_state_id} is TRIGGERED without recording the "
                f"observation that crossed the level. That observation is the whole explanation "
                f"of the exit"
            )
        if self.state is TrailingStopState.INACTIVE and self.stop_quote is not None:
            raise ValueError(
                "an INACTIVE trailing stop cannot carry a level: the activation threshold has "
                "not been reached, so there is nothing to trail from"
            )
        return self

    @model_validator(mode="after")
    def _a_level_never_exceeds_its_peak(self) -> TrailingStopRecord:
        if (
            self.stop_quote is not None
            and self.peak_quote is not None
            and self.stop_quote > self.peak_quote
        ):
            raise ValueError(
                f"trailing level {self.stop_quote} is above the peak {self.peak_quote}, "
                f"which would exit the position at a price better than it ever reached"
            )
        return self

    @property
    def active(self) -> bool:
        return self.state in (TrailingStopState.ARMED, TrailingStopState.ACTIVE)


# ---------------------------------------------------------------------------
# Thesis
# ---------------------------------------------------------------------------
class ThesisConditionCheck(ImmutableModel):
    """One invalidation condition, and what a deterministic check concluded.

    ``NOT_EVALUATED`` is the expected answer for most conditions and is not a
    failure. The condition text is carried verbatim so an operator can read it,
    and ``observable`` names what research said one would have to look at —
    but neither is ever *interpreted*. A sentence is not a signal.
    """

    condition: str = Field(min_length=1)
    observable: str | None = None
    outcome: ThesisConditionOutcome = ThesisConditionOutcome.NOT_EVALUATED
    #: Which structured fact settled it, when one did. ``None`` for
    #: ``NOT_EVALUATED``, and a validator keeps it that way.
    evidence: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _a_verdict_names_its_evidence(self) -> ThesisConditionCheck:
        if (
            self.outcome is not ThesisConditionOutcome.NOT_EVALUATED
            and not (self.evidence or "").strip()
        ):
            raise ValueError(
                f"condition {self.condition!r} is recorded {self.outcome.value} without naming "
                f"the structured fact that settled it. A verdict with no evidence behind it is "
                f"an interpretation, which this engine does not make"
            )
        if self.outcome is ThesisConditionOutcome.NOT_EVALUATED and self.evidence:
            raise ValueError("a NOT_EVALUATED condition cannot carry evidence: nothing was checked")
        return self


# ---------------------------------------------------------------------------
# Policy outcomes and the evaluation
# ---------------------------------------------------------------------------
class ExitPolicyOutcome(ImmutableModel):
    """What one deterministic policy concluded, and on what figures.

    ``measured`` and ``threshold`` are strings so an exact decimal survives
    storage without becoming a float. They are the two numbers an operator
    compares when asking "how close was this", and a policy that reported only
    its verdict would make that question unanswerable.
    """

    policy: ExitPolicyKind
    decision: ExitDecisionType
    reason_code: ExitReasonCode
    #: What this policy actually measured, as an exact decimal string.
    measured: str | None = None
    #: What it was compared against.
    threshold: str | None = None
    summary: str = Field(min_length=1)
    detail: str | None = None
    #: Set when the policy could not be evaluated at all. Distinct from a pass,
    #: exactly as ``RiskCheckOutcome.NOT_EVALUATED`` is.
    evaluated: bool = True

    @model_validator(mode="after")
    def _the_reason_belongs_to_the_verdict(self) -> ExitPolicyOutcome:
        expected = {
            ExitDecisionType.WAIT: EXIT_WAIT_REASONS,
            ExitDecisionType.EXIT: EXIT_TRIGGER_REASONS,
            ExitDecisionType.BLOCK: EXIT_BLOCK_REASONS,
        }[self.decision]
        if self.reason_code not in expected:
            raise ValueError(
                f"{self.policy.value} reports {self.decision.value} with reason "
                f"{self.reason_code.value}, which is not a {self.decision.value} reason. The "
                f"three vocabularies partition ExitReasonCode and must stay disjoint"
            )
        return self


class ExitEvaluation(ImmutableModel):
    """One complete evaluation of one position: inputs, outcomes and identity.

    Immutable and content-addressed. The content hash covers what was measured
    and what was concluded, and deliberately excludes every observation clock,
    so re-evaluating unchanged state is recognisable as a repeat rather than as
    a second judgement.
    """

    evaluation_id: Identifier
    position_id: Identifier
    as_of: UtcDatetime
    evaluated_at: UtcDatetime
    schema_version: Identifier = EXIT_SCHEMA_VERSION

    # --- what is being judged ---------------------------------------------
    underlying: Ticker
    strategy: StrategyType
    lifecycle_state: PositionLifecycleState
    structure_status: StructureStatus
    open_quantity: int = Field(ge=0)
    days_to_expiration: int | None = None
    expiration: date | None = None
    max_loss_basis: MaxLossBasis | None = None
    #: Money at risk, from the strategy's declared basis. ``None`` when the
    #: basis is one this system cannot compute — a block, never an estimate.
    max_loss_total: Money | None = Field(default=None, ge=0)

    valuation: PositionValuation
    trailing: TrailingStopRecord | None = None
    thesis_checks: list[ThesisConditionCheck] = Field(default_factory=list)
    policy: ExitPolicySnapshot

    # --- what every policy said, in precedence order ----------------------
    outcomes: list[ExitPolicyOutcome] = Field(default_factory=list)

    # --- provenance: ids, never copies ------------------------------------
    entry_execution_id: Identifier | None = None
    allocation_id: Identifier | None = None
    opportunity_id: Identifier | None = None
    campaign_id: Identifier | None = None
    research_report_id: Identifier | None = None
    strategy_decision_id: Identifier | None = None
    contract_selection_id: Identifier | None = None
    position_snapshot_id: Identifier | None = None
    reconciliation_id: Identifier | None = None

    content_hash: Identifier
    versions: SystemVersions

    #: Structurally zero. An evaluation reads; it never sends. Read off the
    #: broker where one was involved, so the zero is evidence.
    orders_submitted: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _an_evaluation_submits_nothing(self) -> ExitEvaluation:
        if self.orders_submitted:
            raise ValueError(
                f"an exit evaluation reported {self.orders_submitted} submitted order(s). "
                f"Deciding whether a position should close never places an order"
            )
        return self

    def outcome_for(self, policy: ExitPolicyKind) -> ExitPolicyOutcome | None:
        return next((outcome for outcome in self.outcomes if outcome.policy is policy), None)

    @property
    def blocking_outcomes(self) -> list[ExitPolicyOutcome]:
        return [o for o in self.outcomes if o.decision is ExitDecisionType.BLOCK]

    @property
    def triggering_outcomes(self) -> list[ExitPolicyOutcome]:
        return [o for o in self.outcomes if o.decision is ExitDecisionType.EXIT]


class ExitDecisionRecord(ImmutableModel):
    """The verdict for one position, and everything it rests on.

    The Milestone 10 audit artifact. It projects onto the Milestone 1
    :class:`~trading_system.domain.models.ExitDecision` through
    :meth:`to_exit_decision`, exactly as research, strategy, execution and
    reconciliation project onto their Milestone 1 shapes.

    ``close_whole_strategy`` is ``True`` and a validator keeps it there. A
    multi-leg structure exits as one order; there is no independent-leg exit
    path in this milestone, and this is the field that would have to lie for
    there to be one.
    """

    decision_id: Identifier
    evaluation_id: Identifier
    position_id: Identifier
    as_of: UtcDatetime
    decided_at: UtcDatetime
    schema_version: Identifier = EXIT_SCHEMA_VERSION

    decision: ExitDecisionType
    #: Every reason that contributed, most significant first. An ``EXIT``
    #: carries exactly one trigger; a ``BLOCK`` may carry several, because an
    #: operator resolving one wants to know about the rest.
    reason_codes: list[ExitReasonCode] = Field(min_length=1)
    #: Which policy produced the decisive reason.
    triggering_policy: ExitPolicyKind | None = None

    underlying: Ticker
    strategy: StrategyType
    lifecycle_state: PositionLifecycleState
    #: Copied from what the broker holds. Nothing here computes a quantity.
    quantity: int = Field(ge=0)
    close_whole_strategy: bool = True

    # --- the figures the decision rested on -------------------------------
    exit_quote: Money | None = Field(default=None, ge=0)
    exit_value: Money | None = Field(default=None, ge=0)
    entry_cost: Money | None = Field(default=None, gt=0)
    unrealized_pnl: Money | None = None
    return_pct: str | None = None
    days_to_expiration: int | None = None
    quote_field: ExitQuoteField = ExitQuoteField.BID
    data_quality: DataQuality | None = None
    currency: str | None = None

    summary: str = Field(min_length=1)
    detail: str | None = None
    #: What a person should do about a ``BLOCK``. Never an instruction to
    #: trade: a blocked position is one nobody should act on until the reason
    #: is resolved, and naming a trade here would be exactly that action.
    recommended_action: str | None = None

    policy_version: Identifier
    entry_execution_id: Identifier | None = None
    opportunity_id: Identifier | None = None
    campaign_id: Identifier | None = None
    trading_mode: TradingMode
    versions: SystemVersions

    @model_validator(mode="after")
    def _every_reason_belongs_to_the_verdict(self) -> ExitDecisionRecord:
        expected = {
            ExitDecisionType.WAIT: EXIT_WAIT_REASONS,
            ExitDecisionType.EXIT: EXIT_TRIGGER_REASONS,
            ExitDecisionType.BLOCK: EXIT_BLOCK_REASONS,
        }[self.decision]
        stray = [code.value for code in self.reason_codes if code not in expected]
        if stray:
            raise ValueError(
                f"a {self.decision.value} decision carries reason(s) {stray} that belong to a "
                f"different verdict"
            )
        if self.decision is ExitDecisionType.EXIT and len(self.reason_codes) != 1:
            raise ValueError(
                f"an EXIT names exactly one triggering policy, got "
                f"{[c.value for c in self.reason_codes]}. Precedence decides which policy "
                f"exits a position, and recording several would hide which one did"
            )
        return self

    @model_validator(mode="after")
    def _a_structure_exits_whole(self) -> ExitDecisionRecord:
        if not self.close_whole_strategy:
            raise ValueError(
                "an exit decision cannot close part of a structure. A straddle with one leg "
                "sold is a naked long option sized against limits nobody checked for it, and "
                "there is no independent-leg exit path in this milestone"
            )
        return self

    @model_validator(mode="after")
    def _an_exit_names_the_policy_that_triggered_it(self) -> ExitDecisionRecord:
        if self.decision is ExitDecisionType.EXIT and self.triggering_policy is None:
            raise ValueError(
                "an EXIT must name the policy that triggered it; 'sell' with no policy behind "
                "it is not a deterministic decision"
            )
        if self.decision is ExitDecisionType.EXIT and self.quantity < 1:
            raise ValueError(
                f"an EXIT for {self.quantity} contract(s) is not an exit. A position the "
                f"broker does not hold cannot be closed"
            )
        return self

    @property
    def requests_order(self) -> bool:
        return self.decision is ExitDecisionType.EXIT

    @property
    def primary_reason(self) -> ExitReasonCode:
        return self.reason_codes[0]

    def to_exit_decision(self, *, versions: SystemVersions | None = None) -> ExitDecision:
        """Project onto the Milestone 1 workflow boundary.

        The full record is the Milestone 10 artifact; this is the narrow
        contract the rest of the system was built against
        (``schemas/exit_decision.json``).

        Raises for ``BLOCK``. :class:`ExitAction` has exactly two members, and
        neither is honest about a refusal: ``HOLD`` would record a considered
        decision to keep a position when what happened is that no decision
        could be made, and ``SELL`` would be catastrophically wrong. A block is
        the absence of a decision, and the Milestone 1 vocabulary has no shape
        for one.
        """
        if self.decision is ExitDecisionType.BLOCK:
            raise ValueError(
                f"exit decision {self.decision_id} is BLOCK "
                f"({', '.join(c.value for c in self.reason_codes)}), which the Milestone 1 "
                f"ExitAction vocabulary cannot express. HOLD would claim a decision to keep "
                f"this position; what actually happened is that no decision could be made"
            )
        if self.decision is ExitDecisionType.WAIT:
            return ExitDecision(
                decision_id=self.decision_id,
                position_id=self.position_id,
                as_of=self.as_of,
                decision=ExitAction.HOLD,
                reason=None,
                detail=self.summary,
                close_whole_strategy=True,
                versions=versions or self.versions,
            )

        reason = _M1_EXIT_REASON.get(self.primary_reason)
        if reason is None:  # pragma: no cover - defended by the partition test
            raise ValueError(
                f"exit reason {self.primary_reason.value} has no Milestone 1 ExitReason"
            )
        return ExitDecision(
            decision_id=self.decision_id,
            position_id=self.position_id,
            as_of=self.as_of,
            decision=ExitAction.SELL,
            reason=reason,
            detail=self.summary,
            close_whole_strategy=True,
            versions=versions or self.versions,
        )


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------
class PositionLifecycleSnapshot(ImmutableModel):
    """What has become of one position, and of our attempt to end it.

    Deliberately **not** a second copy of Milestone 9's position reality. This
    record holds what *exit management* knows: which lifecycle state the
    position is in, which evaluation last looked at it, and which exit
    execution — if any — was requested for it. Quantities and broker facts are
    referenced by id and read from Milestone 9, which stays authoritative.
    """

    lifecycle_id: Identifier
    position_id: Identifier
    as_of: UtcDatetime
    updated_at: UtcDatetime
    schema_version: Identifier = EXIT_SCHEMA_VERSION

    state: PositionLifecycleState
    underlying: Ticker
    strategy: StrategyType
    #: What the broker holds, copied from Milestone 9. Never computed here.
    open_quantity: int = Field(ge=0)

    entry_execution_id: Identifier | None = None
    opportunity_id: Identifier | None = None
    allocation_id: Identifier | None = None
    campaign_id: Identifier | None = None
    research_report_id: Identifier | None = None
    strategy_decision_id: Identifier | None = None

    #: The exit this position is waiting on, if any.
    exit_execution_id: Identifier | None = None
    exit_request_id: Identifier | None = None
    exit_decision_id: Identifier | None = None
    exit_submitted_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None

    last_evaluation_id: Identifier | None = None
    last_decision: ExitDecisionType | None = None
    evaluations: int = Field(default=0, ge=0)
    blocked_reason: ExitReasonCode | None = None
    content_hash: Identifier | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _a_blocked_position_says_why(self) -> PositionLifecycleSnapshot:
        if self.state is PositionLifecycleState.BLOCKED and self.blocked_reason is None:
            raise ValueError(
                f"position {self.position_id} is BLOCKED without a reason. A block that cannot "
                f"be explained cannot be resolved, and this state is only left by resolving it"
            )
        return self

    @model_validator(mode="after")
    def _a_submitted_exit_names_its_execution(self) -> PositionLifecycleSnapshot:
        if (
            self.state
            in (PositionLifecycleState.EXIT_SUBMITTED, PositionLifecycleState.EXIT_UNKNOWN)
            and self.exit_execution_id is None
        ):
            raise ValueError(
                f"position {self.position_id} is {self.state.value} but names no exit "
                f"execution. An order we cannot name is one we cannot resolve or reconcile"
            )
        return self

    @model_validator(mode="after")
    def _a_closed_position_holds_nothing(self) -> PositionLifecycleSnapshot:
        if self.state is PositionLifecycleState.CLOSED and self.open_quantity:
            raise ValueError(
                f"position {self.position_id} is CLOSED while the broker reports "
                f"{self.open_quantity} contract(s). CLOSED is a statement about broker reality, "
                f"not about our intention"
            )
        return self

    @property
    def terminal(self) -> bool:
        return self.state is PositionLifecycleState.CLOSED

    @property
    def may_submit_exit(self) -> bool:
        """Whether a new exit order may be built for this position.

        ``False`` for ``EXIT_SUBMITTED`` and ``EXIT_UNKNOWN`` alike: both mean
        an exit order may be live at the broker, and re-submitting over either
        closes the position twice.
        """
        return self.state not in EXIT_SUBMISSION_BLOCKED_STATES

    def with_event(self, event: PositionLifecycleEvent) -> PositionLifecycleSnapshot:
        """Fold one observation onto this record, returning a new one.

        Reconstructed through the model rather than ``model_copy``-ed, exactly
        as :meth:`~trading_system.reservations.models.Reservation.with_event`
        is: a copy does not revalidate, so an event that produced a record that
        cannot be true — CLOSED while the broker still holds contracts — would
        surface later as a wrong screen rather than here as an error.
        """
        from trading_system.exit.lifecycle import validate_lifecycle_transition

        if event.position_id != self.position_id:
            raise ValueError(
                f"event {event.event_id} belongs to position {event.position_id}, not "
                f"{self.position_id}"
            )
        if event.state is not self.state:
            validate_lifecycle_transition(self.state, event.state)

        payload = self.model_dump()
        payload.update(
            {
                "state": event.state,
                "updated_at": event.observed_at,
                "as_of": event.occurred_at,
            }
        )
        for field in (
            "exit_execution_id",
            "exit_request_id",
            "exit_decision_id",
            "last_evaluation_id",
            "open_quantity",
        ):
            value = getattr(event, field, None)
            if value is not None:
                payload[field] = value
        if event.decision is not None:
            payload["last_decision"] = event.decision
            payload["evaluations"] = self.evaluations + 1
        if event.state is PositionLifecycleState.BLOCKED:
            payload["blocked_reason"] = event.reason_code
        else:
            payload["blocked_reason"] = None
        if event.event_type is PositionLifecycleEventType.EXIT_SUBMITTED:
            payload["exit_submitted_at"] = event.occurred_at
        if event.state is PositionLifecycleState.CLOSED:
            payload["closed_at"] = event.occurred_at
            payload["open_quantity"] = 0
        if event.detail:
            payload["detail"] = event.detail
        return PositionLifecycleSnapshot.model_validate(payload)


class PositionLifecycleEvent(ImmutableModel):
    """One appended observation about a position's lifecycle.

    ``occurred_at`` is when the thing happened and ``observed_at`` when we
    learned of it. Kept apart for the same reason the execution ledger keeps
    them apart: collapsing them loses the only means of telling a slow monitor
    from a slow market.
    """

    event_id: Identifier
    position_id: Identifier
    #: Monotonic within one position, so ordering survives equal timestamps.
    sequence: int = Field(ge=0)
    event_type: PositionLifecycleEventType
    #: The state the position is in *after* this event.
    state: PositionLifecycleState
    occurred_at: UtcDatetime
    observed_at: UtcDatetime
    source: Identifier
    schema_version: Identifier = EXIT_SCHEMA_VERSION

    decision: ExitDecisionType | None = None
    reason_code: ExitReasonCode | None = None
    detail: str | None = None

    last_evaluation_id: Identifier | None = None
    exit_decision_id: Identifier | None = None
    exit_request_id: Identifier | None = None
    exit_execution_id: Identifier | None = None
    open_quantity: int | None = Field(default=None, ge=0)

    # --- trailing, where this event moved it -------------------------------
    peak_quote: Money | None = Field(default=None, ge=0)
    stop_quote: Money | None = Field(default=None, ge=0)
    observed_quote: Money | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _a_block_names_its_reason(self) -> PositionLifecycleEvent:
        if self.state is PositionLifecycleState.BLOCKED and self.reason_code is None:
            raise ValueError("a BLOCKED lifecycle event must carry the reason code that caused it")
        return self


# ---------------------------------------------------------------------------
# The request handed to Milestone 8
# ---------------------------------------------------------------------------
class ExitRequest(ImmutableModel):
    """A deliberate instruction to close one position, handed to Milestone 8.

    The boundary artifact. Milestone 10 says *why* and *what*; Milestone 8's
    execution service owns *how* — validation, the order intent, the broker
    call, ``UNKNOWN`` handling, the broker order id and the execution record —
    and nothing here duplicates any of it.

    ``exit_authorized`` is required to be ``True`` and is not defaulted, for
    exactly the reason
    :class:`~trading_system.execution.models.ExecutionRequest` requires the
    same: a request object that can exist unauthorised is one a caller can
    forget to check.

    ``quantity`` is copied from the broker-observed position. No field here can
    express a different one, and there is no sizing anywhere in this milestone.
    """

    exit_request_id: Identifier
    position_id: Identifier
    decision_id: Identifier
    evaluation_id: Identifier
    created_at: UtcDatetime
    schema_version: Identifier = EXIT_SCHEMA_VERSION

    #: Must be True. A False value is a construction error.
    exit_authorized: bool
    requested_by: Identifier = "cli"

    underlying: Ticker
    strategy: StrategyType
    #: The whole holding, copied. An exit closes the structure.
    quantity: int = Field(ge=1)
    close_whole_strategy: bool = True

    exit_reason: ExitReasonCode
    triggering_policy: ExitPolicyKind
    #: The exit reference in the broker's quoted terms — the price the decision
    #: was made against. The limit actually sent is derived from it by
    #: Milestone 8's order builder, exactly once.
    reference_quote: Money = Field(gt=0)
    quote_field: ExitQuoteField
    quote_as_of: UtcDatetime | None = None
    currency: str | None = None

    order_type: OrderType
    time_in_force: TimeInForce
    trading_mode: TradingMode
    dry_run: bool = False

    # --- provenance: ids, never copies ------------------------------------
    entry_execution_id: Identifier
    allocation_id: Identifier
    campaign_id: Identifier
    opportunity_id: Identifier
    purchase_card_id: Identifier | None = None
    risk_decision_id: Identifier | None = None

    policy_version: Identifier
    versions: SystemVersions

    @model_validator(mode="after")
    def _authorisation_is_not_optional(self) -> ExitRequest:
        if not self.exit_authorized:
            raise ValueError(
                "an ExitRequest must carry exit_authorized=True. Deciding that a position "
                "should close is not permission to send an order; build no request rather "
                "than an unauthorised one"
            )
        return self

    @model_validator(mode="after")
    def _a_structure_exits_whole(self) -> ExitRequest:
        if not self.close_whole_strategy:
            raise ValueError(
                "an exit request cannot close part of a structure; there is no independent-leg "
                "exit path in this milestone"
            )
        return self

    @model_validator(mode="after")
    def _only_a_trigger_reason_closes_a_position(self) -> ExitRequest:
        if self.exit_reason not in EXIT_TRIGGER_REASONS:
            raise ValueError(
                f"exit reason {self.exit_reason.value} is not a reason to exit. Only a policy "
                f"that actually triggered may produce an exit request"
            )
        return self


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
class ExitRunCounts(ImmutableModel):
    """How many positions reached each verdict."""

    evaluated: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)
    exiting: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    closed: int = Field(default=0, ge=0)
    exits_submitted: int = Field(default=0, ge=0)
    exits_refused: int = Field(default=0, ge=0)


class ExitRunResult(ImmutableModel):
    """The immutable record of one monitoring or exit-evaluation run."""

    run_id: Identifier
    campaign_id: Identifier
    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: ExitRunStatus
    schema_version: Identifier = EXIT_SCHEMA_VERSION

    trading_mode: TradingMode
    dry_run: bool = False
    policy_version: Identifier
    #: True when this run was permitted to hand exits to Milestone 8. A run
    #: that only evaluated says so, and a validator refuses one that claims to
    #: have submitted anyway.
    execution_authorized: bool = False

    evaluations: list[ExitEvaluation] = Field(default_factory=list)
    decisions: list[ExitDecisionRecord] = Field(default_factory=list)
    counts: ExitRunCounts = Field(default_factory=ExitRunCounts)
    #: Exit executions this run created, by id. Milestone 8 owns the records.
    exit_execution_ids: list[str] = Field(default_factory=list)

    position_snapshot_id: Identifier | None = None
    reconciliation_id: Identifier | None = None

    #: Read off the broker after the run, never accumulated by this code.
    orders_submitted: int = Field(default=0, ge=0)
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _an_unauthorised_run_submits_nothing(self) -> ExitRunResult:
        """Evaluation never trades, and a dry run never trades.

        Enforced on the record rather than trusted to the service, because the
        single most damaging thing a monitoring cycle could do is leave behind
        an artifact that looks like it closed a position it did not.
        """
        if self.dry_run and self.orders_submitted:
            raise ValueError(
                f"a dry run reported {self.orders_submitted} submitted order(s); a dry run "
                f"that reached a broker is a bug, not a diagnostic"
            )
        if not self.execution_authorized:
            if self.orders_submitted:
                raise ValueError(
                    f"an unauthorised exit run reported {self.orders_submitted} submitted "
                    f"order(s). Evaluating whether a position should close never closes one"
                )
            if self.exit_execution_ids:
                raise ValueError(
                    f"an unauthorised exit run created exit executions "
                    f"{self.exit_execution_ids}; evaluation and execution are separate acts"
                )
        return self

    @property
    def positions(self) -> list[str]:
        return sorted({decision.position_id for decision in self.decisions})

    @property
    def exits(self) -> list[ExitDecisionRecord]:
        return [d for d in self.decisions if d.decision is ExitDecisionType.EXIT]

    @property
    def blocks(self) -> list[ExitDecisionRecord]:
        return [d for d in self.decisions if d.decision is ExitDecisionType.BLOCK]
