"""Artifacts for the controlled closure of pre-existing broker holdings.

Reconciliation reports an ``ORPHAN_BROKER_POSITION`` and never adopts it: the
broker holds something no execution of ours accounts for, its acquisition
provenance is ``UNKNOWN``, and it stays ``UNKNOWN``. That is correct, and it
leaves an operator with one question this package answers:

    **may we deliberately close this holding, and what exactly happened?**

Three artifacts, in the order they come into existence:

:class:`CleanupTarget`
    One broker-observed holding, identified by the broker's own contract id and
    described entirely from the snapshot. It carries no strategy, no
    allocation, no thesis and no acquisition story, because none exists.
:class:`OrphanCleanupRequest`
    The explicit, immutable target set an operator authorised, naming the
    reconciliation that identified the orphans. There is no "close everything"
    shape: the model *is* a list of specific contracts.
:class:`OrphanCleanupRun`
    What happened. One outcome per target — the gates, the order, the broker
    order id, the fills, the quantity the broker held afterwards — plus the
    reconciliation that observed the result.

What is deliberately absent, with tests that fail loudly:

* **No adoption.** No field anywhere here names an allocation, a purchase card,
  a risk decision, an opportunity, a strategy or a research report, and the
  execution record a cleanup produces *refuses* to carry one.
* **No money of the campaign's.** No capital commitment, no reservation, no
  budget figure and no realised profit and loss. This system did not buy the
  holding; attributing its cost or its proceeds to a campaign would invent the
  one number nobody could ever check.
* **No corrective judgement.** Nothing here decides that a holding *should* be
  closed. An operator decided; this records what they decided and what came of
  it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum, unique

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    ExecutionReasonCode,
    ExecutionState,
    OptionRight,
    SecurityType,
    TradingMode,
)
from trading_system.domain.models import (
    Identifier,
    ImmutableModel,
    Money,
    SystemVersions,
    Ticker,
    UtcDatetime,
)

__all__ = [
    "CLEANUP_SCHEMA_VERSION",
    "CLEANUP_TERMINAL_STATUSES",
    "CleanupOutcome",
    "CleanupOutcomeStatus",
    "CleanupRunStatus",
    "CleanupTarget",
    "OrphanCleanupRequest",
    "OrphanCleanupRun",
    "cleanup_request_identifier",
    "cleanup_run_identifier",
]

#: Bumped when a stored cleanup artifact changes shape. Folded into every
#: derived identifier, so records written under different shapes cannot collide.
CLEANUP_SCHEMA_VERSION = "1.0.0"

#: Why this holding is being closed. One value, and it is not a placeholder: it
#: is the *only* reason this operation exists, and a second one would be a
#: second policy nobody wrote down.
CLEANUP_REASON = "PRE_EXISTING_ORPHAN_POSITION"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
@unique
class CleanupOutcomeStatus(StrEnum):
    """What became of one target.

    Six members, and the distinctions between them are the point. In
    particular ``ALREADY_CLOSED`` is not a success and ``UNCERTAIN`` is not a
    failure: the first means nothing needed doing, and the second means an
    order may be live at the broker right now.
    """

    #: The broker confirmed the whole holding traded and reports none of it.
    CLOSED = "CLOSED"
    #: Some of it traded. The remainder is reported and nothing further is sent.
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    #: An order is working at the broker; the holding is still there for now.
    WORKING = "WORKING"
    #: The broker no longer reports the holding and this run sent nothing. The
    #: ordinary answer on a second invocation.
    ALREADY_CLOSED = "ALREADY_CLOSED"
    #: A gate refused, or our own validation did. **Nothing left this process**,
    #: so the broker's submitted-order counter did not move either.
    REFUSED = "REFUSED"
    #: The broker refused the order. Deliberately not ``REFUSED``: the attempt
    #: reached the broker and the broker's own counter records it, which is a
    #: different fact from a gate that stopped the order here. Nothing is
    #: working as a result, and nothing is re-sent.
    REJECTED = "REJECTED"
    #: Something was sent and the outcome was never learned. Not a failure, and
    #: emphatically not a reason to send another: resolve it by observing the
    #: broker.
    UNCERTAIN = "UNCERTAIN"


@unique
class CleanupRunStatus(StrEnum):
    """The run as a whole."""

    #: Every target ended CLOSED or ALREADY_CLOSED.
    COMPLETE = "COMPLETE"
    #: Some targets are done and some are not. Each outcome says which.
    PARTIAL = "PARTIAL"
    #: Nothing was submitted. Either a gate refused, or this was a review.
    NOTHING_SUBMITTED = "NOTHING_SUBMITTED"
    #: At least one submission's outcome is unknown. Read this before anything
    #: else in the run: an order may be live.
    UNCERTAIN = "UNCERTAIN"
    #: Built and evaluated everything, contacted no broker, sent nothing.
    DRY_RUN = "DRY_RUN"
    #: No orphan holding was found to target at all.
    NO_TARGETS = "NO_TARGETS"


#: Outcomes after which this target needs no further action.
CLEANUP_TERMINAL_STATUSES = frozenset(
    {CleanupOutcomeStatus.CLOSED, CleanupOutcomeStatus.ALREADY_CLOSED}
)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def cleanup_request_identifier(
    *,
    account_reference: str,
    reconciliation_id: str,
    contract_keys: list[str],
    trading_mode: TradingMode,
    policy_version: str,
    schema_version: str = CLEANUP_SCHEMA_VERSION,
) -> str:
    """Derive a cleanup request's identity from exactly what it authorises.

    The contract keys are sorted, so the same four holdings named in a
    different order are the same request rather than two. The reconciliation is
    included because the same holdings identified by a *different* observation
    are a different authorisation: the operator reviewed a specific report.
    """
    digest = stable_hash(
        [
            "ORPHAN_CLEANUP_REQUEST",
            schema_version,
            account_reference,
            reconciliation_id,
            sorted(contract_keys),
            trading_mode.value,
            policy_version,
        ]
    )
    return f"cleanup-req-{digest[:20]}"


def cleanup_run_identifier(
    *,
    request_id: str,
    as_of: datetime,
    outcomes: list[str],
    dry_run: bool,
    schema_version: str = CLEANUP_SCHEMA_VERSION,
) -> str:
    """Derive a run's identity from its inputs **and what it concluded**.

    ``outcomes`` is load bearing, and this repository has learned the same
    lesson four times before — in allocation, in execution, in profit and loss
    and in the scheduler. Two runs over the same request reaching different
    answers are different facts: the first sold four holdings, the second found
    them already gone and sent nothing. An id derived from the inputs alone
    collides them, and the immutable store correctly refuses to write the
    second.
    """
    digest = stable_hash(
        [
            "ORPHAN_CLEANUP_RUN",
            schema_version,
            request_id,
            as_of.isoformat(),
            sorted(outcomes),
            dry_run,
        ]
    )
    return f"cleanuprun-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}"


# ---------------------------------------------------------------------------
# What is being closed
# ---------------------------------------------------------------------------
class CleanupTarget(ImmutableModel):
    """One pre-existing broker holding, as the broker described it.

    Every field is copied from a :class:`~trading_system.positions.models.
    ObservedPosition`. Nothing is completed, corrected or derived — in
    particular ``trading_class`` stays ``None`` when the broker did not report
    one, because it cannot be derived from the symbol (real SPY validation
    returned a ``2SPY`` trading class for an ``SPY`` position).

    ``key`` is the broker's own contract identity (``cid:848575117``). A target
    identified any other way is refused outright: adjusted contracts share
    symbol, strike, expiry and right, so a symbol is not an identity and a
    cleanup addressed by one could sell the wrong contract.
    """

    key: Identifier
    contract_id: int = Field(gt=0)
    position_id: Identifier
    account_reference: Identifier

    underlying: Ticker
    symbol: Identifier
    asset_class: SecurityType
    expiration: date | None = None
    strike: Money | None = Field(default=None, gt=0)
    right: OptionRight | None = None
    multiplier: int | None = Field(default=None, ge=1)
    trading_class: str | None = None
    exchange: str | None = None
    local_symbol: str | None = None
    currency: str | None = None

    #: Signed, exactly as the broker reports it. Positive is long. A negative
    #: value is preserved rather than refused here, so the refusal can name the
    #: real number; the gates and the order builder both stop it.
    quantity: Money
    #: Money for one contract with the multiplier in it, as the broker reports
    #: it. Recorded for the audit trail and **never** used as a price, never
    #: attributed to the campaign, and never turned into a profit or loss.
    average_cost: Money | None = None
    #: The broker's own price for the holding, in *quoted* terms (48.77, not
    #: 4877.46). The reference the limit price is derived from.
    market_price: Money | None = None
    market_value: Money | None = None

    #: Which reconciliation finding identified this holding as an orphan.
    finding_id: Identifier
    reconciliation_id: Identifier

    broker_source: Identifier
    observed_at: UtcDatetime
    broker_timestamp: UtcDatetime | None = None
    simulated: bool = False

    @model_validator(mode="after")
    def _identity_comes_from_the_broker(self) -> CleanupTarget:
        if not self.key.startswith("cid:"):
            raise ValueError(
                f"cleanup target {self.key!r} is not identified by a broker contract id. A "
                f"symbol is not an identity — adjusted contracts share symbol, strike, expiry "
                f"and right — and an order addressed by one could sell the wrong contract"
            )
        if self.key != f"cid:{self.contract_id}":
            raise ValueError(
                f"cleanup target key {self.key!r} disagrees with contract id {self.contract_id}"
            )
        return self

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    def describe(self) -> str:
        parts = [self.symbol]
        if self.expiration is not None:
            parts.append(self.expiration.isoformat())
        if self.strike is not None:
            parts.append(str(self.strike))
        if self.right is not None:
            parts.append(self.right.value)
        return " ".join(parts)


# ---------------------------------------------------------------------------
# The authorisation
# ---------------------------------------------------------------------------
class OrphanCleanupRequest(ImmutableModel):
    """A deliberate instruction to close these specific pre-existing holdings.

    ``cleanup_authorized`` is required to be ``True`` and is not defaulted, for
    exactly the reason :class:`~trading_system.execution.models.
    ExecutionRequest` requires the same: a request object that can exist
    unauthorised is one a caller can forget to check.

    ``targets`` is a non-empty list of specific contracts. There is no field
    meaning "all", no predicate, no filter and no symbol pattern — the shape of
    this model is what makes "sell everything" inexpressible rather than merely
    unlikely.
    """

    cleanup_request_id: Identifier
    #: The observation that identified these as orphans. Not decoration: the
    #: gates re-check that it is recent and that it still describes reality.
    source_reconciliation_id: Identifier
    account_reference: Identifier
    campaign_id: Identifier
    requested_at: UtcDatetime
    schema_version: Identifier = CLEANUP_SCHEMA_VERSION

    targets: list[CleanupTarget] = Field(min_length=1)

    #: Must be True. A False value is a construction error, not a request that
    #: will be politely declined later.
    cleanup_authorized: bool
    #: Who or what asked. Recorded, never trusted for permission.
    requested_by: Identifier = "cli"
    reason: Identifier = CLEANUP_REASON

    trading_mode: TradingMode
    dry_run: bool = False
    policy_version: Identifier
    versions: SystemVersions

    @model_validator(mode="after")
    def _authorisation_is_not_optional(self) -> OrphanCleanupRequest:
        if not self.cleanup_authorized:
            raise ValueError(
                "an OrphanCleanupRequest must carry cleanup_authorized=True. Listing the orphan "
                "holdings in an account is not permission to sell out of them; build no request "
                "rather than an unauthorised one"
            )
        return self

    @model_validator(mode="after")
    def _each_holding_is_named_once(self) -> OrphanCleanupRequest:
        keys = [target.key for target in self.targets]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(
                f"cleanup request names {', '.join(duplicates)} more than once. Two orders for "
                f"one holding is how a long position becomes a short one"
            )
        return self

    @model_validator(mode="after")
    def _the_reason_is_the_only_one_there_is(self) -> OrphanCleanupRequest:
        if self.reason != CLEANUP_REASON:
            raise ValueError(
                f"cleanup reason {self.reason!r} is not {CLEANUP_REASON}. This operation exists "
                f"for exactly one purpose, and a second reason would be a second policy nobody "
                f"wrote down"
            )
        return self

    def target_for(self, key: str) -> CleanupTarget | None:
        return next((target for target in self.targets if target.key == key), None)

    @property
    def total_quantity(self) -> Decimal:
        return sum((target.quantity for target in self.targets), Decimal("0"))


# ---------------------------------------------------------------------------
# What happened
# ---------------------------------------------------------------------------
class CleanupOutcome(ImmutableModel):
    """What became of one target: the gates, the order, and broker reality.

    Two quantities are recorded and they are never conflated.
    ``filled_quantity`` is what the broker said traded; ``observed_quantity_after``
    is what the broker said it still holds when asked *afterwards*. Only the
    second can close a target, because a reported fill is a claim about an
    order and a position read is a claim about the account.
    """

    key: Identifier
    contract_id: int = Field(gt=0)
    symbol: Identifier
    describe: str
    status: CleanupOutcomeStatus
    schema_version: Identifier = CLEANUP_SCHEMA_VERSION

    #: What was held when the order was built, and what was asked for. Equal by
    #: construction — the builder refuses anything else — and both are recorded
    #: so the equality is checkable rather than asserted.
    observed_quantity_before: Money
    requested_quantity: int = Field(ge=0)

    # --- the order --------------------------------------------------------
    execution_id: Identifier | None = None
    execution_request_id: Identifier | None = None
    order_intent_id: Identifier | None = None
    broker_order_id: str | None = None
    execution_state: ExecutionState | None = None
    limit_price: Money | None = Field(default=None, gt=0)
    reference_quote: Money | None = Field(default=None, gt=0)
    filled_quantity: int = Field(default=0, ge=0)
    average_fill_price: Money | None = Field(default=None, gt=0)
    #: Read off the broker, never asserted.
    orders_submitted: int = Field(default=0, ge=0)

    # --- broker reality afterwards ----------------------------------------
    #: ``None`` means the broker was not re-read, which is a different fact
    #: from "it holds none". A target is never called CLOSED on a ``None``.
    observed_quantity_after: Money | None = None
    observed_after_at: UtcDatetime | None = None

    # --- why --------------------------------------------------------------
    reason_codes: list[ExecutionReasonCode] = Field(default_factory=list)
    gate_failures: list[str] = Field(default_factory=list)
    detail: str | None = None

    @model_validator(mode="after")
    def _nothing_is_called_closed_without_looking(self) -> CleanupOutcome:
        """``CLOSED`` is a claim about the account, not about an order.

        A filled order is what a broker said about a submission; a position of
        zero is what it said about the account. Only the second can end this,
        and a run that could not re-read the broker reports ``WORKING`` or
        ``UNCERTAIN`` rather than assuming the fill did what it said.
        """
        if self.status is CleanupOutcomeStatus.CLOSED:
            if self.observed_quantity_after is None:
                raise ValueError(
                    f"{self.key} is reported CLOSED without a broker observation afterwards. A "
                    f"reported fill is not a position read: only the broker can say the account "
                    f"holds none of it"
                )
            if self.observed_quantity_after != 0:
                raise ValueError(
                    f"{self.key} is reported CLOSED while the broker still holds "
                    f"{self.observed_quantity_after}"
                )
        if self.status is CleanupOutcomeStatus.ALREADY_CLOSED and self.orders_submitted:
            raise ValueError(
                f"{self.key} is reported ALREADY_CLOSED but this run submitted "
                f"{self.orders_submitted} order(s). Those are contradictory claims about the "
                f"same invocation"
            )
        if self.status is CleanupOutcomeStatus.REFUSED and self.orders_submitted:
            raise ValueError(
                f"{self.key} is reported REFUSED but {self.orders_submitted} order(s) were "
                f"submitted. REFUSED means nothing left this process; a broker that received "
                f"the order and turned it down is REJECTED, and one that never answered is "
                f"UNCERTAIN"
            )
        return self

    @model_validator(mode="after")
    def _a_refusal_names_a_reason(self) -> CleanupOutcome:
        if self.status in (
            CleanupOutcomeStatus.REFUSED,
            CleanupOutcomeStatus.REJECTED,
        ) and not (self.reason_codes or self.gate_failures or self.detail):
            raise ValueError(
                f"{self.key} is {self.status.value} but names no reason code, failed gate or detail"
            )
        return self

    @property
    def remaining_quantity(self) -> Decimal | None:
        return self.observed_quantity_after


class OrphanCleanupRun(ImmutableModel):
    """The immutable record of one cleanup invocation.

    Answers, for every target: what was targeted, why, which reconciliation
    identified it, who authorised it, when it was observed, what order was
    sent, what the broker called that order, what filled, what the broker held
    afterwards, and what the following reconciliation concluded.

    ``orders_submitted`` is read off the broker's own counter rather than
    counted here, and a dry run refuses a non-zero value outright.
    """

    run_id: Identifier
    cleanup_request_id: Identifier
    source_reconciliation_id: Identifier
    #: The reconciliation run *after* the cleanup. ``None`` when none was run —
    #: which is itself worth recording, because then nothing here has been
    #: independently confirmed.
    result_reconciliation_id: Identifier | None = None
    account_reference: Identifier
    campaign_id: Identifier
    schema_version: Identifier = CLEANUP_SCHEMA_VERSION

    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: CleanupRunStatus
    trading_mode: TradingMode
    dry_run: bool = False
    broker: Identifier
    requested_by: Identifier = "cli"
    reason: Identifier = CLEANUP_REASON

    outcomes: list[CleanupOutcome] = Field(default_factory=list)
    #: Every gate that was evaluated and what it decided, in order. Recorded
    #: whether or not it passed: a run that submitted nothing is exactly as
    #: worth auditing as one that did.
    gates: list[str] = Field(default_factory=list)

    #: Read off the broker.
    orders_submitted: int = Field(default=0, ge=0)
    #: Always zero, and validated as zero. A cleanup closes what is there; it
    #: never places a compensating, hedging or corrective trade.
    corrective_orders: int = Field(default=0, ge=0)

    #: The trace this run was recorded under, so the artifact and the telemetry
    #: can be correlated without either containing the other.
    trace_id: str | None = None

    policy_version: Identifier
    versions: SystemVersions
    detail: str | None = None

    @model_validator(mode="after")
    def _a_dry_run_submits_nothing(self) -> OrphanCleanupRun:
        if self.dry_run:
            if self.orders_submitted:
                raise ValueError("a dry-run cleanup must submit no orders")
            if any(outcome.orders_submitted for outcome in self.outcomes):
                raise ValueError("a dry-run cleanup outcome cannot report a submitted order")
            if any(outcome.broker_order_id for outcome in self.outcomes):
                raise ValueError("a dry-run cleanup cannot carry a broker order id")
        return self

    @model_validator(mode="after")
    def _nothing_corrective_is_ever_placed(self) -> OrphanCleanupRun:
        if self.corrective_orders:
            raise ValueError(
                f"a cleanup run reports {self.corrective_orders} corrective order(s). This "
                f"operation closes exactly the holdings an operator named; it never hedges, "
                f"offsets or compensates for one"
            )
        return self

    @model_validator(mode="after")
    def _the_count_matches_the_outcomes(self) -> OrphanCleanupRun:
        counted = sum(outcome.orders_submitted for outcome in self.outcomes)
        if counted != self.orders_submitted:
            raise ValueError(
                f"the run reports {self.orders_submitted} submitted order(s) but its outcomes "
                f"account for {counted}. The count is read off the broker and must reconcile "
                f"with the per-target records, or one of them is wrong about a real order"
            )
        return self

    @property
    def uncertain(self) -> int:
        return sum(
            1 for outcome in self.outcomes if outcome.status is CleanupOutcomeStatus.UNCERTAIN
        )

    @property
    def closed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status in CLEANUP_TERMINAL_STATUSES)

    @property
    def outstanding(self) -> tuple[CleanupOutcome, ...]:
        return tuple(
            outcome for outcome in self.outcomes if outcome.status not in CLEANUP_TERMINAL_STATUSES
        )
