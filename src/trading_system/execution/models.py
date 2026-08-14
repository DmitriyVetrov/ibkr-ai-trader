"""Canonical execution artifacts.

Milestone 8's boundary is narrow on purpose. Everything this package *reads* —
the authorisation, its legs, its quantity, its money — is Milestone 7's, and
nothing here may restate it differently. What this package *adds* is the record
of one submission attempt and what the broker said about it.

Four artifacts:

:class:`ExecutionRequest`
    A deliberate instruction to submit. Distinct from the allocation on
    purpose: an ``allocation_id`` is a thing that exists, not a decision to act
    on it, and the two must not be the same object or the second would be
    implied by the first.
:class:`ExecutionRecord`
    One submission attempt: exactly which contracts, at what price, in what
    mode, and everything the broker reported back. Written once; later broker
    news arrives as an event and produces a *new* record rather than editing
    the stored one.
:class:`ExecutionEvent`
    One observation, appended. The history is the truth and the record is a
    fold of it, so an intermediate state is never silently overwritten by a
    later one.
:class:`ExecutionRunResult`
    The record of one ``execution run``: what it was asked to do, what it
    submitted, and what it refused.

What is deliberately absent, with tests that fail loudly:

* **No sizing.** No field recomputes a quantity, a capital commitment or a
  maximum loss. Every one of them is copied from the authorisation, and a
  validator refuses a record whose figures disagree with it.
* **No AI.** No model name, no prompt, no rationale. Execution translates an
  approved deterministic artifact into an order; there is nothing here for a
  model to decide.
* **No inferred fill.** ``filled_quantity`` starts at zero and only an
  execution report moves it. Nothing derives it from the submitted quantity.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    LIVE_EXECUTION_STATES,
    ExecutionEventType,
    ExecutionIntent,
    ExecutionReasonCode,
    ExecutionRunStatus,
    ExecutionState,
    LegAction,
    OptionRight,
    OrderStatus,
    OrderType,
    StrategyType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import (
    ExecutionResult,
    Fill,
    Identifier,
    ImmutableModel,
    Money,
    SystemVersions,
    Ticker,
    UtcDatetime,
)

__all__ = [
    "EXECUTION_SCHEMA_VERSION",
    "ExecutionEvent",
    "ExecutionLeg",
    "ExecutionRecord",
    "ExecutionRequest",
    "ExecutionRunCounts",
    "ExecutionRunResult",
    "execution_identifier",
    "execution_request_identifier",
    "execution_run_identifier",
    "exit_execution_request_identifier",
]

#: Bumped when a stored execution artifact changes shape. Folded into every
#: derived identifier, so records written under different shapes cannot collide.
EXECUTION_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def execution_request_identifier(
    *,
    allocation_id: str,
    trading_mode: TradingMode,
    order_type: OrderType,
    time_in_force: TimeInForce,
    policy_version: str,
    schema_version: str = EXECUTION_SCHEMA_VERSION,
) -> str:
    """Derive the identity of *this trade, executed this way*.

    This is what makes submission idempotent, and every term in it is load
    bearing. The allocation says which trade; the mode, order type and
    time-in-force say what would actually be sent, so a genuinely different
    order is a different identity rather than a silent second copy of the same
    one. The policy version is included because a changed execution policy
    produces a different order from identical inputs.

    Deliberately excludes the clock. An identity that changed with time would
    make every retry a new request, which is precisely the duplicate-order bug
    this exists to prevent — a client that timed out and tried again would
    submit a second order and see nothing wrong with it.
    """
    digest = stable_hash(
        [
            "EXECUTION_REQUEST",
            schema_version,
            allocation_id,
            trading_mode.value,
            order_type.value,
            time_in_force.value,
            policy_version,
        ]
    )
    return f"exec-req-{digest[:20]}"


def exit_execution_request_identifier(
    *,
    position_id: str,
    entry_execution_id: str,
    trading_mode: TradingMode,
    order_type: OrderType,
    time_in_force: TimeInForce,
    policy_version: str,
    schema_version: str = EXECUTION_SCHEMA_VERSION,
) -> str:
    """Derive the identity of *closing this position, this way* (Milestone 10).

    A separate function rather than an extra term on
    :func:`execution_request_identifier`, deliberately. Adding a discriminator
    there would change every identity that function has ever produced, and
    those identities are what stop a stored entry from being submitted twice.
    The two forms cannot collide because their prefixes differ — ``exec-req-``
    against ``exit-req-`` — which is the same technique the position ledger
    uses to keep ``cid:`` and ``sym:`` keys apart.

    Excludes the clock and the exit *reason*, for the same reason the entry
    side excludes the clock: an exit triggered by a trailing stop and the same
    exit triggered a minute later by the expiration policy are one order for
    one position, and giving them different identities would let the second one
    send a second order.
    """
    digest = stable_hash(
        [
            "EXIT_EXECUTION_REQUEST",
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


def execution_identifier(
    *,
    execution_request_id: str,
    attempt: int,
    schema_version: str = EXECUTION_SCHEMA_VERSION,
) -> str:
    """Derive one execution attempt's identity.

    An execution *record* is one attempt; the *request* is the trade. They are
    separate so a refused attempt (validation failed, market closed) can be
    stored honestly without consuming the identity of the trade itself — and so
    that a second attempt against a request that already reached the broker is
    recognisable as exactly that rather than filed as unrelated.
    """
    digest = stable_hash(["EXECUTION", schema_version, execution_request_id, attempt])
    return f"execution-{digest[:20]}"


def execution_run_identifier(
    *,
    as_of: datetime,
    allocation_run_id: str | None,
    execution_request_ids: list[str],
    config_version: str,
    policy_version: str,
    trading_mode: TradingMode,
    dry_run: bool,
    ledger_state: list[str] | None = None,
    schema_version: str = EXECUTION_SCHEMA_VERSION,
) -> str:
    """Derive one run's identity from what it was asked to do.

    ``ledger_state`` is what makes this correct rather than merely unique. The
    same authorisations executed against a ledger that has since recorded a
    submission are a *different* decision reaching a different answer — the
    second run refuses where the first sent. Deriving the id from the inputs
    alone would collide the two, and the immutable store would refuse to write
    the second. An unchanged re-run still lands on the same id, which is what
    makes it idempotent rather than a second record of one event.

    This is the same lesson the allocation ledger records about including the
    campaign's committed state, for the same reason.
    """
    digest = stable_hash(
        [
            "EXECUTION_RUN",
            schema_version,
            as_of.isoformat(),
            allocation_run_id or "",
            sorted(execution_request_ids),
            sorted(ledger_state or []),
            config_version,
            policy_version,
            trading_mode.value,
            dry_run,
        ]
    )
    return f"execrun-{digest[:20]}"


# ---------------------------------------------------------------------------
# The authorisation to submit
# ---------------------------------------------------------------------------
class ExecutionRequest(ImmutableModel):
    """A deliberate instruction to submit one approved authorisation.

    ``execution_authorized`` is required to be ``True`` and is not defaulted:
    a request that merely names an allocation cannot be constructed, so there
    is no shape in which "load this allocation" and "send this order" are the
    same call. That is the entire reason this model exists.
    """

    execution_request_id: Identifier
    allocation_id: Identifier
    campaign_id: Identifier
    opportunity_id: Identifier
    requested_at: UtcDatetime
    schema_version: Identifier = EXECUTION_SCHEMA_VERSION

    #: Must be True. A False value is a construction error rather than a
    #: request that will be politely declined later, because a request object
    #: that can exist unauthorised is one a caller can forget to check.
    execution_authorized: bool
    #: Who or what asked. Recorded, never trusted for permission.
    requested_by: Identifier = "cli"

    order_type: OrderType
    time_in_force: TimeInForce
    trading_mode: TradingMode
    #: A dry run builds and validates everything and submits nothing.
    dry_run: bool = False

    policy_version: Identifier
    versions: SystemVersions

    @model_validator(mode="after")
    def _authorisation_is_not_optional(self) -> ExecutionRequest:
        if not self.execution_authorized:
            raise ValueError(
                "an ExecutionRequest must carry execution_authorized=True. An allocation id "
                "alone is not permission to send an order; build no request rather than an "
                "unauthorised one"
            )
        return self


# ---------------------------------------------------------------------------
# What was actually sent
# ---------------------------------------------------------------------------
class ExecutionLeg(ImmutableModel):
    """One contract as submitted, copied from the authorisation.

    ``contract_id`` is required and is never derived from symbol, strike and
    expiration. Milestone 6 resolved a specific contract against a specific
    chain; re-deriving one here would be selecting a contract at execution
    time, which is precisely what the architecture forbids — and the failure
    mode is an order for something nobody chose.
    """

    leg_index: int = Field(ge=0)
    contract_id: int = Field(gt=0)
    action: LegAction
    right: OptionRight
    underlying: Ticker
    expiration: date
    strike: Money = Field(gt=0)
    #: Never assumed. A standard US equity option is 100, and a system that
    #: assumed so would misprice the first one that is not.
    multiplier: int = Field(gt=0)
    ratio: int = Field(default=1, ge=1)
    trading_class: str = Field(min_length=1)
    exchange: str | None = None
    local_symbol: str | None = None
    currency: str | None = None


class ExecutionRecord(ImmutableModel):
    """One submission attempt and everything the broker said about it.

    Immutable. Later broker news produces a new record through
    :meth:`with_event`, and the stored history is what the current record is
    folded from. Nothing here is edited in place: an execution that reported
    two fills must still be able to show that it once reported one.
    """

    execution_id: Identifier
    execution_request_id: Identifier
    allocation_id: Identifier
    purchase_card_id: Identifier
    risk_decision_id: Identifier
    order_intent_id: Identifier
    campaign_id: Identifier
    opportunity_id: Identifier
    schema_version: Identifier = EXECUTION_SCHEMA_VERSION

    created_at: UtcDatetime
    updated_at: UtcDatetime

    # --- what -------------------------------------------------------------
    underlying: Ticker
    strategy: StrategyType
    #: Whether this submission establishes a position or ends one. Added by
    #: Milestone 10 and defaulted to ``OPEN``, so every record written before
    #: it keeps its meaning. Three ledgers read this record and two treat the
    #: intents differently: the reservation ledger resolves committed capital
    #: against ``OPEN`` executions only, and the position ledger derives a
    #: logical strategy structure from ``OPEN`` executions only. The *fills* of
    #: a ``CLOSE`` still net into the expected-position arithmetic, which is
    #: precisely how a position becomes closed.
    intent: ExecutionIntent = ExecutionIntent.OPEN
    legs: list[ExecutionLeg] = Field(default_factory=list)
    #: Units of the *structure*, exactly as authorised. Never recomputed.
    quantity: int = Field(ge=0)
    multiplier: int = Field(default=0, ge=0)
    order_type: OrderType
    time_in_force: TimeInForce

    # --- money: several different figures, never conflated -----------------
    #
    # The authorisation's reference price, the price we asked for, and what
    # actually traded are three separate facts. Overwriting any of them with
    # another destroys the only evidence of slippage there will ever be.
    #
    # Two *units* are in play and mixing them is a factor-of-100 error, so they
    # are named apart rather than distinguished by comment:
    #
    #   reference_price  — money for one whole unit of the structure, exactly
    #                      as Milestone 7 authorised it (multiplier included);
    #   reference_quote  — the same figure in the broker's quoted terms, which
    #                      is what a limit price and a fill price are in;
    #   submitted_price  — the limit actually sent, in quoted terms;
    #   average/last_fill_price — the broker's own numbers, quoted terms,
    #                      recorded exactly as reported.
    #
    # Everything after the first is comparable to everything else after it,
    # which is what makes slippage a subtraction rather than a conversion.
    #: Cost of one unit of the structure as Milestone 7 recorded it. Never
    #: overwritten by anything the broker says.
    reference_price: Money | None = Field(default=None, ge=0)
    #: ``reference_price`` restated in quoted terms, for comparison with fills.
    reference_quote: Money | None = Field(default=None, ge=0)
    #: The limit price sent, in the broker's quoted terms. None for a market
    #: order, which this milestone does not permit.
    submitted_price: Money | None = Field(default=None, gt=0)
    #: Capital Milestone 7 authorised. Never overwritten by what was spent.
    capital_commitment: Money = Field(default=Decimal("0"), ge=0)
    maximum_loss: Money = Field(default=Decimal("0"), ge=0)
    currency: str | None = None

    # --- mode --------------------------------------------------------------
    trading_mode: TradingMode
    dry_run: bool = False
    broker: Identifier

    # --- broker reality ----------------------------------------------------
    #
    # Every field below is nullable and stays nullable. A broker that reported
    # nothing reported nothing; a zero would be a claim we are not entitled to
    # make (specification section 18).
    state: ExecutionState
    broker_order_id: str | None = None
    broker_status: OrderStatus | None = None
    filled_quantity: int = Field(default=0, ge=0)
    remaining_quantity: int | None = Field(default=None, ge=0)
    average_fill_price: Money | None = Field(default=None, gt=0)
    last_fill_price: Money | None = Field(default=None, gt=0)
    fills: list[Fill] = Field(default_factory=list)
    broker_message: str | None = None

    submitted_at: UtcDatetime | None = None
    acknowledged_at: UtcDatetime | None = None
    filled_at: UtcDatetime | None = None
    #: When the broker itself says it acted, where it tells us. Kept apart from
    #: our own observation clocks so provenance survives.
    broker_timestamp: UtcDatetime | None = None

    #: Read off the broker, never asserted. A record claiming zero while the
    #: broker counted one would be the most dangerous line in the system.
    orders_submitted: int = Field(default=0, ge=0)

    # --- why ---------------------------------------------------------------
    reason_codes: list[ExecutionReasonCode] = Field(default_factory=list)
    failure_reason: str | None = None

    # --- provenance: ids, never copies -------------------------------------
    allocation_run_id: Identifier | None = None
    contract_selection_id: Identifier | None = None
    strategy_decision_id: Identifier | None = None
    research_report_id: Identifier | None = None
    account_snapshot_id: Identifier | None = None
    #: Milestone 10 provenance, set only on a ``CLOSE``. Ids, never copies:
    #: what the exit decided and why lives in the exit ledger, and an execution
    #: record that restated it would be a second, divergent account of one
    #: judgement.
    position_id: Identifier | None = None
    exit_decision_id: Identifier | None = None
    exit_reason: str | None = None
    entry_execution_id: Identifier | None = None
    policy_version: Identifier
    versions: SystemVersions

    @model_validator(mode="after")
    def _a_close_names_what_it_is_closing(self) -> ExecutionRecord:
        """An exit must say which position it ends.

        Without it a closing order is indistinguishable from an opening one
        that happens to sell, and the reservation ledger, the position ledger
        and reconciliation would each have to guess.
        """
        if self.intent is ExecutionIntent.CLOSE and not self.position_id:
            raise ValueError(
                "a CLOSE execution must name the position_id it is closing; an exit that "
                "cannot say what it is exiting cannot be reconciled against one"
            )
        if self.intent is ExecutionIntent.OPEN and self.exit_decision_id is not None:
            raise ValueError(
                "an OPEN execution cannot carry an exit decision; establishing a position and "
                "ending one are different acts and must not share a record shape"
            )
        return self

    @model_validator(mode="after")
    def _a_close_commits_no_new_capital(self) -> ExecutionRecord:
        """An exit returns capital; it never commits more.

        Enforced rather than trusted, because a closing record that carried a
        capital commitment would be counted by the campaign ledger as a second
        authorisation against the same money.
        """
        if self.intent is ExecutionIntent.CLOSE:
            if self.capital_commitment:
                raise ValueError(
                    f"a CLOSE execution reports a capital commitment of "
                    f"{self.capital_commitment}. Closing a position commits no new capital, "
                    f"and recording one would authorise the same money twice"
                )
            if self.maximum_loss:
                raise ValueError(
                    f"a CLOSE execution reports a maximum loss of {self.maximum_loss}. Ending "
                    f"a position adds no new risk; the risk it removes is the position's own"
                )
        return self

    @model_validator(mode="after")
    def _a_fill_never_exceeds_what_was_submitted(self) -> ExecutionRecord:
        if self.quantity and self.filled_quantity > self.quantity:
            raise ValueError(
                f"filled_quantity {self.filled_quantity} exceeds the submitted quantity "
                f"{self.quantity}; a broker cannot fill more than was sent, so this is a "
                f"tracking bug rather than a market event"
            )
        if (
            self.remaining_quantity is not None
            and self.quantity
            and self.filled_quantity + self.remaining_quantity > self.quantity
        ):
            raise ValueError(
                f"filled {self.filled_quantity} + remaining {self.remaining_quantity} exceeds "
                f"the submitted quantity {self.quantity}"
            )
        return self

    @model_validator(mode="after")
    def _a_terminal_fill_is_complete(self) -> ExecutionRecord:
        if self.state is ExecutionState.FILLED and self.filled_quantity != self.quantity:
            raise ValueError(
                f"FILLED claims the whole structure traded, but {self.filled_quantity} of "
                f"{self.quantity} units filled. A partially filled order is PARTIALLY_FILLED; "
                f"reporting it as FILLED would claim a position that does not exist"
            )
        if self.state is ExecutionState.PARTIALLY_FILLED and not 0 < self.filled_quantity < max(
            self.quantity, 1
        ):
            raise ValueError(
                f"PARTIALLY_FILLED requires a fill strictly between nothing and everything, "
                f"got {self.filled_quantity} of {self.quantity}"
            )
        return self

    @model_validator(mode="after")
    def _nothing_is_claimed_without_a_broker(self) -> ExecutionRecord:
        """A dry run submits nothing, and says so in every field.

        Enforced here rather than trusted to the caller: the single most
        damaging thing a diagnostic could do is leave behind a record that
        looks like a real order.
        """
        if self.dry_run:
            if self.orders_submitted:
                raise ValueError("a dry run must submit no orders")
            if self.broker_order_id is not None:
                raise ValueError("a dry run cannot carry a broker order id")
            if self.filled_quantity:
                raise ValueError("a dry run cannot report a fill")
            if self.state in LIVE_EXECUTION_STATES:
                raise ValueError(
                    f"a dry run cannot be in {self.state.value}: nothing was sent, so no order "
                    f"can be in flight"
                )
        if self.state is ExecutionState.SUBMITTED and self.broker_order_id is None:
            raise ValueError(
                "SUBMITTED without a broker order id is not a tracked order. Use UNKNOWN: an "
                "order we cannot name is one we cannot cancel or reconcile"
            )
        return self

    @model_validator(mode="after")
    def _a_refusal_names_a_reason(self) -> ExecutionRecord:
        if self.state in (ExecutionState.REJECTED, ExecutionState.FAILED) and not self.reason_codes:
            raise ValueError(f"a {self.state.value} execution must carry at least one reason code")
        return self

    # --- derived views -----------------------------------------------------
    @property
    def submitted(self) -> bool:
        """Whether an order may exist at the broker.

        Includes ``SUBMISSION_PENDING`` and ``UNKNOWN`` deliberately: both mean
        *we may have sent one*, and the absence of an acknowledgement is not
        evidence that nothing was sent.
        """
        return self.state in LIVE_EXECUTION_STATES

    @property
    def is_terminal(self) -> bool:
        from trading_system.execution.state_machine import is_terminal

        return is_terminal(self.state)

    @property
    def submitted_notional(self) -> Decimal | None:
        """What the order asked to spend, if it had filled entirely at its limit.

        Distinct from ``capital_commitment``: that is what was *authorised*.
        Where a limit price sits below the reference price the two differ, and
        both are true statements about different things.
        """
        if self.submitted_price is None:
            return None
        return self.submitted_price * Decimal(self.quantity)

    @property
    def executed_capital(self) -> Decimal | None:
        """What actually traded. ``None`` until something does."""
        if self.average_fill_price is None or not self.filled_quantity:
            return None
        return self.average_fill_price * Decimal(self.filled_quantity)

    def with_event(self, event: ExecutionEvent) -> ExecutionRecord:
        """Fold one observation onto this record, returning a new one.

        The state transition is validated, so an event that would move the
        record along an illegal edge raises and changes nothing. Broker fields
        are only ever *overwritten by a broker observation* — an event that
        carries no opinion about the fill leaves the fill alone rather than
        resetting it to zero.
        """
        from trading_system.execution.state_machine import validate_transition

        if event.execution_id != self.execution_id:
            raise ValueError(
                f"event {event.event_id} belongs to execution {event.execution_id}, not "
                f"{self.execution_id}"
            )
        if event.state is not self.state:
            validate_transition(self.state, event.state)

        updates: dict[str, object] = {
            "state": event.state,
            "updated_at": event.observed_at,
        }
        if event.broker_order_id is not None:
            updates["broker_order_id"] = event.broker_order_id
        if event.broker_status is not None:
            updates["broker_status"] = event.broker_status
        if event.filled_quantity is not None:
            updates["filled_quantity"] = event.filled_quantity
        if event.remaining_quantity is not None:
            updates["remaining_quantity"] = event.remaining_quantity
        if event.average_fill_price is not None:
            updates["average_fill_price"] = event.average_fill_price
        if event.last_fill_price is not None:
            updates["last_fill_price"] = event.last_fill_price
        if event.broker_message is not None:
            updates["broker_message"] = event.broker_message
        if event.broker_timestamp is not None:
            updates["broker_timestamp"] = event.broker_timestamp
        if event.fills:
            known = {fill.fill_id for fill in self.fills}
            merged = list(self.fills) + [f for f in event.fills if f.fill_id not in known]
            updates["fills"] = merged
        if event.reason_code is not None and event.reason_code not in self.reason_codes:
            updates["reason_codes"] = [*self.reason_codes, event.reason_code]
        if event.detail and event.state in (ExecutionState.REJECTED, ExecutionState.FAILED):
            updates["failure_reason"] = event.detail
        if event.event_type is ExecutionEventType.EXECUTION_SUBMITTED:
            updates.setdefault("submitted_at", self.submitted_at or event.observed_at)
            updates["acknowledged_at"] = event.broker_timestamp or event.observed_at
        if event.state is ExecutionState.FILLED:
            updates["filled_at"] = event.broker_timestamp or event.observed_at

        return self.model_copy(update=updates)

    def to_execution_result(self) -> ExecutionResult:
        """Project onto the Milestone 1 workflow boundary.

        The full record is the audit artifact; this is the narrow contract the
        rest of the system was built against (``schemas/execution_result.json``),
        exactly as research and strategy project onto their Milestone 1 shapes.

        Raises for a state the Milestone 1 vocabulary cannot express. ``UNKNOWN``
        is the important one: :class:`OrderStatus` has no member for "we do not
        know", and mapping it onto ``PENDING_SUBMIT`` would turn an ambiguous
        submission into a tidy claim that nothing was sent.
        """
        status = _M1_STATUS.get(self.state)
        if status is None:
            raise ValueError(
                f"execution state {self.state.value} has no Milestone 1 OrderStatus. "
                f"An ambiguous or unsent submission must not be projected as a definite one"
            )
        return ExecutionResult(
            intent_id=self.order_intent_id,
            broker=self.broker,
            broker_order_id=self.broker_order_id,
            status=status,
            orders_submitted=self.orders_submitted,
            filled_quantity=self.filled_quantity,
            average_fill_price=self.average_fill_price,
            fills=list(self.fills),
            submitted_at=self.submitted_at,
            last_update_at=self.updated_at,
            message=self.broker_message or self.failure_reason,
            trading_mode=self.trading_mode,
        )


#: Execution states the Milestone 1 ``OrderStatus`` vocabulary can express.
#: ``UNKNOWN``, ``CREATED`` and ``VALIDATED`` are deliberately absent — the
#: boundary has no honest member for them.
_M1_STATUS: dict[ExecutionState, OrderStatus] = {
    ExecutionState.SUBMISSION_PENDING: OrderStatus.PENDING_SUBMIT,
    ExecutionState.SUBMITTED: OrderStatus.SUBMITTED,
    ExecutionState.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    ExecutionState.FILLED: OrderStatus.FILLED,
    ExecutionState.CANCEL_PENDING: OrderStatus.SUBMITTED,
    ExecutionState.CANCELLED: OrderStatus.CANCELLED,
    ExecutionState.REJECTED: OrderStatus.REJECTED,
}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class ExecutionEvent(ImmutableModel):
    """One appended observation about an execution.

    ``occurred_at`` is when the thing happened, ``observed_at`` when we learned
    of it, and where the broker stamps its own clock that is kept too. Three
    timestamps because they genuinely differ, and collapsing them would lose
    the only means of telling a slow feed from a slow fill.
    """

    event_id: Identifier
    execution_id: Identifier
    #: Monotonic within one execution, so ordering survives equal timestamps.
    sequence: int = Field(ge=0)
    event_type: ExecutionEventType
    #: The state the execution is in *after* this event.
    state: ExecutionState
    occurred_at: UtcDatetime
    observed_at: UtcDatetime
    source: Identifier
    schema_version: Identifier = EXECUTION_SCHEMA_VERSION

    reason_code: ExecutionReasonCode | None = None
    detail: str | None = None

    # --- what the broker said, if anything ---------------------------------
    broker_order_id: str | None = None
    broker_status: OrderStatus | None = None
    filled_quantity: int | None = Field(default=None, ge=0)
    remaining_quantity: int | None = Field(default=None, ge=0)
    average_fill_price: Money | None = Field(default=None, gt=0)
    last_fill_price: Money | None = Field(default=None, gt=0)
    fills: list[Fill] = Field(default_factory=list)
    broker_message: str | None = None
    broker_timestamp: UtcDatetime | None = None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
class ExecutionRunCounts(ImmutableModel):
    """How many authorisations reached each outcome."""

    considered: int = Field(default=0, ge=0)
    submitted: int = Field(default=0, ge=0)
    refused: int = Field(default=0, ge=0)
    already_submitted: int = Field(default=0, ge=0)
    uncertain: int = Field(default=0, ge=0)


class ExecutionRunResult(ImmutableModel):
    """The immutable record of one execution run."""

    run_id: Identifier
    campaign_id: Identifier
    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: ExecutionRunStatus
    schema_version: Identifier = EXECUTION_SCHEMA_VERSION

    trading_mode: TradingMode
    dry_run: bool = False
    broker: Identifier
    policy_version: Identifier

    allocation_run_id: Identifier | None = None
    executions: list[ExecutionRecord] = Field(default_factory=list)
    counts: ExecutionRunCounts = Field(default_factory=ExecutionRunCounts)

    #: Read off the broker after the run, never accumulated by this code.
    orders_submitted: int = Field(default=0, ge=0)
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _a_dry_run_submits_nothing(self) -> ExecutionRunResult:
        """A dry run may report any honest outcome except one that claims a trade.

        Deliberately not "the status must be DRY_RUN". A dry run that found no
        allocation run has something specific to say, and forcing it to say
        ``DRY_RUN`` would replace a diagnosis with a label. What it must never
        do is claim a submission, so ``SUCCESS`` and ``PARTIAL`` — the two
        statuses that assert an order reached a broker — are refused, along
        with any non-zero count or any execution believed live.
        """
        if not self.dry_run:
            return self
        if self.orders_submitted:
            raise ValueError(
                f"a dry run reported {self.orders_submitted} submitted order(s); a dry run "
                f"that reached a broker is a bug, not a diagnostic"
            )
        if self.status in (ExecutionRunStatus.SUCCESS, ExecutionRunStatus.PARTIAL):
            raise ValueError(
                f"a dry run cannot be recorded as {self.status.value}: that status asserts an "
                f"order reached a broker, and a dry run sends nothing"
            )
        live = [e.execution_id for e in self.executions if e.submitted]
        if live:
            raise ValueError(f"a dry run left executions believed live at the broker: {live}")
        return self

    @property
    def symbols(self) -> list[str]:
        return sorted({execution.underlying for execution in self.executions})

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionRunStatus.SUCCESS
