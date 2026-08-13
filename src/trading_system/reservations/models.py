"""Canonical reservation artifacts: capital committed, and what became of it.

Milestone 7 authorises capital and cannot know whether the order ever filled,
so it treats every authorisation as spent — the conservative reading, and the
one that stops the same euro being authorised twice. This package is what
finally lets that capital move, and it moves it only on **proof**.

.. code-block:: text

    APPROVED CampaignAllocation (M7)
          |
    Reservation                RESERVED: committed, not invested
          |
    execution outcome          the only thing that may change it
          |
    CONSUMED / RELEASED / PARTIALLY_CONSUMED / UNKNOWN

Four rules are enforced by the shapes here rather than by discipline:

* **``RESERVED`` is not ``INVESTED``.** Committed capital and a held position
  are different facts, and only a confirmed fill turns one into the other.
* **``UNKNOWN`` is not ``RELEASED``.** An execution whose outcome was never
  learned may be a live order at the broker right now. Its capital stays
  locked, and :class:`ReservationState` has a distinct member so that "we are
  holding this because we do not know" can never be summarised as "reserved".
* **The accounting balances exactly.** ``consumed + released + remaining``
  equals what was authorised, in decimal, checked on every record. An identity
  that only holds most of the time is not an identity.
* **Consumed capital may exceed the authorisation only with evidence.** A
  broker correction is a distinct, recorded event; without one, the model
  refuses to be constructed.

Both the authorised amount and the actual executed amount are kept. Milestone 7
authorised a figure; the market filled at another. Overwriting the first with
the second would destroy the only evidence of the difference.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    COMMITTED_RESERVATION_STATES,
    ReservationEventType,
    ReservationReasonCode,
    ReservationState,
    StrategyType,
)
from trading_system.domain.models import (
    Identifier,
    ImmutableModel,
    Money,
    Ticker,
    UtcDatetime,
)

__all__ = [
    "RESERVATIONS_SCHEMA_VERSION",
    "CampaignCapital",
    "Reservation",
    "ReservationEvent",
    "reservation_event_identifier",
    "reservation_identifier",
]

#: Bumped when a stored reservation artifact changes shape.
RESERVATIONS_SCHEMA_VERSION = "1.0.0"


def reservation_identifier(
    *,
    campaign_id: str,
    allocation_id: str,
    opportunity_id: str,
    schema_version: str = RESERVATIONS_SCHEMA_VERSION,
) -> str:
    """Derive a reservation's identity from the authorisation it holds capital for.

    Deliberately excludes the clock and every runtime measurement, so replaying
    the allocation ledger twice produces the same reservation rather than a
    second one. One authorisation, one reservation — the same property that
    makes :func:`~trading_system.risk.models.opportunity_identifier` stop the
    same opportunity being funded twice.
    """
    digest = stable_hash(
        ["RESERVATION", schema_version, campaign_id, allocation_id, opportunity_id]
    )
    return f"reservation-{digest[:20]}"


def reservation_event_identifier(
    *,
    reservation_id: str,
    sequence: int,
    event_type: str,
    schema_version: str = RESERVATIONS_SCHEMA_VERSION,
) -> str:
    """Derive one event's identity, so a replayed observation does not duplicate."""
    digest = stable_hash(
        ["RESERVATION_EVENT", schema_version, reservation_id, sequence, event_type]
    )
    return f"resevt-{digest[:20]}"


class Reservation(ImmutableModel):
    """Capital this campaign committed to one authorisation, and its fate.

    Immutable. Every later observation is an appended
    :class:`ReservationEvent`, and the current record is a fold of them — so an
    audit can still answer "when did this capital stop being available, and on
    what evidence", which is precisely the question an unexplained shortfall in
    the campaign turns into.
    """

    reservation_id: Identifier
    campaign_id: Identifier
    allocation_id: Identifier
    opportunity_id: Identifier
    schema_version: Identifier = RESERVATIONS_SCHEMA_VERSION

    symbol: Ticker
    strategy: StrategyType
    currency: str = Field(min_length=3, max_length=8)

    # --- what Milestone 7 authorised. Never overwritten. -------------------
    authorized_amount: Money = Field(ge=0)
    authorized_max_loss: Money = Field(default=Decimal("0"), ge=0)
    authorized_quantity: int = Field(ge=0)
    authorized_at: UtcDatetime

    # --- what actually happened -------------------------------------------
    consumed_amount: Money = Field(default=Decimal("0"), ge=0)
    consumed_quantity: int = Field(default=0, ge=0)
    released_amount: Money = Field(default=Decimal("0"), ge=0)
    remaining_amount: Money = Field(default=Decimal("0"), ge=0)
    #: Consumed capital beyond the authorisation. Only a recorded broker
    #: correction may make this non-zero; the validator refuses it otherwise.
    over_authorized_amount: Money = Field(default=Decimal("0"), ge=0)
    #: True when ``consumed_amount`` came from actual fill economics rather
    #: than from the authorisation's own unit cost prorated by filled units.
    #: Both are exact; they are not the same number, and which one was used is
    #: part of the record rather than something to infer from context.
    consumed_from_actual_fills: bool = False

    state: ReservationState = ReservationState.RESERVED
    reason_codes: list[ReservationReasonCode] = Field(default_factory=list)

    created_at: UtcDatetime
    updated_at: UtcDatetime

    # --- provenance: ids, never copies -------------------------------------
    execution_id: Identifier | None = None
    execution_request_id: Identifier | None = None
    purchase_card_id: Identifier | None = None
    contract_selection_id: Identifier | None = None
    research_report_id: Identifier | None = None
    allocation_run_id: Identifier | None = None
    broker_order_id: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _the_accounting_balances(self) -> Reservation:
        total = self.consumed_amount + self.released_amount + self.remaining_amount
        authorized = self.authorized_amount + self.over_authorized_amount
        if total != authorized:
            raise ValueError(
                f"reservation {self.reservation_id}: consumed {self.consumed_amount} + released "
                f"{self.released_amount} + remaining {self.remaining_amount} is {total}, which "
                f"does not equal the authorised {authorized}. Capital that is neither spent, "
                f"returned nor committed has been lost track of"
            )
        return self

    @model_validator(mode="after")
    def _an_overrun_needs_evidence(self) -> Reservation:
        if self.over_authorized_amount and (
            ReservationReasonCode.BROKER_CORRECTION not in self.reason_codes
        ):
            raise ValueError(
                f"reservation {self.reservation_id} consumed {self.over_authorized_amount} more "
                f"than was authorised without a recorded broker correction. Spending past an "
                f"authorisation is either a broker fact worth recording or an accounting bug; "
                f"it is never a silent adjustment"
            )
        return self

    @model_validator(mode="after")
    def _the_state_matches_the_money(self) -> Reservation:
        """The state is a claim about the money and must agree with it.

        ``UNKNOWN`` is the important line here: it requires capital still
        committed. A reservation that had released everything and then claimed
        ``UNKNOWN`` would be the exact failure this milestone exists to
        prevent — an ambiguous order with its budget already spent again.
        """
        state = self.state
        if state is ReservationState.RESERVED and (self.consumed_amount or self.released_amount):
            raise ValueError(
                f"reservation {self.reservation_id} is RESERVED but has already consumed "
                f"{self.consumed_amount} and released {self.released_amount}"
            )
        if state is ReservationState.RELEASED and (self.consumed_amount or self.remaining_amount):
            raise ValueError(
                f"reservation {self.reservation_id} is RELEASED but still holds "
                f"{self.remaining_amount} and consumed {self.consumed_amount}; a release is a "
                f"claim that nothing was spent"
            )
        if state is ReservationState.CONSUMED and (
            self.remaining_amount or not self.consumed_amount
        ):
            raise ValueError(
                f"reservation {self.reservation_id} is CONSUMED but consumed "
                f"{self.consumed_amount} with {self.remaining_amount} still committed"
            )
        if state is ReservationState.PARTIALLY_CONSUMED and not (
            self.consumed_amount and self.remaining_amount
        ):
            raise ValueError(
                f"reservation {self.reservation_id} is PARTIALLY_CONSUMED but consumed "
                f"{self.consumed_amount} of {self.authorized_amount} with "
                f"{self.remaining_amount} committed; partially consumed means both are non-zero"
            )
        if state is ReservationState.UNKNOWN and not self.remaining_amount:
            raise ValueError(
                f"reservation {self.reservation_id} is UNKNOWN but holds no committed capital. "
                f"An unresolved execution may be a live order; its capital stays locked until "
                f"the broker settles what happened"
            )
        if state is ReservationState.UNKNOWN and (
            ReservationReasonCode.EXECUTION_UNKNOWN not in self.reason_codes
        ):
            raise ValueError(
                f"reservation {self.reservation_id} is UNKNOWN without saying so in its reason "
                f"codes; an operator must be able to filter for exactly this case"
            )
        return self

    # --- derived views -----------------------------------------------------
    @property
    def committed_amount(self) -> Decimal:
        """Capital this reservation still takes out of the campaign.

        Consumed capital is in a position and remaining capital is held for an
        order that may yet be sent; both are unavailable. Only released capital
        returns to the campaign.
        """
        return self.consumed_amount + self.remaining_amount

    @property
    def committed(self) -> bool:
        return self.state in COMMITTED_RESERVATION_STATES

    @property
    def locked_by_uncertainty(self) -> bool:
        """Whether this capital is held solely because an outcome is unknown."""
        return self.state is ReservationState.UNKNOWN

    @property
    def unspent_authorisation(self) -> Decimal:
        """Authorised capital that no fill has claimed. Not the same as available."""
        return self.authorized_amount - self.consumed_amount

    def with_event(self, event: ReservationEvent) -> Reservation:
        """Fold one observation onto this reservation, returning a new one.

        Amounts move by explicit deltas rather than being overwritten, so an
        event that says nothing about the money leaves the money alone. The
        resulting record is validated, which means an event that would break
        the accounting identity raises and changes nothing.
        """
        if event.reservation_id != self.reservation_id:
            raise ValueError(
                f"event {event.event_id} belongs to reservation {event.reservation_id}, not "
                f"{self.reservation_id}"
            )
        consumed = self.consumed_amount + event.consumed_delta
        released = self.released_amount + event.released_delta
        authorized = self.authorized_amount + self.over_authorized_amount + event.over_delta
        remaining = authorized - consumed - released

        updates: dict[str, object] = {
            "state": event.state,
            "updated_at": event.observed_at,
            "consumed_amount": consumed,
            "released_amount": released,
            "remaining_amount": remaining,
            "over_authorized_amount": self.over_authorized_amount + event.over_delta,
            "consumed_quantity": self.consumed_quantity + event.consumed_quantity_delta,
        }
        if event.reason_code is not None and event.reason_code not in self.reason_codes:
            updates["reason_codes"] = [*self.reason_codes, event.reason_code]
        if event.execution_id is not None:
            updates["execution_id"] = event.execution_id
        if event.broker_order_id is not None:
            updates["broker_order_id"] = event.broker_order_id
        if event.consumed_from_actual_fills:
            updates["consumed_from_actual_fills"] = True
        if event.detail:
            updates["detail"] = event.detail

        # Reconstructed rather than copied, so every validator runs again. A
        # copy would let an event that breaks the accounting identity produce a
        # record that cannot be true — and this is a money ledger, so the place
        # to find that out is here rather than in a later total.
        return Reservation(**(self.model_dump() | updates))


class ReservationEvent(ImmutableModel):
    """One appended observation about a reservation.

    Deltas rather than totals, deliberately. A total recorded on an event is a
    second copy of the record's own state and drifts from it the first time two
    observations arrive out of order; a delta composes.
    """

    event_id: Identifier
    reservation_id: Identifier
    #: Monotonic within one reservation, so ordering survives equal timestamps.
    sequence: int = Field(ge=0)
    event_type: ReservationEventType
    #: The state the reservation is in *after* this event.
    state: ReservationState
    occurred_at: UtcDatetime
    observed_at: UtcDatetime
    source: Identifier
    schema_version: Identifier = RESERVATIONS_SCHEMA_VERSION

    consumed_delta: Money = Field(default=Decimal("0"))
    released_delta: Money = Field(default=Decimal("0"))
    over_delta: Money = Field(default=Decimal("0"))
    consumed_quantity_delta: int = Field(default=0)
    consumed_from_actual_fills: bool = False

    reason_code: ReservationReasonCode | None = None
    detail: str | None = None
    execution_id: Identifier | None = None
    broker_order_id: str | None = None
    #: The reconciliation that produced this observation, where one did. An
    #: economic change with no evidence behind it is not something this ledger
    #: should be able to record silently.
    reconciliation_id: Identifier | None = None
    fill_ids: list[str] = Field(default_factory=list)


class CampaignCapital(ImmutableModel):
    """What the campaign's capital is actually doing, right now.

    The answer Milestone 7 could not give: how much of the envelope is spent,
    how much is held for orders that have not resolved, and how much is
    genuinely free. ``locked_by_unknown`` is called out separately because it
    is the figure an operator most needs and the one most easily mistaken for
    available money.
    """

    campaign_id: Identifier
    as_of: UtcDatetime
    currency: str = Field(min_length=3, max_length=8)
    schema_version: Identifier = RESERVATIONS_SCHEMA_VERSION

    budget: Money = Field(ge=0)
    reserve: Money = Field(ge=0)

    authorized_total: Money = Field(default=Decimal("0"), ge=0)
    consumed_total: Money = Field(default=Decimal("0"), ge=0)
    released_total: Money = Field(default=Decimal("0"), ge=0)
    committed_total: Money = Field(default=Decimal("0"), ge=0)
    #: Capital held solely because an execution's outcome is unknown. Not
    #: available, and not in a position either.
    locked_by_unknown: Money = Field(default=Decimal("0"), ge=0)
    #: May be negative if a broker correction consumed past the envelope. That
    #: is a real state worth reporting, not one to clamp away.
    available: Money = Decimal("0")

    reservation_count: int = Field(default=0, ge=0)
    unknown_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _available_is_what_is_left(self) -> CampaignCapital:
        expected = self.budget - self.reserve - self.committed_total
        if self.available != expected:
            raise ValueError(
                f"campaign {self.campaign_id}: available {self.available} is not budget "
                f"{self.budget} less reserve {self.reserve} less committed "
                f"{self.committed_total} ({expected})"
            )
        return self

    @property
    def allocatable(self) -> Decimal:
        return self.budget - self.reserve

    @property
    def constrained_by_uncertainty(self) -> bool:
        """Whether unresolved executions are what is holding capital back."""
        return self.locked_by_unknown > 0
