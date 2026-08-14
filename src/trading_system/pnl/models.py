"""Canonical realised profit-and-loss artifacts (Milestone 11).

Three artifacts, and the boundaries between them are the design:

:class:`RealizedPnL`
    What one *structure* made or lost, matched leg by leg from
    broker-confirmed fills. One record per closed strategy position, never one
    per leg — a straddle is one trade with one result, and reporting two would
    describe two positions nobody opened.
:class:`DailyPnL`
    One exchange-local trading day's realised result, and — separately — how
    reliable that figure is. The daily loss limit is evaluated against this.
:class:`ReservationSettlement`
    The record of committed capital returning to the campaign, or of the
    refusal to return it. Written whichever way it went: a decision not to
    settle is a decision.

What is deliberately absent, with tests that fail loudly:

* **No estimate.** No field may be filled from a limit price, a reference
  price, a midpoint or an assumed multiplier. Where the fills do not support a
  figure it is ``None`` and the status is ``NOT_AVAILABLE``.
* **No FX.** Entry and exit in different currencies produce no result at all.
* **No decision.** No quantity, no permission, no order, no limit. These are
  measurements of things that already happened.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    CommissionStatus,
    DailyPnLStatus,
    OptionRight,
    PnLReasonCode,
    PnLStatus,
    SettlementBlockReason,
    SettlementStatus,
    StrategyType,
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
    "PNL_SCHEMA_VERSION",
    "DailyPnL",
    "PnLRunResult",
    "RealizedPnL",
    "RealizedPnLLeg",
    "ReservationSettlement",
    "daily_pnl_identifier",
    "realized_pnl_identifier",
    "settlement_identifier",
]

#: Bumped when a stored profit-and-loss artifact changes shape. Folded into
#: every derived identifier, so records written under different shapes cannot
#: collide.
PNL_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def realized_pnl_identifier(
    *,
    position_id: str,
    entry_execution_id: str,
    exit_execution_ids: list[str],
    matched_quantity: str,
    schema_version: str = PNL_SCHEMA_VERSION,
) -> str:
    """Derive one realised result's identity from the fills behind it.

    Excludes the clock and includes ``matched_quantity`` deliberately. A
    position that closes in two tranches produces two genuinely different
    results — the first over four units, the second over ten — and an identity
    that ignored the quantity would make the second look like a rewrite of the
    first, which the immutable store would refuse. Recomputing an unchanged
    result lands on the same id, which is what makes the whole settlement path
    safe to run every fifteen minutes.
    """
    digest = stable_hash(
        [
            "REALIZED_PNL",
            schema_version,
            position_id,
            entry_execution_id,
            sorted(exit_execution_ids),
            matched_quantity,
        ]
    )
    return f"pnl-{digest[:20]}"


def daily_pnl_identifier(
    *,
    campaign_id: str,
    session_date: date,
    pnl_ids: list[str],
    status: str,
    schema_version: str = PNL_SCHEMA_VERSION,
) -> str:
    """Derive one day's roll-up identity from the results it aggregates.

    ``status`` is part of it because a day that was ``UNKNOWN`` at 16:00 and
    ``TRACKED`` at 17:00 — the missing commission report having arrived — is
    two different statements about that day, and both are worth keeping.
    """
    digest = stable_hash(
        [
            "DAILY_PNL",
            schema_version,
            campaign_id,
            session_date.isoformat(),
            sorted(pnl_ids),
            status,
        ]
    )
    return f"dailypnl-{digest[:20]}"


def settlement_identifier(
    *,
    reservation_id: str,
    position_id: str,
    settled_amount: str,
    status: str,
    schema_version: str = PNL_SCHEMA_VERSION,
) -> str:
    """Derive one settlement's identity from what it moved.

    This is what makes settlement idempotent *economically* rather than merely
    at the record level. Running it twice over unchanged evidence derives the
    same id, the reservation ledger recognises the replayed event and appends
    nothing, and the capital moves once. A duplicate record is untidy; a double
    release is money.
    """
    digest = stable_hash(
        [
            "RESERVATION_SETTLEMENT",
            schema_version,
            reservation_id,
            position_id,
            settled_amount,
            status,
        ]
    )
    return f"settlement-{digest[:20]}"


# ---------------------------------------------------------------------------
# The result of one structure
# ---------------------------------------------------------------------------
class RealizedPnLLeg(ImmutableModel):
    """One leg's contribution, from its own confirmed fills.

    Kept alongside the structure total rather than instead of it. The total is
    what the trade made; the legs are how. A straddle whose call paid for the
    put is a different story from one where both lost, and only the legs tell
    it — but neither is ever reported as a trade in its own right.
    """

    leg_index: int = Field(ge=0)
    key: Identifier
    contract_id: int | None = None
    right: OptionRight | None = None
    strike: Money | None = Field(default=None, gt=0)
    expiration: date | None = None
    #: Never assumed. ``None`` here is what makes the whole result unavailable.
    multiplier: int | None = Field(default=None, ge=1)

    #: Units bought and sold, from confirmed fills only.
    opened_quantity: Money = Field(default=Decimal("0"), ge=0)
    closed_quantity: Money = Field(default=Decimal("0"), ge=0)
    #: The units both sides support. The result covers these and no others.
    matched_quantity: Money = Field(default=Decimal("0"), ge=0)

    #: Money actually paid for ``matched_quantity``, multiplier included.
    entry_cost: Money | None = None
    #: Money actually received for ``matched_quantity``.
    exit_proceeds: Money | None = None
    #: The broker's quoted terms (6.05), not money (605.00).
    average_entry_quote: Money | None = Field(default=None, gt=0)
    average_exit_quote: Money | None = Field(default=None, gt=0)

    entry_commission: Money | None = None
    exit_commission: Money | None = None

    entry_fill_ids: list[str] = Field(default_factory=list)
    exit_fill_ids: list[str] = Field(default_factory=list)
    currency: str | None = None

    @model_validator(mode="after")
    def _matched_never_exceeds_either_side(self) -> RealizedPnLLeg:
        if self.matched_quantity > self.opened_quantity:
            raise ValueError(
                f"leg {self.leg_index} matched {self.matched_quantity} units against "
                f"{self.opened_quantity} opened; a position cannot close more than it opened"
            )
        if self.matched_quantity > self.closed_quantity:
            raise ValueError(
                f"leg {self.leg_index} matched {self.matched_quantity} units against "
                f"{self.closed_quantity} closed"
            )
        return self

    @property
    def gross_pnl(self) -> Decimal | None:
        """Proceeds less cost, for the matched units. ``None`` if either is unknown."""
        if self.entry_cost is None or self.exit_proceeds is None:
            return None
        return self.exit_proceeds - self.entry_cost


class RealizedPnL(ImmutableModel):
    """What one closed strategy structure actually made or lost.

    Immutable and content-addressed. Recomputing it over unchanged fills
    produces the same record under the same id, which is what lets the
    settlement job run on a cadence without accumulating near-identical copies
    of one fact.

    The gross and net figures are separate and both may be ``None``
    independently: the broker's fill prices support a gross result long before
    its commission reports arrive, and reporting a net figure built from a
    zero-filled commission would understate the cost of every trade the feed
    was slow about.
    """

    pnl_id: Identifier
    position_id: Identifier
    campaign_id: Identifier
    schema_version: Identifier = PNL_SCHEMA_VERSION

    underlying: Ticker
    strategy: StrategyType
    status: PnLStatus
    reason_codes: list[PnLReasonCode] = Field(default_factory=list)

    # --- what closed -------------------------------------------------------
    #: Units of the *structure* that opened and closed, from the weakest leg.
    opened_quantity: Money = Field(default=Decimal("0"), ge=0)
    closed_quantity: Money = Field(default=Decimal("0"), ge=0)
    matched_quantity: Money = Field(default=Decimal("0"), ge=0)
    legs: list[RealizedPnLLeg] = Field(default_factory=list)

    # --- the money ---------------------------------------------------------
    #
    # Every figure below is money for the matched units, multiplier included,
    # and every one of them is nullable. A broker that reported nothing
    # reported nothing; a zero would be a claim we are not entitled to make.
    entry_cost: Money | None = None
    exit_proceeds: Money | None = None
    realized_gross_pnl: Money | None = None
    #: Gross less actually-reported commissions. ``None`` whenever a single
    #: commission is missing — a partly-known cost is not a cost.
    realized_net_pnl: Money | None = None
    total_commission: Money | None = None
    commission_status: CommissionStatus = CommissionStatus.NOT_AVAILABLE
    #: Result over entry cost, as a percentage. ``None`` when either is unknown
    #: or the entry cost is zero.
    return_pct: float | None = None
    currency: str | None = None

    # --- when --------------------------------------------------------------
    opened_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    #: The exchange-local session the closure belongs to. A closure at 21:30
    #: UTC belongs to the New York session that has just ended, and bounding
    #: the day in UTC would file it under tomorrow.
    session_date: date | None = None
    computed_at: UtcDatetime

    # --- provenance: ids and fill references, never copies -----------------
    entry_execution_id: Identifier | None = None
    exit_execution_ids: list[str] = Field(default_factory=list)
    allocation_id: Identifier | None = None
    opportunity_id: Identifier | None = None
    reservation_id: Identifier | None = None
    research_report_id: Identifier | None = None
    contract_selection_id: Identifier | None = None
    #: Every broker execution id the figure rests on. The audit trail back to
    #: the account, without copying anything out of it.
    source_fill_ids: list[str] = Field(default_factory=list)
    account_reference: Identifier | None = None
    broker_source: Identifier = "UNKNOWN"
    versions: SystemVersions | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _an_unavailable_result_claims_no_figure(self) -> RealizedPnL:
        """``NOT_AVAILABLE`` carries no money, and a reason for why.

        Enforced rather than trusted. A record that said "not available" while
        carrying a gross figure would be read by whichever consumer looked at
        the number rather than the status — and one of those consumers is the
        daily loss limit.
        """
        if self.status is PnLStatus.NOT_AVAILABLE:
            populated = [
                name
                for name, value in (
                    ("realized_gross_pnl", self.realized_gross_pnl),
                    ("realized_net_pnl", self.realized_net_pnl),
                    ("return_pct", self.return_pct),
                )
                if value is not None
            ]
            if populated:
                raise ValueError(
                    f"realised profit and loss {self.pnl_id} is NOT_AVAILABLE but carries "
                    f"{', '.join(populated)}. An unavailable result reports no figure at all; "
                    f"a number next to that status is the one a consumer will read"
                )
            if not self.reason_codes:
                raise ValueError(
                    f"realised profit and loss {self.pnl_id} is NOT_AVAILABLE without saying "
                    f"why. 'We could not compute it' is only useful with the reason attached"
                )
        return self

    @model_validator(mode="after")
    def _the_arithmetic_holds(self) -> RealizedPnL:
        if (
            self.realized_gross_pnl is not None
            and self.entry_cost is not None
            and self.exit_proceeds is not None
            and self.realized_gross_pnl != self.exit_proceeds - self.entry_cost
        ):
            raise ValueError(
                f"realised profit and loss {self.pnl_id}: gross {self.realized_gross_pnl} is "
                f"not proceeds {self.exit_proceeds} less cost {self.entry_cost}"
            )
        if (
            self.realized_net_pnl is not None
            and self.realized_gross_pnl is not None
            and self.total_commission is not None
            and self.realized_net_pnl != self.realized_gross_pnl - self.total_commission
        ):
            raise ValueError(
                f"realised profit and loss {self.pnl_id}: net {self.realized_net_pnl} is not "
                f"gross {self.realized_gross_pnl} less commission {self.total_commission}"
            )
        return self

    @model_validator(mode="after")
    def _a_net_figure_needs_known_costs(self) -> RealizedPnL:
        if self.realized_net_pnl is not None and self.commission_status is not (
            CommissionStatus.KNOWN
        ):
            raise ValueError(
                f"realised profit and loss {self.pnl_id} reports a net figure while its "
                f"commission status is {self.commission_status.value}. A net result computed "
                f"over partly-reported costs understates what the trade took"
            )
        return self

    @model_validator(mode="after")
    def _a_partial_result_says_so(self) -> RealizedPnL:
        if (
            self.status is PnLStatus.COMPLETE
            and self.opened_quantity
            and self.matched_quantity != self.opened_quantity
        ):
            raise ValueError(
                f"realised profit and loss {self.pnl_id} is COMPLETE but matched "
                f"{self.matched_quantity} of {self.opened_quantity} units. A partly closed "
                f"structure is PARTIAL; calling it complete would report a finished trade"
            )
        return self

    # --- derived views -----------------------------------------------------
    @property
    def available(self) -> bool:
        return self.status is not PnLStatus.NOT_AVAILABLE

    @property
    def best_available_pnl(self) -> Decimal | None:
        """The net figure where costs are known, otherwise the gross one.

        Named for what it is. A caller that needs to know whether commissions
        are in it reads :attr:`commission_status`; a caller that silently used
        this as "the result" would be understating losses by the trading costs,
        which is why the name refuses to be mistaken for one.
        """
        return (
            self.realized_net_pnl
            if self.realized_net_pnl is not None
            else (self.realized_gross_pnl)
        )

    @property
    def is_loss(self) -> bool:
        figure = self.best_available_pnl
        return figure is not None and figure < 0


# ---------------------------------------------------------------------------
# The day
# ---------------------------------------------------------------------------
class DailyPnL(ImmutableModel):
    """One exchange-local trading day's realised result, and its reliability.

    ``status`` is the field that matters. ``TRACKED`` means every closure that
    day produced a usable figure and the total is a real number. ``UNKNOWN``
    means positions closed and at least one produced nothing — which is not
    zero loss, and the risk engine is required to treat the two differently.
    """

    daily_pnl_id: Identifier
    campaign_id: Identifier
    session_date: date
    timezone: str
    schema_version: Identifier = PNL_SCHEMA_VERSION

    status: DailyPnLStatus
    currency: str = Field(min_length=3, max_length=8)

    #: The day's realised result. ``None`` whenever ``status`` is not
    #: ``TRACKED`` — an unknown figure is never rendered as a number.
    realized_pnl: Money | None = None
    realized_gross_pnl: Money | None = None
    total_commission: Money | None = None
    commission_status: CommissionStatus = CommissionStatus.NOT_AVAILABLE

    #: The loss, as a positive number, or zero on a profitable day. The figure
    #: the daily loss limit is compared against.
    realized_loss: Money | None = Field(default=None, ge=0)

    positions_closed: int = Field(default=0, ge=0)
    positions_with_result: int = Field(default=0, ge=0)
    positions_without_result: int = Field(default=0, ge=0)

    pnl_ids: list[str] = Field(default_factory=list)
    #: Positions that closed today and produced no usable figure. Named, not
    #: counted only: "which one" is the first question anybody asks.
    unavailable_position_ids: list[str] = Field(default_factory=list)

    computed_at: UtcDatetime
    detail: str | None = None

    @model_validator(mode="after")
    def _an_unknown_day_reports_no_total(self) -> DailyPnL:
        if self.status is not DailyPnLStatus.TRACKED and (
            self.realized_pnl is not None or self.realized_loss is not None
        ):
            raise ValueError(
                f"daily profit and loss {self.daily_pnl_id} is {self.status.value} but reports "
                f"a total. A day whose figure is not trustworthy reports no figure: the loss "
                f"limit must see an absence, never a number that happens to be small"
            )
        if self.status is DailyPnLStatus.TRACKED and self.realized_pnl is None:
            raise ValueError(
                f"daily profit and loss {self.daily_pnl_id} is TRACKED but carries no total"
            )
        if self.status is DailyPnLStatus.UNKNOWN and not self.unavailable_position_ids:
            raise ValueError(
                f"daily profit and loss {self.daily_pnl_id} is UNKNOWN without naming a "
                f"position that produced no result"
            )
        return self

    @model_validator(mode="after")
    def _the_loss_matches_the_total(self) -> DailyPnL:
        if self.realized_pnl is None or self.realized_loss is None:
            return self
        expected = -self.realized_pnl if self.realized_pnl < 0 else Decimal("0")
        if self.realized_loss != expected:
            raise ValueError(
                f"daily profit and loss {self.daily_pnl_id}: loss {self.realized_loss} does "
                f"not follow from a realised result of {self.realized_pnl}"
            )
        return self

    @model_validator(mode="after")
    def _the_counts_add_up(self) -> DailyPnL:
        total = self.positions_with_result + self.positions_without_result
        if total != self.positions_closed:
            raise ValueError(
                f"daily profit and loss {self.daily_pnl_id}: {self.positions_with_result} with "
                f"a result plus {self.positions_without_result} without is {total}, not the "
                f"{self.positions_closed} positions recorded closed"
            )
        return self

    @property
    def tracked(self) -> bool:
        return self.status is DailyPnLStatus.TRACKED


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
class ReservationSettlement(ImmutableModel):
    """Committed capital returning to the campaign — or the refusal to return it.

    Written whichever way it went. A refusal is the more interesting record of
    the two: it names the evidence that was missing, which is what an operator
    needs to know when a campaign's available capital does not go back up after
    a position closes.
    """

    settlement_id: Identifier
    reservation_id: Identifier
    position_id: Identifier
    campaign_id: Identifier
    allocation_id: Identifier
    schema_version: Identifier = PNL_SCHEMA_VERSION

    status: SettlementStatus
    block_reason: SettlementBlockReason | None = None
    currency: str = Field(min_length=3, max_length=8)

    #: Capital committed to this authorisation before settlement.
    committed_before: Money = Field(default=Decimal("0"), ge=0)
    #: Capital this settlement returned to the campaign. Zero on a refusal.
    settled_amount: Money = Field(default=Decimal("0"), ge=0)
    #: Still committed after it. Non-zero after a partial settlement.
    committed_after: Money = Field(default=Decimal("0"), ge=0)

    pnl_id: Identifier | None = None
    realized_pnl: Money | None = None
    #: The fraction of the structure this settlement covers, as matched units
    #: over authorised units. Recorded so a partial settlement's arithmetic is
    #: checkable rather than trusted.
    matched_quantity: Money = Field(default=Decimal("0"), ge=0)
    authorized_quantity: int = Field(default=0, ge=0)

    settled_at: UtcDatetime
    reconciliation_id: Identifier | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _a_refusal_moves_nothing_and_says_why(self) -> ReservationSettlement:
        if self.status is SettlementStatus.BLOCKED:
            if self.settled_amount:
                raise ValueError(
                    f"settlement {self.settlement_id} is BLOCKED but returned "
                    f"{self.settled_amount}. A refusal moves no money"
                )
            if self.block_reason is None:
                raise ValueError(
                    f"settlement {self.settlement_id} is BLOCKED without naming the evidence "
                    f"that was missing. 'Blocked' alone is not something anybody can act on"
                )
        if self.status is not SettlementStatus.BLOCKED and self.block_reason is not None:
            raise ValueError(
                f"settlement {self.settlement_id} is {self.status.value} but carries a block "
                f"reason; a settlement that happened was not refused"
            )
        return self

    @model_validator(mode="after")
    def _the_capital_balances(self) -> ReservationSettlement:
        if self.committed_before - self.settled_amount != self.committed_after:
            raise ValueError(
                f"settlement {self.settlement_id}: {self.committed_before} committed less "
                f"{self.settled_amount} settled is not the {self.committed_after} recorded as "
                f"still committed. Capital that is neither returned nor held has been lost "
                f"track of"
            )
        return self

    @model_validator(mode="after")
    def _a_full_settlement_leaves_nothing_committed(self) -> ReservationSettlement:
        if self.status is SettlementStatus.SETTLED and self.committed_after:
            raise ValueError(
                f"settlement {self.settlement_id} is SETTLED but leaves {self.committed_after} "
                f"committed; that is PARTIALLY_SETTLED"
            )
        if self.status is SettlementStatus.PARTIALLY_SETTLED and not (
            self.settled_amount and self.committed_after
        ):
            raise ValueError(
                f"settlement {self.settlement_id} is PARTIALLY_SETTLED but returned "
                f"{self.settled_amount} leaving {self.committed_after}; partial means both are "
                f"non-zero"
            )
        return self

    @property
    def moved_capital(self) -> bool:
        return self.settled_amount > 0


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
class PnLRunResult(ImmutableModel):
    """The record of one settlement pass: what it computed and what it moved.

    ``orders_submitted`` is carried and is always zero. Nothing in this package
    can place an order, and the line is evidence of that rather than
    decoration — it is read off nothing, because there is no broker here to
    read it off.
    """

    run_id: Identifier
    campaign_id: Identifier
    as_of: UtcDatetime
    generated_at: UtcDatetime
    schema_version: Identifier = PNL_SCHEMA_VERSION

    dry_run: bool = False
    positions_examined: int = Field(default=0, ge=0)
    results_computed: int = Field(default=0, ge=0)
    results_unavailable: int = Field(default=0, ge=0)
    settlements_applied: int = Field(default=0, ge=0)
    settlements_blocked: int = Field(default=0, ge=0)
    capital_returned: Money = Field(default=Decimal("0"), ge=0)
    currency: str = Field(min_length=3, max_length=8)

    pnl_ids: list[str] = Field(default_factory=list)
    settlement_ids: list[str] = Field(default_factory=list)
    daily_pnl_id: Identifier | None = None

    #: Structurally zero. This package holds no broker and has no order path.
    orders_submitted: int = Field(default=0, ge=0, le=0)
    versions: SystemVersions | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _a_dry_run_returns_no_capital(self) -> PnLRunResult:
        if self.dry_run and (self.capital_returned or self.settlements_applied):
            raise ValueError(
                f"profit-and-loss run {self.run_id} is a dry run but reports "
                f"{self.settlements_applied} settlement(s) returning {self.capital_returned}. "
                f"A diagnostic moves no money"
            )
        return self
