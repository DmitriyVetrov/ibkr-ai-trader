"""Canonical risk and allocation input models.

This module holds everything *both* deterministic engines read, which is why it
sits in ``risk/`` rather than in ``allocation/``: the risk engine runs first and
may reject before a quantity is ever calculated, so it cannot depend on the
package that calculates one. The dependency runs one way — ``allocation``
imports ``risk``, never the reverse — and a test asserts it.

Five artifacts:

:class:`AccountSnapshot`
    What the broker said about the account at one instant, captured once and
    stored. The engines read *this*, never a broker, so no risk calculation can
    hide a live request behind itself (Milestone 2's one-reliable-round-trip
    constraint makes that unsafe as well as untestable).
:class:`CampaignSnapshot`
    The campaign's own capital envelope and what it has already committed.
    Independent of the account: a paper account holding a million euro is not
    permission to spend a million euro.
:class:`AllocationCandidate`
    One purchase candidate from Milestone 6, plus the strategy-level risk
    semantics that apply to it. The **only** thing the risk engine evaluates.
:class:`PortfolioExposure`
    Deterministic arithmetic over what the campaign already holds.
:class:`RiskEvaluation`
    The verdict, as a list of individual checks with actual and limit values,
    so an explanation is *derived* from structured data rather than written.

Three properties are enforced by the shapes here rather than by discipline:

* **Nothing is invented.** Every optional measurement is ``None`` when the data
  did not carry it. A candidate with no price says so; it does not report zero,
  and :class:`CandidatePrice` refuses to be marked available without a figure.
* **An unevaluated check is not a passed check.** :class:`RiskCheck` carries
  ``NOT_EVALUATED`` as a distinct outcome, because a limit nobody could compute
  has not been satisfied.
* **No order can be expressed.** There is no field anywhere here for an order
  type, a side, a limit price or a broker order id. Milestone 7 authorises
  capital; it does not construct an order.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    ConfidenceLevel,
    DailyPnLStatus,
    DataQuality,
    Direction,
    ExpectedMagnitude,
    LegAction,
    MarketHypothesis,
    MaxLossBasis,
    OptionRight,
    PriceSource,
    RiskCheckOutcome,
    RiskLimitScope,
    RiskOutcome,
    RiskReasonCode,
    StrategyType,
    TradingMode,
)
from trading_system.domain.models import (
    BrokerAccount,
    BrokerPosition,
    Identifier,
    ImmutableModel,
    Money,
    Ticker,
    UtcDatetime,
)
from trading_system.fx.convert import convert
from trading_system.fx.models import FxConversion, FxRateTable

__all__ = [
    "ALLOCATION_SCHEMA_VERSION",
    "AccountPosition",
    "AccountSnapshot",
    "AllocationCandidate",
    "CampaignPosition",
    "CampaignSnapshot",
    "CandidateLeg",
    "CandidatePrice",
    "OpportunityScore",
    "PortfolioExposure",
    "RiskCheck",
    "RiskEvaluation",
    "RiskLimits",
    "StrategyRiskProfile",
    "account_snapshot_identifier",
    "build_account_snapshot_payload",
    "opportunity_identifier",
]

#: Version of the Milestone 7 artifact shapes. Stamped onto every stored
#: record, so a past authorisation can never silently change meaning after a
#: schema change.
ALLOCATION_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Account state
# ---------------------------------------------------------------------------
class AccountPosition(ImmutableModel):
    """One position the broker reports, reduced to what risk actually needs.

    A projection rather than a copy: the risk engine needs to know that capital
    is committed and to what, not every field the broker returned. The full
    broker record stays in the broker layer where it came from.

    ``quantity`` is signed and a ``Decimal`` because that is what the broker
    reports; nothing here assumes a position is long or whole.
    """

    symbol: Identifier
    security_type: Identifier
    quantity: Money
    contract_id: int | None = None
    currency: str | None = None
    multiplier: int | None = Field(default=None, ge=1)
    average_cost: Money | None = None
    market_value: Money | None = None
    unrealized_pnl: Money | None = None

    expiration: date | None = None
    strike: Money | None = Field(default=None, gt=0)
    right: OptionRight | None = None

    @classmethod
    def of(cls, position: BrokerPosition) -> AccountPosition:
        return cls(
            symbol=position.symbol,
            security_type=position.security_type.value,
            quantity=position.quantity,
            contract_id=position.contract_id,
            currency=position.currency,
            multiplier=position.multiplier,
            average_cost=position.average_cost,
            market_value=position.market_value,
            unrealized_pnl=position.unrealized_pnl,
            expiration=position.expiration,
            strike=position.strike,
            right=position.right,
        )


class AccountSnapshot(ImmutableModel):
    """What the broker said about the account at one instant.

    The single boundary between broker reality and deterministic risk. It is
    captured by an explicit command, stored immutably, and read back by id —
    the engines never hold a broker and could not refresh this if they wanted
    to. That is deliberate twice over: it keeps risk reproducible, and it keeps
    Milestone 2's one-reliable-round-trip-per-connection constraint from
    turning a risk calculation into an unbounded broker request.

    Every monetary field the broker may omit is ``None``. "No buying power
    reported" and "zero buying power" are different facts, and an engine that
    read the first as the second would refuse every trade for the wrong reason
    — or, with the opposite sign convention, approve one it should not.
    """

    snapshot_id: Identifier
    as_of: UtcDatetime
    captured_at: UtcDatetime
    broker: Identifier
    account_id: Identifier
    currency: str = Field(min_length=3, max_length=8)
    trading_mode: TradingMode
    schema_version: Identifier = ALLOCATION_SCHEMA_VERSION

    cash: Money | None = None
    net_liquidation: Money | None = None
    buying_power: Money | None = None
    available_funds: Money | None = None
    excess_liquidity: Money | None = None
    unrealized_pnl: Money | None = None
    realized_pnl: Money | None = None

    #: Cash per currency, exactly as the broker reported it and never summed.
    #: An account holding EUR 5,000 and USD 0 records two figures; there is no
    #: total here, because a total would need a rate and would hide it.
    cash_by_currency: dict[str, Money] = Field(default_factory=dict)
    #: The rates the broker quoted **in the same read** as the balances above.
    #: Capturing them together is what makes a conversion point-in-time honest:
    #: a balance can never be converted with a rate from another instant,
    #: because there is no other instant's rate on this artifact to reach for.
    fx_rates: FxRateTable = Field(default_factory=FxRateTable)

    positions: list[AccountPosition] = Field(default_factory=list)
    #: Whether the broker connection that produced this was read-only. An
    #: authorisation resting on a snapshot from a writable connection is not
    #: less valid, but it is a different operational fact and is recorded.
    read_only: bool = True
    #: Orders this capture submitted. Structurally zero — capture is read-only —
    #: and recorded so the claim is evidence rather than an assertion.
    orders_submitted: int = Field(default=0, ge=0)
    simulated: bool = False

    @model_validator(mode="after")
    def _capture_submits_no_orders(self) -> AccountSnapshot:
        if self.orders_submitted != 0:
            raise ValueError(
                f"an account snapshot is a read-only capture, but this one reports "
                f"{self.orders_submitted} submitted order(s)"
            )
        return self

    @model_validator(mode="after")
    def _capture_cannot_precede_the_instant_it_describes(self) -> AccountSnapshot:
        if self.captured_at < self.as_of:
            raise ValueError(
                f"account snapshot {self.snapshot_id} claims to describe "
                f"{self.as_of.isoformat()} but was captured earlier, at "
                f"{self.captured_at.isoformat()}"
            )
        return self

    def known_at(self, instant: datetime) -> bool:
        """Point-in-time visibility: retrieval binds, not the instant described.

        A snapshot captured after ``instant`` was not available then, however
        recent the state it describes. This is the same rule the data layer
        applies, and it exists so a historical replay cannot allocate against
        an account balance nobody had yet seen.
        """
        return self.captured_at <= instant and self.as_of <= instant

    def age_seconds(self, instant: datetime) -> float:
        return (instant - self.as_of).total_seconds()

    @property
    def spendable(self) -> Decimal | None:
        """What the broker says is actually available, in the **base** currency.

        The most conservative of the figures the broker reported. ``None`` is
        propagated rather than replaced: an unknown balance is not a large one.

        This figure is in :attr:`currency`, which is the account's base
        currency and is *not* necessarily the currency a campaign trades in.
        Comparing it against an instrument price without going through
        :meth:`spendable_in` is the cross-currency comparison this milestone
        exists to remove, so the property name deliberately does not read like
        a number that is ready to compare.
        """
        figures = [
            value
            for value in (self.available_funds, self.buying_power, self.cash)
            if value is not None
        ]
        return min(figures) if figures else None

    def spendable_in(
        self, currency: str, *, as_of: datetime, max_rate_age_seconds: float | None
    ) -> FxConversion | None:
        """Available funds expressed in ``currency``, or the reason they are not.

        ``None`` means the broker reported no balance at all - a different fact
        from "we hold nothing" and from "we could not convert it", and the
        caller has an ``INVALID_ACCOUNT_SNAPSHOT`` check for it already.

        Otherwise the answer is always an :class:`FxConversion`, including when
        the currencies match: a same-currency result records the identity
        explicitly, so every comparison downstream reads its figure from a
        conversion rather than sometimes from one and sometimes from a raw
        balance.
        """
        available = self.spendable
        if available is None:
            return None
        return convert(
            available,
            from_currency=self.currency,
            to_currency=currency,
            rates=self.fx_rates,
            as_of=as_of,
            max_age_seconds=max_rate_age_seconds,
        )


def account_snapshot_identifier(
    *,
    broker: str,
    account_id: str,
    as_of: datetime,
    payload_digest: str,
    schema_version: str = ALLOCATION_SCHEMA_VERSION,
) -> str:
    """Derive an account snapshot's id from what it says, not from when we ran.

    Content-addressed for the same reason the data layer's snapshot ids are:
    capturing an unchanged account twice should be recognisable as one
    observation rather than recorded as two different balances.
    """
    digest = stable_hash(
        ["ACCOUNT_SNAPSHOT", schema_version, broker, account_id, as_of.isoformat(), payload_digest]
    )
    return f"account-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}"


def build_account_snapshot_payload(
    account: BrokerAccount, positions: list[BrokerPosition]
) -> list[object]:
    """The content an account snapshot's identity is derived from.

    Deliberately excludes observation clocks and our own verdicts, exactly as
    the data layer's payload hash does: the question is *is this the same
    account state?*, not *when did we look?*
    """
    return [
        account.account_id,
        account.currency,
        str(account.cash),
        str(account.net_liquidation),
        str(account.buying_power),
        str(account.available_funds),
        str(account.excess_liquidity),
        # The rates are part of the observation, not decoration on it. Two
        # captures of an unchanged balance at different rates convert to
        # different capital, so an id derived from the balance alone would
        # collide two genuinely different facts and the immutable store would
        # refuse the second - the same lesson allocation records about the
        # campaign's committed state and execution about the ledger's.
        sorted(f"{code}={rate}" for code, rate in account.exchange_rates.items()),
        sorted(f"{code}={amount}" for code, amount in account.cash_by_currency.items()),
        sorted(
            f"{p.symbol}|{p.security_type.value}|{p.contract_id}|{p.quantity}|{p.average_cost}"
            for p in positions
        ),
    ]


# ---------------------------------------------------------------------------
# Campaign state
# ---------------------------------------------------------------------------
class CampaignPosition(ImmutableModel):
    """Capital this campaign has already authorised for one opportunity.

    Note what this is *not*: it is not a broker position. Milestone 7 ends at
    an authorisation, and a reservation that was authorised but never executed
    still consumes campaign budget here. That is the conservative reading —
    double-authorising the same capital is the failure worth preventing — and
    releasing stale reservations belongs to the milestone that learns whether
    an order ever filled.
    """

    opportunity_id: Identifier
    allocation_id: Identifier
    symbol: Ticker
    strategy: StrategyType
    direction: Direction
    quantity: int = Field(ge=1)
    capital_committed: Money = Field(ge=0)
    max_loss: Money = Field(ge=0)
    authorized_at: UtcDatetime
    expiration: date | None = None
    contract_selection_id: Identifier | None = None
    research_report_id: Identifier | None = None

    def known_at(self, instant: datetime) -> bool:
        return self.authorized_at <= instant


class CampaignSnapshot(ImmutableModel):
    """The campaign's capital envelope and what it has already committed.

    Independent of the account by construction: there is no field here for
    broker equity, and nothing in the engine derives ``budget`` from one. The
    IBKR paper account may hold any balance at all; the campaign may spend
    ``budget`` less its reserve, and no more.

    ``allocated + available + reserve == budget`` holds exactly, in decimal, and
    is checked here rather than trusted — an accounting identity that only
    holds most of the time is not an accounting identity.

    **Which currency.** ``currency`` is what this campaign *trades* in, and
    every figure here is in it: the reservations replayed from the ledger are
    the cost of contracts, and a contract costs what it is quoted in. Where the
    operator's capital is held in a different currency, ``budget`` is the
    converted equivalent, ``declared_budget`` keeps the original, and ``fx``
    records the rate that connects them.

    A rate that moves therefore moves the envelope, and that is correct rather
    than a defect: EUR 5,000 buys a different number of dollars this week than
    last, while a reservation made last week stays the number of dollars it
    actually committed. Within one run the rate is fixed — it is captured with
    the account — so the arithmetic is reproducible.

    When no valid rate exists the budget is **not** converted, ``currency``
    stays the declared one, and the risk engine refuses every candidate before
    any of these figures is compared with a price.
    """

    campaign_id: Identifier
    as_of: UtcDatetime
    #: What every figure below is in — the currency this campaign trades.
    currency: str = Field(min_length=3, max_length=8)
    schema_version: Identifier = ALLOCATION_SCHEMA_VERSION

    budget: Money = Field(ge=0)
    #: Held back by policy and never allocated. A non-zero reserve is normal.
    reserve: Money = Field(ge=0)
    #: The envelope as configured, in ``declared_currency``, never converted in
    #: place. The operator's own money keeps its own currency in the record.
    declared_budget: Money | None = Field(default=None, ge=0)
    declared_currency: str | None = Field(default=None, min_length=3, max_length=8)
    #: The conversion between the two, or the reason there is none.
    fx: FxConversion | None = None
    budget_source: Identifier = "CONFIG"
    open_positions: list[CampaignPosition] = Field(default_factory=list)
    #: Realised profit and loss for the day, when it is tracked. ``None`` means
    #: *no figure*, which the daily-loss check records as NOT_EVALUATED or
    #: refuses outright — it is never read as "no losses today".
    realized_pnl_today: Money | None = None
    #: How far that figure can be relied on (Milestone 11). Three states, and
    #: the distinction between the last two is the point:
    #:
    #: ``TRACKED``
    #:     every position that closed today produced a usable result.
    #: ``UNKNOWN``
    #:     positions closed today and at least one produced no result. This is
    #:     an absence of knowledge about a day on which money moved, and the
    #:     engine is required to treat it differently from the case below.
    #: ``NOT_TRACKED``
    #:     no profit-and-loss ledger was consulted. The state of every snapshot
    #:     written before Milestone 11, which is why it is the default.
    daily_pnl_status: DailyPnLStatus = DailyPnLStatus.NOT_TRACKED
    #: Positions that closed today and produced no usable figure. Named rather
    #: than counted: "which one" is the first question anybody asks of an
    #: unknown daily result.
    unavailable_pnl_position_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _an_unknown_day_carries_no_figure(self) -> CampaignSnapshot:
        """``UNKNOWN`` and a number cannot both be true.

        Enforced rather than trusted, because the consumer that matters reads
        the number: a snapshot claiming ``UNKNOWN`` while carrying a comfortable
        realised figure would pass the daily loss limit on a day nobody could
        actually measure.
        """
        if (
            self.daily_pnl_status is not DailyPnLStatus.TRACKED
            and self.realized_pnl_today is not None
        ):
            raise ValueError(
                f"campaign {self.campaign_id} reports a realised figure of "
                f"{self.realized_pnl_today} while its daily profit-and-loss status is "
                f"{self.daily_pnl_status.value}. A figure that is not trustworthy is not "
                f"reported: the limit must see an absence, never a number that happens to "
                f"look safe"
            )
        if self.daily_pnl_status is DailyPnLStatus.TRACKED and self.realized_pnl_today is None:
            raise ValueError(
                f"campaign {self.campaign_id} claims a TRACKED daily result but carries no figure"
            )
        return self

    @model_validator(mode="after")
    def _reserve_fits_inside_the_budget(self) -> CampaignSnapshot:
        if self.reserve > self.budget:
            raise ValueError(
                f"campaign {self.campaign_id}: reserve {self.reserve} exceeds the budget "
                f"{self.budget}"
            )
        if self.allocated > self.allocatable:
            raise ValueError(
                f"campaign {self.campaign_id}: {self.allocated} is already committed but only "
                f"{self.allocatable} is allocatable; the accounting is inconsistent"
            )
        return self

    @model_validator(mode="after")
    def _one_reservation_per_opportunity(self) -> CampaignSnapshot:
        identifiers = [position.opportunity_id for position in self.open_positions]
        duplicates = sorted({i for i in identifiers if identifiers.count(i) > 1})
        if duplicates:
            raise ValueError(
                f"campaign {self.campaign_id} holds two reservations for the same "
                f"opportunity: {', '.join(duplicates)}"
            )
        return self

    @property
    def allocatable(self) -> Decimal:
        """The most that may ever be committed: the budget less its reserve."""
        return self.budget - self.reserve

    @property
    def allocated(self) -> Decimal:
        return sum((p.capital_committed for p in self.open_positions), Decimal("0"))

    @property
    def available(self) -> Decimal:
        """What is left to commit. Never negative; the validator guarantees it."""
        return self.allocatable - self.allocated

    @property
    def open_risk(self) -> Decimal:
        return sum((p.max_loss for p in self.open_positions), Decimal("0"))

    @property
    def position_count(self) -> int:
        return len(self.open_positions)

    def holds(self, opportunity_id: str) -> CampaignPosition | None:
        return next(
            (p for p in self.open_positions if p.opportunity_id == opportunity_id),
            None,
        )

    def committed_to(self, symbol: str) -> Decimal:
        wanted = symbol.strip().upper()
        return sum(
            (p.capital_committed for p in self.open_positions if p.symbol == wanted),
            Decimal("0"),
        )

    def committed_to_strategy(self, strategy: StrategyType) -> Decimal:
        return sum(
            (p.capital_committed for p in self.open_positions if p.strategy is strategy),
            Decimal("0"),
        )

    def committed_to_direction(self, direction: Direction) -> Decimal:
        return sum(
            (p.capital_committed for p in self.open_positions if p.direction is direction),
            Decimal("0"),
        )

    def positions_in(self, symbol: str) -> int:
        wanted = symbol.strip().upper()
        return sum(1 for p in self.open_positions if p.symbol == wanted)


# ---------------------------------------------------------------------------
# The candidate
# ---------------------------------------------------------------------------
class CandidateLeg(ImmutableModel):
    """One contract of a purchase candidate, as risk and allocation see it.

    Carried rather than referenced because Milestone 8 has to be able to build
    an order from an authorisation without re-running contract selection. What
    is *not* carried is anything resembling an instruction: no order type, no
    limit price, no side beyond the structural ``action`` the strategy defines.
    """

    leg_index: int = Field(ge=0)
    action: LegAction
    right: OptionRight
    ratio: int = Field(default=1, ge=1)
    underlying: Ticker
    expiration: date
    strike: Money = Field(gt=0)
    multiplier: int = Field(ge=1)
    contract_id: int
    trading_class: str = Field(min_length=1)
    exchange: str | None = None
    local_symbol: str | None = None
    currency: str | None = None

    #: The quote the unit cost was derived from, kept for audit. Optional
    #: because a leg may be quoted on one side only, in which case the cost
    #: estimate is unavailable rather than guessed.
    bid: Money | None = None
    ask: Money | None = None
    quote_as_of: UtcDatetime | None = None
    quote_snapshot_id: str | None = None


class CandidatePrice(ImmutableModel):
    """What one unit of the structure costs, and where that figure came from.

    ``available=False`` is a first-class state: with no ask on some leg the
    cost is *unknown*, every figure is ``None`` and ``unavailable_reason`` says
    which leg and why. Nothing is replaced with a zero, a midpoint conjured
    from one side, or the last price anyone happened to see.
    """

    available: bool = False
    source: PriceSource | None = None
    currency: str | None = None
    #: Cost of exactly one unit of the structure, all legs, including the
    #: multiplier. There is no quantity here: that is the allocation engine's
    #: answer and a field for it would invite pre-empting it.
    unit_cost: Money | None = Field(default=None, ge=0)
    max_leg_spread_pct: float | None = Field(default=None, ge=0)
    #: Oldest leg quote, so staleness is judged on the weakest link.
    quote_as_of: UtcDatetime | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _unavailable_carries_no_figure(self) -> CandidatePrice:
        if self.available:
            if self.unit_cost is None:
                raise ValueError("an available price must state a unit_cost")
            if self.source is None:
                raise ValueError("an available price must name the field it came from")
            return self
        if self.unit_cost is not None:
            raise ValueError(
                f"an unavailable price must carry no figure, but states {self.unit_cost}"
            )
        if not (self.unavailable_reason or "").strip():
            raise ValueError("an unavailable price must say why")
        return self


class StrategyRiskProfile(ImmutableModel):
    """The strategy-level risk semantics that apply to one candidate.

    Copied onto the candidate at build time rather than looked up inside the
    engine. Two reasons, and the second is the important one:

    * the engine stays a pure function of its inputs, so a stored evaluation
      can be reproduced without rebuilding a registry;
    * the limits that *actually applied* are recorded on the artifact, so a
      decision stays explainable after the configuration has moved on.

    ``max_loss_basis`` comes from the strategy's own structure. The engine
    never assumes one formula fits every strategy — a basis it cannot compute
    is a rejection, not an estimate.
    """

    strategy: StrategyType
    strategy_version: Identifier
    max_loss_basis: MaxLossBasis
    directional_view: Direction
    single_position: bool = True
    leg_count: int = Field(ge=1)

    dte_min: int = Field(ge=0)
    dte_max: int = Field(ge=0)
    #: The option price band, in the instrument's own currency (the
    #: campaign's target currency). Never converted.
    min_option_price: Money = Field(ge=0)
    max_option_price: Money = Field(ge=0)
    max_bid_ask_spread_pct: float = Field(ge=0.0)


class OpportunityScore(ImmutableModel):
    """A deterministic priority score, with every component shown.

    Not a model and not a probability: a weighted sum of structured upstream
    facts, recorded component by component so the total can be recomputed by
    hand from the stored record. Its only purpose is to order candidates when
    the budget cannot fund them all.
    """

    total: float = Field(ge=0.0, le=100.0)
    research_confidence: float = Field(ge=0.0, le=100.0)
    strategy_confidence: float = Field(ge=0.0, le=100.0)
    expected_magnitude: float = Field(ge=0.0, le=100.0)
    spread_quality: float = Field(ge=0.0, le=100.0)
    data_quality: float = Field(ge=0.0, le=100.0)
    weights: dict[str, float] = Field(default_factory=dict)


class AllocationCandidate(ImmutableModel):
    """One purchase candidate, and everything either engine may reason from.

    The Milestone 6 boundary made concrete: legs and the cost of one unit of
    the structure, plus the ids of every artifact it descends from. There is
    deliberately no quantity, no allocation and no money beyond the unit cost —
    those are what this milestone computes, and a field for them here would let
    an upstream stage pre-empt the deterministic layer.
    """

    opportunity_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    schema_version: Identifier = ALLOCATION_SCHEMA_VERSION

    strategy: StrategyType
    risk_profile: StrategyRiskProfile
    legs: list[CandidateLeg] = Field(min_length=1)
    expiration: date
    dte: int = Field(ge=0)
    price: CandidatePrice
    score: OpportunityScore

    hypothesis: MarketHypothesis | None = None
    research_confidence: ConfidenceLevel | None = None
    strategy_confidence: ConfidenceLevel | None = None
    expected_magnitude: ExpectedMagnitude | None = None
    horizon_days: int | None = Field(default=None, ge=1, le=365)

    #: Upstream data-quality verdicts, carried across verbatim. The risk engine
    #: reads them and never re-grades them: Milestone 3 owns that judgement.
    research_usable: bool = True
    data_quality: DataQuality = DataQuality.OK

    # --- provenance: ids, never copies -------------------------------------
    contract_selection_id: Identifier
    contract_run_id: Identifier
    strategy_decision_id: Identifier | None = None
    strategy_run_id: Identifier | None = None
    research_report_id: Identifier | None = None
    research_run_id: Identifier | None = None
    universe_run_id: Identifier | None = None
    input_snapshot_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _legs_agree_with_the_candidate(self) -> AllocationCandidate:
        underlyings = {leg.underlying for leg in self.legs}
        if underlyings != {self.symbol}:
            raise ValueError(f"every leg must reference {self.symbol}, got {sorted(underlyings)}")
        if any(leg.expiration != self.expiration for leg in self.legs):
            raise ValueError("every leg must share the candidate's expiration")
        if len(self.legs) != self.risk_profile.leg_count:
            raise ValueError(
                f"{self.strategy.value} has {self.risk_profile.leg_count} leg(s) but the "
                f"candidate carries {len(self.legs)}"
            )
        if self.risk_profile.strategy is not self.strategy:
            raise ValueError("the risk profile describes a different strategy")
        return self

    @property
    def multiplier(self) -> int:
        return self.legs[0].multiplier

    @property
    def currencies(self) -> set[str]:
        """Every currency the legs are quoted in. More than one is a rejection."""
        return {leg.currency for leg in self.legs if leg.currency}

    def known_at(self, instant: datetime) -> bool:
        """Whether every clock behind this candidate had passed at ``instant``.

        All of them, not whichever one is convenient: the candidate's own
        ``as_of``, the timestamp on the price the cost was derived from, and
        every leg's quote. A future price would otherwise reach the freshness
        check as a *negative* age and read as the freshest possible quote —
        which is exactly the shape of look-ahead bug that produces excellent
        backtests and means nothing.
        """
        if self.as_of > instant:
            return False
        if self.price.quote_as_of is not None and self.price.quote_as_of > instant:
            return False
        return all(leg.quote_as_of is None or leg.quote_as_of <= instant for leg in self.legs)


def opportunity_identifier(
    *,
    contract_selection_id: str,
    strategy_decision_id: str | None,
    research_report_id: str | None,
    schema_version: str = ALLOCATION_SCHEMA_VERSION,
) -> str:
    """Derive an opportunity's identity from the artifacts it descends from.

    This is what makes the stage idempotent. Two runs over the same research,
    the same strategy decision and the same contract selection produce the same
    opportunity id, so the second one recognises an existing reservation
    instead of committing the capital twice.

    Deliberately excludes the allocation run: an opportunity is a thing in the
    world, not an event in a run, and including the run would make every re-run
    look like a new opportunity — which is exactly the duplication this guards
    against.
    """
    digest = stable_hash(
        [
            "OPPORTUNITY",
            schema_version,
            contract_selection_id,
            strategy_decision_id or "",
            research_report_id or "",
        ]
    )
    return f"opportunity-{digest[:20]}"


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
class RiskCheck(ImmutableModel):
    """One limit, evaluated: what it was, what the candidate would make it.

    The unit an explanation is built from. ``actual`` and ``limit`` are strings
    so exact decimals survive JSON and so a check can describe a count, a
    percentage or an amount of money without three separate fields. The prose
    is generated *from* these; it is never the source of truth.
    """

    name: Identifier
    scope: RiskLimitScope
    outcome: RiskCheckOutcome
    actual: str | None = None
    limit: str | None = None
    reason_code: RiskReasonCode | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _a_failure_names_its_reason(self) -> RiskCheck:
        if self.outcome is RiskCheckOutcome.FAIL and self.reason_code is None:
            raise ValueError(f"failed check {self.name!r} must carry a machine-readable reason")
        if self.outcome is RiskCheckOutcome.FAIL and self.reason_code is RiskReasonCode.OK:
            raise ValueError(f"failed check {self.name!r} cannot report OK")
        if self.outcome is RiskCheckOutcome.NOT_EVALUATED and not (self.detail or "").strip():
            raise ValueError(
                f"check {self.name!r} was not evaluated and must say why; an unevaluated "
                f"limit is not a satisfied one"
            )
        return self

    def describe(self) -> str:
        """Human-readable, derived entirely from the structured fields."""
        if self.outcome is RiskCheckOutcome.NOT_EVALUATED:
            return f"{self.name}: not evaluated — {self.detail}"
        comparison = ""
        if self.actual is not None and self.limit is not None:
            comparison = f" (actual {self.actual}, limit {self.limit})"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"{self.name}: {self.outcome.value}{comparison}{suffix}"


class PortfolioExposure(ImmutableModel):
    """What the campaign would look like if this candidate were funded.

    Both sides are recorded — current and resulting — because "would this
    breach the limit" and "how close are we" are different questions and an
    operator asks both.
    """

    campaign_budget: Money = Field(ge=0)
    campaign_reserve: Money = Field(ge=0)
    campaign_allocated: Money = Field(ge=0)
    campaign_available: Money = Field(ge=0)
    campaign_open_risk: Money = Field(ge=0)
    position_count: int = Field(ge=0)

    underlying_exposure: Money = Field(default=Decimal("0"), ge=0)
    strategy_exposure: Money = Field(default=Decimal("0"), ge=0)
    directional_exposure: Money = Field(default=Decimal("0"), ge=0)
    positions_in_underlying: int = Field(default=0, ge=0)

    resulting_campaign_exposure: Money = Field(default=Decimal("0"), ge=0)
    resulting_campaign_risk: Money = Field(default=Decimal("0"), ge=0)
    resulting_underlying_exposure: Money = Field(default=Decimal("0"), ge=0)
    resulting_strategy_exposure: Money = Field(default=Decimal("0"), ge=0)
    resulting_directional_exposure: Money = Field(default=Decimal("0"), ge=0)
    resulting_position_count: int = Field(default=0, ge=0)


class RiskLimits(ImmutableModel):
    """Every limit in force for one evaluation, already resolved and converted.

    The *effective* value of each limit — the tighter of every layer that
    declares it — so a consumer never has to remember to intersect them and
    cannot forget to. ``scopes`` records which layer each effective value came
    from, so "why is the ceiling 1500 when risk.yaml says 1500 and the campaign
    says 1200" is answerable from the artifact.

    **Every money field below is in** :attr:`limit_currency`. That is not a
    detail: the limits are *declared* in the operator's own currency and the
    instruments are *quoted* in the currency they trade in, and this object is
    where the two are reconciled — once, explicitly, with the rate recorded.

    .. code-block:: text

        declared     1500 EUR    budget_currency, from campaign.yaml / risk.yaml
             |
             |  fx: one conversion, one rate, recorded
             v
        effective    1755 USD    target_currency == limit_currency
             |
             v
        compared against a USD option price

    When the conversion did **not** succeed, ``limit_currency`` stays the
    budget currency and :attr:`convertible` is false. The figures are then the
    declared ones and must not be compared against an instrument price; the
    engine's FX check fails the candidate before any comparison is reached, and
    :meth:`usable_against` exists so that claim is checkable rather than
    remembered.
    """

    #: What the limits were declared in — the account's own currency.
    budget_currency: str = Field(default="EUR", min_length=3, max_length=8)
    #: What this campaign trades in, and therefore what its limits have to be
    #: expressed in before any of them can be compared with a price.
    target_currency: str = Field(default="EUR", min_length=3, max_length=8)
    #: The currency the money fields on *this object* are actually in. Equal to
    #: ``target_currency`` when the conversion succeeded and to
    #: ``budget_currency`` when it did not — never silently one while claiming
    #: the other.
    limit_currency: str = Field(default="EUR", min_length=3, max_length=8)
    #: The single conversion applied to every money field, or the reason there
    #: is none. Recorded so a stored authorisation names the rate it rested on.
    fx: FxConversion | None = None
    #: Each money limit as it was configured, before conversion, keyed by field
    #: name. The source figure never loses its currency: 5000 stays 5000 EUR in
    #: the record even when the campaign spends 5850 USD of it.
    declared: dict[str, Money] = Field(default_factory=dict)

    campaign_budget: Money = Field(ge=0)
    campaign_reserve: Money = Field(ge=0)
    max_allocation_per_trade: Money = Field(ge=0)
    min_allocation_per_trade: Money = Field(ge=0)
    max_risk_per_trade: Money = Field(ge=0)
    max_total_open_risk: Money = Field(ge=0)
    max_daily_loss: Money = Field(ge=0)

    max_open_positions: int = Field(ge=0)
    max_positions_per_underlying: int = Field(ge=0)
    max_new_positions_per_run: int = Field(ge=0)
    max_contracts_per_trade: int = Field(ge=0)

    max_underlying_concentration_pct: float = Field(ge=0.0, le=100.0)
    max_strategy_concentration_pct: float = Field(ge=0.0, le=100.0)
    max_directional_exposure_pct: float = Field(ge=0.0, le=100.0)

    min_opportunity_score: float = Field(ge=0.0, le=100.0)
    max_market_data_age_seconds: int = Field(ge=0)
    max_account_snapshot_age_seconds: int = Field(ge=0)
    require_account_snapshot: bool = True
    require_daily_loss_tracking: bool = False
    #: Whether an *unknown* daily figure blocks new capital (Milestone 11).
    #: Separate from the flag above and defaulting to true: "we have never
    #: tracked this" and "we tracked it, positions closed today, and the number
    #: is not trustworthy" are different facts, and only the second is evidence
    #: that something is wrong.
    block_on_unknown_daily_loss: bool = True
    #: How old the rate converting these limits may be at the decision instant.
    max_fx_rate_age_seconds: int = Field(default=86400, ge=0)

    risk_config_version: Identifier
    campaign_id: Identifier
    scopes: dict[str, RiskLimitScope] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _the_stated_currency_matches_what_actually_happened(self) -> RiskLimits:
        budget, target = self.budget_currency.upper(), self.target_currency.upper()
        limit = self.limit_currency.upper()
        if self.convertible:
            if limit != target:
                raise ValueError(
                    f"limits claim to be in {limit} after a successful conversion into "
                    f"{target}. A limit labelled with a currency it is not in is exactly the "
                    f"defect this field exists to prevent"
                )
        elif limit != budget:
            raise ValueError(
                f"no valid conversion, so these limits are still the declared ones in "
                f"{budget}, but they are labelled {limit}"
            )
        if self.fx is not None and (
            self.fx.from_currency.upper() != budget or self.fx.to_currency.upper() != target
        ):
            raise ValueError(
                f"the recorded conversion is {self.fx.from_currency}->{self.fx.to_currency} "
                f"but these limits are declared in {budget} and traded in {target}"
            )
        return self

    @property
    def convertible(self) -> bool:
        """Whether the declared limits reached the traded currency at all."""
        return self.fx is not None and self.fx.ok

    @property
    def needs_conversion(self) -> bool:
        return self.budget_currency.upper() != self.target_currency.upper()

    def usable_against(self, instrument_currency: str | None) -> bool:
        """Whether these limits may be compared with a price in this currency.

        The one question every capacity check is really asking. It is false
        when the conversion failed, and false when the instrument is quoted in
        something other than what this campaign trades — both of which are
        rejections rather than approximations.
        """
        if not self.convertible:
            return False
        if instrument_currency is None:
            return True
        return instrument_currency.upper() == self.limit_currency.upper()

    def concentration_cap(self, percentage: float) -> Decimal:
        """A percentage of the campaign budget, exact to the cent.

        Rounded *down*, so a concentration ceiling is never quietly larger than
        the policy states.
        """
        cap = self.campaign_budget * Decimal(str(percentage)) / Decimal(100)
        return cap.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


class RiskEvaluation(ImmutableModel):
    """The deterministic verdict on one candidate.

    ``APPROVED`` here means *permitted*, not *funded*: it says the candidate
    breaches no limit, and the allocation engine then decides how many units
    the remaining budget actually supports. Keeping the two apart is what lets
    a rejection happen before any quantity is calculated, which is both cheaper
    and clearer than a quantity of zero standing in for a refusal.

    No AI agent can produce, amend or override one of these. There is no field
    for a model, a prompt or a rationale — only checks, and what they measured.
    """

    evaluation_id: Identifier
    opportunity_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    outcome: RiskOutcome
    schema_version: Identifier = ALLOCATION_SCHEMA_VERSION

    reason_codes: list[RiskReasonCode] = Field(min_length=1)
    checks: list[RiskCheck] = Field(default_factory=list)
    limits: RiskLimits
    exposure: PortfolioExposure
    trading_mode: TradingMode

    unit_cost: Money | None = Field(default=None, ge=0)
    unit_max_loss: Money | None = Field(default=None, ge=0)
    max_loss_basis: MaxLossBasis | None = None
    #: The instrument's own currency, which is what ``unit_cost`` is in.
    currency: str | None = None
    #: The conversion that carried this campaign's capital into the currency
    #: above, recorded on the verdict rather than only on the limits, so an
    #: explanation of a rejection can name the rate without loading anything
    #: else. ``None`` on artifacts written before the FX layer existed.
    fx: FxConversion | None = None

    campaign_snapshot_as_of: UtcDatetime | None = None
    account_snapshot_id: Identifier | None = None

    @model_validator(mode="after")
    def _codes_match_the_outcome(self) -> RiskEvaluation:
        """Mirrors the Milestone 1 ``RiskDecision`` invariant exactly."""
        if self.outcome is RiskOutcome.APPROVED:
            if self.reason_codes != [RiskReasonCode.OK]:
                raise ValueError("APPROVED must carry exactly one reason code: OK")
            failures = [c.name for c in self.checks if c.outcome is RiskCheckOutcome.FAIL]
            if failures:
                raise ValueError(
                    f"APPROVED contradicts its own checks, which failed: {', '.join(failures)}"
                )
        elif RiskReasonCode.OK in self.reason_codes:
            raise ValueError("REJECTED must not carry the OK reason code")
        return self

    @property
    def approved(self) -> bool:
        return self.outcome is RiskOutcome.APPROVED

    @property
    def failed_checks(self) -> list[RiskCheck]:
        return [check for check in self.checks if check.outcome is RiskCheckOutcome.FAIL]

    @property
    def unevaluated_checks(self) -> list[RiskCheck]:
        return [check for check in self.checks if check.outcome is RiskCheckOutcome.NOT_EVALUATED]

    def explain(self) -> str:
        """A readable explanation, generated from the checks.

        Never stored alongside the evaluation: a stored explanation is a second
        copy of the truth that can drift from the first.
        """
        header = (
            f"{self.symbol}: {self.outcome.value} ({', '.join(c.value for c in self.reason_codes)})"
        )
        if self.approved:
            lines = [header, f"  checks passed: {len(self.checks)}"]
        else:
            lines = [header]
            lines.extend(f"  FAILED  {check.describe()}" for check in self.failed_checks)
        lines.extend(f"  ------  {check.describe()}" for check in self.unevaluated_checks)
        return "\n".join(lines)
