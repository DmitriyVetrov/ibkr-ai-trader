"""Canonical position and fill artifacts.

Milestone 9 stores **two** kinds of position record, and the whole milestone
rests on never confusing them:

:class:`ObservedPosition`
    What the broker says it holds. Broker reality, normalised but never
    corrected, never completed and never inferred.
:class:`ExpectedPosition`
    What *this system* believes should exist, projected from confirmed broker
    fills and from nothing else. An allocation is not a position, a submitted
    order is not a position, an acknowledgement is not a position, and an
    ``UNKNOWN`` submission is not a position.

Comparing the two is reconciliation. Merging them would destroy the only
evidence that they ever disagreed, which is the one thing worth having.

Three properties are enforced by the shapes here rather than by discipline:

* **Identity comes from the broker.** A position's identity prefers the broker
  contract id and falls back to symbol/expiry/strike/right only when the broker
  supplied none — and records which happened. Adjusted option contracts share
  their human-readable fields, so a system that keyed on those alone would
  eventually merge two different instruments into one record.
* **An unavailable value is ``None``.** Market value, unrealised profit and
  loss and commission are all nullable and stay nullable. "The broker reported
  no market value" and "the market value is zero" are different claims.
* **Units are named apart.** Three different numbers describe the price of one
  option position and mixing any two of them is a factor-of-100 error:

  ``price`` / ``average_price``
      the broker's *quoted* terms — 6.05;
  ``average_cost``
      money for one contract, multiplier included — 605.00, which is what
      IBKR's ``avgCost`` reports for an option;
  ``market_value`` / ``total_cost``
      money for the whole holding.

  Nothing here converts silently between them, and a conversion that cannot be
  performed because the multiplier is unknown yields ``None`` rather than an
  assumed 100.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    AcquisitionProvenance,
    BrokerReadStatus,
    DataQuality,
    OptionRight,
    OrderSide,
    PositionState,
    SecurityType,
    StrategyType,
    StructureStatus,
    ThesisStatus,
    TradingMode,
)
from trading_system.domain.models import (
    Identifier,
    ImmutableModel,
    Money,
    OptionLeg,
    PositionSnapshot,
    Ticker,
    UtcDatetime,
)

__all__ = [
    "POSITIONS_SCHEMA_VERSION",
    "BrokerPositionSnapshot",
    "ExpectedPosition",
    "ObservedFill",
    "ObservedPosition",
    "StrategyLegPosition",
    "StrategyPosition",
    "contract_key",
    "fill_identifier",
    "mask_account",
    "position_identifier",
    "position_snapshot_identifier",
    "strategy_position_identifier",
]

#: Bumped when a stored position artifact changes shape. Folded into every
#: derived identifier, so records written under different shapes cannot collide.
POSITIONS_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def mask_account(account_id: str | None, *, visible: int = 4) -> str:
    """Reduce an account number to what is safe to store and print.

    Account numbers reach logs, artifacts and pasted terminal output, and none
    of those needs the full value to confirm the right account is connected.
    The masked form is what every Milestone 9 artifact carries; the full number
    is never written to one.
    """
    if not account_id:
        return "(unknown)"
    text = account_id.strip()
    if not text:
        return "(unknown)"
    if visible <= 0 or len(text) <= visible:
        return "*" * len(text)
    return f"{'*' * (len(text) - visible)}{text[-visible:]}"


def contract_key(
    *,
    contract_id: int | None,
    symbol: str,
    security_type: SecurityType,
    expiration: date | None = None,
    strike: Decimal | None = None,
    right: OptionRight | None = None,
    currency: str | None = None,
) -> str:
    """A deterministic identity for one instrument.

    The broker's contract id wins whenever there is one: it is the only
    identifier that distinguishes contracts sharing a symbol, an expiration, a
    strike and a right — which adjusted options after a corporate action
    routinely do.

    The fallback is used only when the broker supplied no id, and it is
    deliberately shaped so the two forms can never collide: one begins
    ``cid:``, the other ``sym:``. A record keyed on the weaker form says so, so
    "we identified this position by its label" is visible rather than assumed.
    """
    if contract_id is not None and contract_id > 0:
        return f"cid:{contract_id}"
    parts = [
        symbol.strip().upper(),
        security_type.value,
        expiration.isoformat() if expiration else "",
        str(strike) if strike is not None else "",
        right.value if right else "",
        (currency or "").strip().upper(),
    ]
    return "sym:" + "|".join(parts)


def position_identifier(
    *,
    account_reference: str,
    key: str,
    schema_version: str = POSITIONS_SCHEMA_VERSION,
) -> str:
    """Derive a position's identity from the account and the instrument.

    Deliberately excludes the observation clock, so the *same* holding keeps
    the *same* id across snapshots. That is what lets a history be assembled
    per contract, and what lets an expected position and an observed position
    be compared by id rather than by a fuzzy match on their fields.
    """
    digest = stable_hash(["POSITION", schema_version, account_reference, key])
    return f"position-{digest[:20]}"


def fill_identifier(
    *,
    broker_execution_id: str | None,
    broker_order_id: str | None = None,
    contract: str | None = None,
    side: str | None = None,
    quantity: str | None = None,
    price: str | None = None,
    executed_at: datetime | None = None,
    schema_version: str = POSITIONS_SCHEMA_VERSION,
) -> str:
    """Derive one fill's identity, so re-observing it does not duplicate it.

    The broker's execution id is used alone wherever there is one — that is
    what makes polling idempotent, and it is stable across sessions. Without
    one, the strongest deterministic combination of authoritative broker fields
    stands in: order, contract, side, quantity, price and the instant the
    broker says it traded. That is weaker, and a fill recorded under it is
    flagged rather than treated as equally certain.
    """
    if broker_execution_id and broker_execution_id.strip():
        digest = stable_hash(["FILL", schema_version, broker_execution_id.strip()])
    else:
        digest = stable_hash(
            [
                "FILL_DERIVED",
                schema_version,
                broker_order_id or "",
                contract or "",
                side or "",
                quantity or "",
                price or "",
                executed_at.isoformat() if executed_at else "",
            ]
        )
    return f"fill-{digest[:20]}"


def position_snapshot_identifier(
    *,
    broker: str,
    account_reference: str,
    as_of: datetime,
    payload_digest: str,
    schema_version: str = POSITIONS_SCHEMA_VERSION,
) -> str:
    """Derive a snapshot's id from *what the broker said*, plus when.

    The payload digest excludes observation clocks, exactly as the data layer's
    does, so re-reading an unchanged portfolio at the same instant lands on the
    same id and is recorded as a re-observation rather than as a second,
    differently-named copy of one account state.
    """
    digest = stable_hash(
        [
            "POSITION_SNAPSHOT",
            schema_version,
            broker,
            account_reference,
            as_of.isoformat(),
            payload_digest,
        ]
    )
    return f"positions-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}"


def strategy_position_identifier(
    *,
    opportunity_id: str,
    execution_id: str | None,
    schema_version: str = POSITIONS_SCHEMA_VERSION,
) -> str:
    """Derive a logical strategy position's identity from what it descends from."""
    digest = stable_hash(["STRATEGY_POSITION", schema_version, opportunity_id, execution_id or ""])
    return f"strategypos-{digest[:20]}"


# ---------------------------------------------------------------------------
# Broker reality
# ---------------------------------------------------------------------------
class ObservedPosition(ImmutableModel):
    """One position exactly as the broker reported it.

    Normalised into this system's types and nothing more: no field is filled
    in, corrected or completed. ``provenance`` records whether an execution of
    ours accounts for it, and ``UNKNOWN`` is a perfectly ordinary value — a
    pre-existing holding in a real paper account has no execution history here
    and must never be given an invented one.
    """

    position_id: Identifier
    account_reference: Identifier
    key: Identifier
    as_of: UtcDatetime
    observed_at: UtcDatetime
    schema_version: Identifier = POSITIONS_SCHEMA_VERSION

    # --- what -------------------------------------------------------------
    underlying: Ticker
    asset_class: SecurityType
    symbol: Identifier
    contract_id: int | None = None
    trading_class: str | None = None
    exchange: str | None = None
    local_symbol: str | None = None
    currency: str | None = None
    multiplier: int | None = Field(default=None, ge=1)
    expiration: date | None = None
    strike: Money | None = Field(default=None, gt=0)
    right: OptionRight | None = None

    # --- how much ---------------------------------------------------------
    #: Signed, and a ``Decimal`` because that is what the broker reports.
    #: Positive is long, negative is short, and nothing here assumes either.
    quantity: Money
    #: Money for one contract, multiplier included — IBKR's ``avgCost``.
    average_cost: Money | None = None

    # --- what the broker says it is worth ---------------------------------
    #
    # Every one of these is nullable and stays nullable. A broker that reported
    # no market value reported none; substituting a quantity times a reference
    # price and labelling it "market value" would be our arithmetic wearing the
    # broker's name.
    market_price: Money | None = None
    market_value: Money | None = None
    unrealized_pnl: Money | None = None
    realized_pnl: Money | None = None

    # --- provenance -------------------------------------------------------
    broker_source: Identifier
    broker_timestamp: UtcDatetime | None = None
    provenance: AcquisitionProvenance = AcquisitionProvenance.UNKNOWN
    #: False when the broker supplied no contract id and the identity had to
    #: fall back to human-readable fields. A weaker key, flagged as one.
    identified_by_contract_id: bool = True
    simulated: bool = False

    # A non-finite quantity needs no validator here: ``Money`` is a Decimal
    # field and pydantic refuses NaN and infinity outright, in this model and
    # in the :class:`~trading_system.domain.models.BrokerPosition` it is
    # translated from. A position size that is not a number is a broker
    # translation fault and fails at the type, which is the earliest place it
    # can. ``tests/positions/test_models.py`` pins that behaviour.

    @model_validator(mode="after")
    def _an_option_carries_its_option_terms(self) -> ObservedPosition:
        if self.asset_class in (SecurityType.OPTION, SecurityType.FUTURE_OPTION):
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
                raise ValueError(
                    f"option position {self.symbol} is missing {', '.join(missing)}; an option "
                    f"without its terms cannot be identified or reconciled"
                )
        return self

    @model_validator(mode="after")
    def _the_key_matches_how_it_was_identified(self) -> ObservedPosition:
        expected = self.key.startswith("cid:")
        if expected is not self.identified_by_contract_id:
            raise ValueError(
                f"position {self.position_id} claims identified_by_contract_id="
                f"{self.identified_by_contract_id} but its key is {self.key!r}"
            )
        return self

    @property
    def is_option(self) -> bool:
        return self.asset_class in (SecurityType.OPTION, SecurityType.FUTURE_OPTION)

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    def describe(self) -> str:
        """A one-line label. Derived; never a second copy of the record."""
        if self.is_option:
            return (
                f"{self.underlying} {self.expiration} "
                f"{self.strike} {self.right.value if self.right else ''}".strip()
            )
        return f"{self.underlying} ({self.asset_class.value})"


class BrokerPositionSnapshot(ImmutableModel):
    """Everything the broker reported holding, at one instant.

    Immutable and content-addressed: re-reading an unchanged account produces
    the same id and is recorded as a re-observation, never as a second account
    state.

    ``read_status`` is the field that keeps this honest. A snapshot with status
    ``UNAVAILABLE`` carries no positions *and cannot be read as an empty
    portfolio* — the validators refuse to let a failed read look like an
    account that holds nothing, because that mistake reports every real
    position as gone and every internal expectation as missing.
    """

    snapshot_id: Identifier
    account_reference: Identifier
    broker: Identifier
    trading_mode: TradingMode
    as_of: UtcDatetime
    observed_at: UtcDatetime
    schema_version: Identifier = POSITIONS_SCHEMA_VERSION

    read_status: BrokerReadStatus
    positions: list[ObservedPosition] = Field(default_factory=list)
    content_hash: Identifier

    broker_timestamp: UtcDatetime | None = None
    #: Structurally zero: capturing positions reads and does nothing else. Read
    #: off the broker's own counter rather than asserted, so the claim is
    #: evidence.
    orders_submitted: int = Field(default=0, ge=0)
    read_only: bool = True
    simulated: bool = False
    detail: str | None = None

    @model_validator(mode="after")
    def _a_capture_submits_no_orders(self) -> BrokerPositionSnapshot:
        if self.orders_submitted:
            raise ValueError(
                f"a position snapshot is a read-only capture, but this one reports "
                f"{self.orders_submitted} submitted order(s)"
            )
        return self

    @model_validator(mode="after")
    def _a_failed_read_is_not_an_empty_account(self) -> BrokerPositionSnapshot:
        if self.read_status.usable:
            if self.read_status is BrokerReadStatus.EMPTY and self.positions:
                raise ValueError("a snapshot marked EMPTY cannot carry positions")
            if self.read_status is BrokerReadStatus.OK and not self.positions:
                raise ValueError(
                    "a snapshot marked OK carries no positions. An account that genuinely "
                    "holds nothing is EMPTY; the two must stay distinguishable"
                )
            return self

        if self.positions:
            raise ValueError(
                f"a snapshot with read_status {self.read_status.value} carries "
                f"{len(self.positions)} position(s). If the broker answered, the status must "
                f"say so; a failed read must not carry data that would be reconciled against"
            )
        if not (self.detail or "").strip():
            raise ValueError(
                f"a snapshot with read_status {self.read_status.value} must say what went "
                f"wrong; 'we could not look' and 'there is nothing there' are different facts "
                f"and only one of them is a fact about the account"
            )
        return self

    @model_validator(mode="after")
    def _one_record_per_instrument(self) -> BrokerPositionSnapshot:
        keys = [position.key for position in self.positions]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(
                f"snapshot {self.snapshot_id} reports the same instrument twice: "
                f"{', '.join(duplicates)}"
            )
        return self

    @property
    def usable(self) -> bool:
        """Whether this snapshot may be reconciled against."""
        return self.read_status.usable

    @property
    def option_positions(self) -> list[ObservedPosition]:
        return [position for position in self.positions if position.is_option]

    def known_at(self, instant: datetime) -> bool:
        """Point-in-time visibility: retrieval binds, not the state described.

        A snapshot observed after ``instant`` was not available then, however
        recent the holdings it describes. A historical reconstruction that
        ignored this would claim we knew a position we had not yet looked at.
        """
        return self.observed_at <= instant and self.as_of <= instant

    def age_seconds(self, instant: datetime) -> float:
        return (instant - self.as_of).total_seconds()

    def by_key(self, key: str) -> ObservedPosition | None:
        return next((position for position in self.positions if position.key == key), None)

    def for_underlying(self, underlying: str) -> list[ObservedPosition]:
        wanted = underlying.strip().upper()
        return [position for position in self.positions if position.underlying == wanted]


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------
class ObservedFill(ImmutableModel):
    """One execution report from the broker, recorded once.

    The ground truth for what actually traded. A position is reconstructed from
    these and never from the orders that were submitted: an order that was sent
    is not evidence that it filled.

    ``commission`` is nullable and stays nullable. IBKR frequently reports a
    fill before its commission report arrives, and a zero commission is a claim
    about cost that would quietly understate what a trade actually took.
    """

    fill_id: Identifier
    account_reference: Identifier
    key: Identifier
    schema_version: Identifier = POSITIONS_SCHEMA_VERSION

    broker_execution_id: str | None = None
    broker_order_id: str | None = None

    # --- what -------------------------------------------------------------
    underlying: Ticker
    symbol: Identifier
    asset_class: SecurityType
    contract_id: int | None = None
    expiration: date | None = None
    strike: Money | None = Field(default=None, gt=0)
    right: OptionRight | None = None
    multiplier: int | None = Field(default=None, ge=1)
    local_symbol: str | None = None
    trading_class: str | None = None

    # --- how much ---------------------------------------------------------
    side: OrderSide
    #: Always positive. Direction lives in ``side``; a signed quantity here
    #: would give two places to express one fact and let them disagree.
    quantity: Money = Field(gt=0)
    #: The broker's quoted terms — 6.05 for an option, not 605.00.
    price: Money = Field(gt=0)
    commission: Money | None = None
    currency: str | None = None

    # --- when -------------------------------------------------------------
    executed_at: UtcDatetime
    broker_timestamp: UtcDatetime | None = None
    observed_at: UtcDatetime

    # --- provenance -------------------------------------------------------
    broker_source: Identifier
    #: Our own execution record, when one accounts for this fill. ``None`` for
    #: a fill the system has no execution for — an orphan, reported as such.
    execution_id: Identifier | None = None
    allocation_id: Identifier | None = None
    opportunity_id: Identifier | None = None
    provenance: AcquisitionProvenance = AcquisitionProvenance.UNKNOWN
    #: False when the broker supplied no execution id and identity had to be
    #: derived. Deduplication is weaker for such a fill, and that is recorded.
    identity_from_broker: bool = True
    simulated: bool = False

    @model_validator(mode="after")
    def _identity_matches_its_source(self) -> ObservedFill:
        has_broker_id = bool((self.broker_execution_id or "").strip())
        if has_broker_id is not self.identity_from_broker:
            raise ValueError(
                f"fill {self.fill_id} claims identity_from_broker={self.identity_from_broker} "
                f"but broker_execution_id is {self.broker_execution_id!r}"
            )
        return self

    @property
    def signed_quantity(self) -> Decimal:
        """Contracts added to the position: positive for a buy, negative for a sell."""
        return self.quantity if self.side is OrderSide.BUY else -self.quantity

    @property
    def gross_cost(self) -> Decimal | None:
        """Money this fill moved, multiplier included. ``None`` if unknown.

        Never assumes a multiplier of 100. A standard US equity option is 100
        and a system that assumed so would misprice the first one that is not —
        and would do it silently, which is the part that matters.
        """
        if self.multiplier is None:
            return None
        return self.price * self.quantity * Decimal(self.multiplier)

    @property
    def net_cost(self) -> Decimal | None:
        """``gross_cost`` including commission, when the commission is known."""
        gross = self.gross_cost
        if gross is None or self.commission is None:
            return None
        return gross + self.commission


# ---------------------------------------------------------------------------
# What this system believes it holds
# ---------------------------------------------------------------------------
class ExpectedPosition(ImmutableModel):
    """What confirmed fills say should exist, for one instrument.

    Not broker truth, and never presented as it. This is the internal ledger:
    the arithmetic of every confirmed fill this system knows about, which is
    exactly what makes a comparison against the broker meaningful. If it were
    derived from broker positions it would agree with them by construction and
    reconcile nothing.
    """

    position_id: Identifier
    account_reference: Identifier
    key: Identifier
    as_of: UtcDatetime
    schema_version: Identifier = POSITIONS_SCHEMA_VERSION

    underlying: Ticker
    asset_class: SecurityType
    symbol: Identifier
    contract_id: int | None = None
    expiration: date | None = None
    strike: Money | None = Field(default=None, gt=0)
    right: OptionRight | None = None
    multiplier: int | None = Field(default=None, ge=1)
    currency: str | None = None

    #: Net signed quantity: buys add, sells subtract. Zero is a real answer —
    #: a position opened and closed leaves zero, not nothing.
    quantity: Money
    bought_quantity: Money = Field(default=Decimal("0"), ge=0)
    sold_quantity: Money = Field(default=Decimal("0"), ge=0)

    #: Volume-weighted, in the broker's quoted terms.
    average_price: Money | None = Field(default=None, gt=0)
    #: The same figure as money for one contract, multiplier included, so it is
    #: directly comparable with the broker's ``average_cost``. ``None`` when no
    #: multiplier was reported — never an assumed 100.
    average_cost: Money | None = Field(default=None, gt=0)
    #: Money the confirmed fills moved, multiplier included.
    total_cost: Money | None = None
    commission: Money | None = None
    #: False when at least one contributing fill reported no commission, so a
    #: partial total is never read as a complete one.
    commission_complete: bool = False

    fill_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    allocation_ids: list[str] = Field(default_factory=list)
    opportunity_ids: list[str] = Field(default_factory=list)
    first_fill_at: UtcDatetime | None = None
    last_fill_at: UtcDatetime | None = None
    provenance: AcquisitionProvenance = AcquisitionProvenance.SYSTEM_EXECUTION

    @model_validator(mode="after")
    def _the_arithmetic_holds(self) -> ExpectedPosition:
        if self.quantity != self.bought_quantity - self.sold_quantity:
            raise ValueError(
                f"expected position {self.key}: net quantity {self.quantity} is not "
                f"{self.bought_quantity} bought less {self.sold_quantity} sold"
            )
        return self

    @model_validator(mode="after")
    def _a_position_rests_on_fills(self) -> ExpectedPosition:
        if self.quantity != 0 and not self.fill_ids:
            raise ValueError(
                f"expected position {self.key} claims {self.quantity} contract(s) with no "
                f"contributing fill. Only a confirmed broker fill establishes a position"
            )
        return self

    @property
    def is_open(self) -> bool:
        return self.quantity != 0

    @property
    def is_option(self) -> bool:
        return self.asset_class in (SecurityType.OPTION, SecurityType.FUTURE_OPTION)


class StrategyLegPosition(ImmutableModel):
    """One leg of a logical strategy position, expected against observed.

    Both sides are carried because both are true statements about different
    things, and the difference is the finding. ``observed_quantity`` is ``None``
    when broker data was unusable — distinct from ``0``, which is the broker
    saying it holds none.
    """

    leg_index: int = Field(ge=0)
    key: Identifier
    underlying: Ticker
    right: OptionRight | None = None
    strike: Money | None = Field(default=None, gt=0)
    expiration: date | None = None
    contract_id: int | None = None
    multiplier: int | None = Field(default=None, ge=1)

    expected_quantity: Money
    observed_quantity: Money | None = None

    @property
    def present(self) -> bool:
        """Whether the broker holds at least what was expected, in the right direction."""
        if self.observed_quantity is None:
            return False
        if self.expected_quantity == 0:
            return self.observed_quantity == 0
        if self.expected_quantity > 0:
            return self.observed_quantity >= self.expected_quantity
        return self.observed_quantity <= self.expected_quantity

    @property
    def absent(self) -> bool:
        return self.observed_quantity is not None and self.observed_quantity == 0


class StrategyPosition(ImmutableModel):
    """The logical view: one authorised structure, and what became of it.

    A straddle is one trade and two broker positions. Both facts are kept:
    ``legs`` names the individual contracts, and ``status`` says whether the
    structure the system authorised is actually there. Neither view replaces
    the other — collapsing two broker positions into one synthetic contract
    would invent an instrument that does not exist, and reporting only the legs
    would lose the fact that they were authorised as one trade with one
    maximum loss.
    """

    strategy_position_id: Identifier
    account_reference: Identifier
    as_of: UtcDatetime
    schema_version: Identifier = POSITIONS_SCHEMA_VERSION

    underlying: Ticker
    strategy: StrategyType
    status: StructureStatus
    #: Units of the structure Milestone 7 authorised. Copied, never recomputed.
    authorized_quantity: int = Field(ge=0)
    #: Units the confirmed fills actually established, from the weakest leg.
    filled_quantity: Money = Field(default=Decimal("0"))
    legs: list[StrategyLegPosition] = Field(default_factory=list)

    opportunity_id: Identifier
    allocation_id: Identifier | None = None
    execution_id: Identifier | None = None
    campaign_id: Identifier | None = None
    purchase_card_id: Identifier | None = None
    contract_selection_id: Identifier | None = None
    strategy_decision_id: Identifier | None = None
    research_report_id: Identifier | None = None
    broker_source: Identifier = "UNKNOWN"
    detail: str | None = None

    @model_validator(mode="after")
    def _the_status_matches_the_legs(self) -> StrategyPosition:
        """A structure's status is derived from its legs, and must agree with them.

        The case this exists for is ``PARTIAL``: a straddle whose call is held
        and whose put is not is neither complete nor absent, and either label
        would be a claim about a position that is not the one being held.
        """
        if not self.legs:
            return self
        if self.status is StructureStatus.UNKNOWN:
            return self
        present = [leg for leg in self.legs if leg.present]
        absent = [leg for leg in self.legs if leg.absent]
        if self.status is StructureStatus.COMPLETE and len(present) != len(self.legs):
            raise ValueError(
                f"strategy position {self.strategy_position_id} is recorded COMPLETE but "
                f"{len(self.legs) - len(present)} of {len(self.legs)} legs are not fully held"
            )
        if self.status is StructureStatus.MISSING and len(absent) != len(self.legs):
            raise ValueError(
                f"strategy position {self.strategy_position_id} is recorded MISSING but the "
                f"broker holds {len(self.legs) - len(absent)} of its legs"
            )
        if self.status is StructureStatus.PARTIAL and (
            len(present) == len(self.legs) or len(absent) == len(self.legs)
        ):
            raise ValueError(
                f"strategy position {self.strategy_position_id} is recorded PARTIAL but every "
                f"leg agrees; PARTIAL means some legs are held and some are not"
            )
        return self

    @property
    def complete(self) -> bool:
        return self.status is StructureStatus.COMPLETE

    def to_position_snapshot(
        self,
        *,
        legs: list[OptionLeg],
        source: str,
        market_value: Decimal | None = None,
        unrealized_pnl: Decimal | None = None,
        realized_pnl: Decimal | None = None,
        #: What the three figures above are in. Passed in with them, by the
        #: caller that read them off the broker, because they are one
        #: observation: a valuation labelled with a currency taken from
        #: somewhere else is a valuation nobody can check.
        currency: str | None = None,
        average_entry_price: Decimal | None = None,
        days_to_expiration: int | None = None,
        data_quality: DataQuality = DataQuality.OK,
    ) -> PositionSnapshot:
        """Project onto the Milestone 1 position boundary.

        The full record is the Milestone 9 artifact; this is the narrow shape
        the rest of the system was built against
        (``schemas/position_snapshot.json``), exactly as research, strategy and
        execution project onto their Milestone 1 shapes.

        Raises for ``MISSING`` and ``UNKNOWN``. :class:`PositionState` has no
        member meaning "we expected this and the broker does not report it" —
        that is a reconciliation finding, not a position state, and mapping it
        onto ``CLOSED`` or ``FAILED`` would turn a discrepancy into a tidy
        claim about a trade that was never made.
        """
        state = {
            StructureStatus.COMPLETE: PositionState.OPEN,
            StructureStatus.PARTIAL: PositionState.PARTIALLY_FILLED,
        }.get(self.status)
        if state is None:
            raise ValueError(
                f"strategy position {self.strategy_position_id} is {self.status.value}, which "
                f"the Milestone 1 PositionState vocabulary cannot express. A position the "
                f"broker does not report is a reconciliation finding, not a position"
            )
        return PositionSnapshot(
            position_id=self.strategy_position_id,
            purchase_card_id=self.purchase_card_id,
            as_of=self.as_of,
            state=state,
            underlying=self.underlying,
            strategy_type=self.strategy,
            legs=legs,
            quantity=int(self.filled_quantity),
            average_entry_price=average_entry_price,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            currency=currency,
            days_to_expiration=days_to_expiration,
            thesis_status=ThesisStatus.UNKNOWN,
            source=source,
            data_quality=data_quality,
        )
