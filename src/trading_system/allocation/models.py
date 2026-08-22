"""Canonical allocation artifacts.

What this package *produces*. Everything both engines *read* lives in
:mod:`trading_system.risk.models`, because the risk engine runs first and may
reject before a quantity exists, so it cannot depend on the package that
computes one.

Two artifacts:

:class:`CampaignAllocation`
    One authorisation, or one recorded refusal. The unit of the M7 → M8
    boundary: an APPROVED record carries legs, a quantity, the capital
    committed and the maximum loss, and references every upstream artifact by
    id. Milestone 8 can build an order from it without re-running research,
    strategy selection, contract selection or risk.
:class:`AllocationRunResult`
    The immutable record of one run: every candidate considered, funded or not,
    plus the campaign and account state the decisions rested on.

What is deliberately absent everywhere here — and asserted by tests:

* **No order semantics.** No order type, no side, no limit price, no
  time-in-force, no broker order id. An authorisation says *how much capital
  and risk is permitted*; how to execute that is Milestone 8's question, and a
  field for it here would let this artifact be mistaken for an instruction.
* **No AI.** No model name, no prompt version, no rationale the model wrote. A
  confidence *band* is carried because it feeds the deterministic ordering, but
  nothing an agent said can change a quantity or a limit.
* **No mutable history.** A later run creates a new record; it never edits an
  earlier one, however much the account has moved since.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    AllocationOutcome,
    AllocationPolicy,
    AllocationReason,
    AllocationRunStatus,
    Direction,
    MaxLossBasis,
    PriceSource,
    RiskOutcome,
    RiskReasonCode,
    StrategyType,
    TradingMode,
)
from trading_system.domain.models import (
    AllocationDecision,
    AllocationEntry,
    Identifier,
    ImmutableModel,
    Money,
    SystemVersions,
    Ticker,
    UtcDatetime,
)
from trading_system.fx.models import FxConversion
from trading_system.risk.models import (
    ALLOCATION_SCHEMA_VERSION,
    CampaignSnapshot,
    CandidateLeg,
    OpportunityScore,
    PortfolioExposure,
    RiskEvaluation,
)

__all__ = [
    "AllocationRunCounts",
    "AllocationRunResult",
    "CampaignAllocation",
    "QuantityCalculation",
    "allocation_identifier",
    "allocation_run_identifier",
    "campaign_fingerprint",
]


class QuantityCalculation(ImmutableModel):
    """How the quantity came to be what it is, term by term.

    Every ceiling that entered the minimum is recorded with the number of units
    it permitted, so "why two contracts and not three" is answerable from the
    stored record without re-deriving anything. ``binding_constraint`` names the
    one that actually bound — the cheapest possible answer to the question an
    operator actually asks.

    A quantity is always the **floor** of the minimum. It never rounds up, and
    it is always a whole number: there is no such thing as a third of an option
    contract, and a system that quietly rounded 2.9 up to 3 would commit 3% more
    capital than any limit authorised.
    """

    quantity: int = Field(ge=0)
    unit_cost: Money = Field(gt=0)
    unit_max_loss: Money = Field(ge=0)
    max_loss_basis: MaxLossBasis

    #: Units each ceiling permitted, before the minimum was taken.
    units_by_budget: int = Field(ge=0)
    units_by_risk: int = Field(ge=0)
    units_by_trade_cap: int = Field(ge=0)
    units_by_underlying_concentration: int = Field(ge=0)
    units_by_strategy_concentration: int = Field(ge=0)
    units_by_directional_exposure: int = Field(ge=0)
    units_by_contract_cap: int = Field(ge=0)
    #: ``None`` when the account reported no spendable figure — an unknown
    #: balance constrains nothing here because the guard already refused it.
    units_by_buying_power: int | None = Field(default=None, ge=0)

    binding_constraint: AllocationReason

    @model_validator(mode="after")
    def _quantity_is_the_floor_of_the_minimum(self) -> QuantityCalculation:
        ceilings = [
            self.units_by_budget,
            self.units_by_risk,
            self.units_by_trade_cap,
            self.units_by_underlying_concentration,
            self.units_by_strategy_concentration,
            self.units_by_directional_exposure,
            self.units_by_contract_cap,
        ]
        if self.units_by_buying_power is not None:
            ceilings.append(self.units_by_buying_power)
        smallest = min(ceilings)
        if self.quantity != smallest:
            raise ValueError(
                f"quantity {self.quantity} is not the minimum of its own ceilings "
                f"({smallest}); a quantity that exceeds a ceiling was not authorised by it"
            )
        return self

    @property
    def total_cost(self) -> Decimal:
        return self.unit_cost * Decimal(self.quantity)

    @property
    def total_max_loss(self) -> Decimal:
        return self.unit_max_loss * Decimal(self.quantity)


class CampaignAllocation(ImmutableModel):
    """One authorisation, or one recorded refusal to authorise.

    This is the artifact Milestone 8 consumes, and the boundary is exact: an
    ``APPROVED`` record says *this much capital and this much risk is permitted
    for this structure*. It is **not an order**. Nothing here names a price to
    pay, a side to trade or a venue; an execution engine reading this still has
    to decide how to work the order, and is still bound by everything recorded
    here while doing so.

    A record whose outcome is not ``APPROVED`` carries no quantity and no money,
    enforced by a validator — the same rule that stops a failed research report
    carrying a hypothesis, for the same reason. A refusal that quietly carried a
    size would be indistinguishable from an authorisation of that size.
    """

    allocation_id: Identifier
    run_id: Identifier
    opportunity_id: Identifier
    campaign_id: Identifier
    symbol: Ticker
    as_of: UtcDatetime
    decided_at: UtcDatetime
    outcome: AllocationOutcome
    schema_version: Identifier = ALLOCATION_SCHEMA_VERSION

    strategy: StrategyType
    strategy_version: Identifier | None = None
    #: What this structure's payoff expresses, from the strategy's own
    #: definition. Recorded rather than re-derived so directional exposure can
    #: be replayed from the ledger without rebuilding a strategy registry — the
    #: configuration that produced it may since have changed.
    direction: Direction
    legs: list[CandidateLeg] = Field(default_factory=list)
    expiration: date | None = None
    dte: int | None = Field(default=None, ge=0)

    # --- how much ----------------------------------------------------------
    quantity: int = Field(default=0, ge=0)
    unit_cost: Money | None = Field(default=None, ge=0)
    capital_committed: Money = Field(default=Decimal("0"), ge=0)
    unit_max_loss: Money | None = Field(default=None, ge=0)
    total_max_loss: Money = Field(default=Decimal("0"), ge=0)
    risk_basis: MaxLossBasis | None = None
    price_source: PriceSource | None = None
    currency: str | None = None
    calculation: QuantityCalculation | None = None

    # --- why ---------------------------------------------------------------
    risk_outcome: RiskOutcome
    reason_codes: list[RiskReasonCode] = Field(default_factory=list)
    allocation_reasons: list[AllocationReason] = Field(default_factory=list)
    opportunity_score: OpportunityScore
    rank: int = Field(ge=1)
    risk_evaluation: RiskEvaluation
    exposure_after: PortfolioExposure

    # --- provenance: ids, never copies -------------------------------------
    contract_selection_id: Identifier
    contract_run_id: Identifier
    strategy_decision_id: Identifier | None = None
    strategy_run_id: Identifier | None = None
    research_report_id: Identifier | None = None
    research_run_id: Identifier | None = None
    universe_run_id: Identifier | None = None
    account_snapshot_id: Identifier | None = None
    campaign_snapshot_as_of: UtcDatetime | None = None
    input_snapshot_ids: list[str] = Field(default_factory=list)

    trading_mode: TradingMode
    #: True when produced by a ``--dry-run``. Such a record is diagnostic and
    #: may never be read as an executable authorisation; the run that holds it
    #: is not persisted to authoritative history at all.
    dry_run: bool = False
    versions: SystemVersions
    detail: str | None = None

    @model_validator(mode="after")
    def _only_an_approval_commits_capital(self) -> CampaignAllocation:
        if self.outcome is AllocationOutcome.APPROVED:
            missing = [
                name
                for name, value in (
                    ("unit_cost", self.unit_cost),
                    ("unit_max_loss", self.unit_max_loss),
                    ("risk_basis", self.risk_basis),
                    ("calculation", self.calculation),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"an APPROVED allocation must state {', '.join(missing)}")
            if self.quantity < 1:
                raise ValueError(
                    "an APPROVED allocation must authorise at least one contract; zero units "
                    "is NO_TRADE, which is a different answer"
                )
            if not self.legs:
                raise ValueError("an APPROVED allocation must carry the legs it authorises")
            if self.risk_outcome is not RiskOutcome.APPROVED:
                raise ValueError(
                    "an allocation cannot be approved over a risk rejection; no layer may "
                    "override the risk engine"
                )
            return self

        if self.quantity != 0:
            raise ValueError(
                f"a {self.outcome.value} allocation must authorise nothing, but states "
                f"{self.quantity} contract(s)"
            )
        if self.capital_committed != 0 or self.total_max_loss != 0:
            raise ValueError(f"a {self.outcome.value} allocation must commit no capital")
        return self

    @model_validator(mode="after")
    def _the_arithmetic_holds(self) -> CampaignAllocation:
        if self.outcome is not AllocationOutcome.APPROVED:
            return self
        assert self.unit_cost is not None and self.unit_max_loss is not None  # narrowing
        units = Decimal(self.quantity)
        if self.capital_committed != self.unit_cost * units:
            raise ValueError(
                f"capital_committed {self.capital_committed} is not {self.unit_cost} x "
                f"{self.quantity}"
            )
        if self.total_max_loss != self.unit_max_loss * units:
            raise ValueError(
                f"total_max_loss {self.total_max_loss} is not {self.unit_max_loss} x "
                f"{self.quantity}"
            )
        return self

    @model_validator(mode="after")
    def _the_verdict_matches_its_evaluation(self) -> CampaignAllocation:
        if self.risk_evaluation.opportunity_id != self.opportunity_id:
            raise ValueError("the risk evaluation is about a different opportunity")
        if self.risk_evaluation.outcome is not self.risk_outcome:
            raise ValueError("the recorded risk outcome contradicts the evaluation it cites")
        # A risk rejection may be recorded as REJECTED or, when the reason is
        # that this exact opportunity already holds a reservation, as
        # ALREADY_ALLOCATED. The two are different facts an operator needs to
        # tell apart — "a limit refused this" and "we did this already" — and
        # neither authorises capital, which is the property that matters here.
        if self.risk_outcome is RiskOutcome.REJECTED and self.outcome not in (
            AllocationOutcome.REJECTED,
            AllocationOutcome.ALREADY_ALLOCATED,
        ):
            raise ValueError(
                f"the risk engine rejected this candidate, so the allocation outcome must be "
                f"REJECTED or ALREADY_ALLOCATED, not {self.outcome.value}"
            )
        return self

    @property
    def approved(self) -> bool:
        return self.outcome is AllocationOutcome.APPROVED

    def to_allocation_entry(self) -> AllocationEntry:
        """Project onto the Milestone 1 allocation boundary."""
        return AllocationEntry(
            opportunity_id=self.opportunity_id,
            ticker=self.symbol,
            rank=self.rank,
            opportunity_score=self.opportunity_score.total,
            allocated=self.capital_committed,
        )


class AllocationRunCounts(ImmutableModel):
    """How many candidates reached each outcome.

    Derived from the run's own decisions, which is why elapsed time is
    deliberately absent: a measured duration is a fact about the machine, and
    storing it in a content-addressed record would make two otherwise identical
    runs differ.
    """

    candidates_considered: int = Field(default=0, ge=0)
    approved: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    no_trade: int = Field(default=0, ge=0)
    already_allocated: int = Field(default=0, ge=0)


class AllocationRunResult(ImmutableModel):
    """The immutable record of one allocation run.

    Deterministic end to end: no model is consulted, no clock is read inside a
    calculation, and given the same candidates, the same campaign and account
    snapshots, the same configuration and the same ``as_of``, this record is
    identical apart from its own generation clock.

    ``allocated + reserve + available == budget`` holds exactly, in decimal, and
    is checked here — an accounting identity that only holds most of the time
    is not an accounting identity.
    """

    run_id: Identifier
    campaign_id: Identifier
    as_of: UtcDatetime
    generated_at: UtcDatetime
    status: AllocationRunStatus
    schema_version: Identifier = ALLOCATION_SCHEMA_VERSION

    policy: AllocationPolicy
    policy_version: Identifier
    trading_mode: TradingMode
    dry_run: bool = False

    #: Campaign state as it stood *before* this run committed anything.
    campaign_before: CampaignSnapshot
    account_snapshot_id: Identifier | None = None

    #: What every monetary figure on this run is in: the campaign's **traded**
    #: currency. Where the operator's capital is held in another currency,
    #: ``budget`` below is the converted equivalent and ``declared_budget``
    #: keeps the original. Both are recorded because they answer different
    #: questions - what the operator has, and what this campaign may spend.
    currency: str = Field(default="EUR", min_length=3, max_length=8)
    budget: Money = Field(ge=0)
    reserve: Money = Field(ge=0)
    #: The envelope as configured, in ``declared_currency``. Never converted in
    #: place: the source figure keeps its currency for the life of the record.
    declared_budget: Money | None = Field(default=None, ge=0)
    declared_currency: str | None = Field(default=None, min_length=3, max_length=8)
    #: The conversion that produced ``budget`` from ``declared_budget``, rate
    #: and source included, so the arithmetic can be checked by hand. ``None``
    #: on runs from before the FX layer, and on runs that needed no conversion
    #: only in the sense that the identity is still recorded rather than
    #: omitted.
    fx: FxConversion | None = None
    allocated_before: Money = Field(ge=0)
    allocated_this_run: Money = Field(default=Decimal("0"), ge=0)
    available_after: Money = Field(ge=0)
    risk_authorized_this_run: Money = Field(default=Decimal("0"), ge=0)

    allocations: list[CampaignAllocation] = Field(default_factory=list)
    counts: AllocationRunCounts = Field(default_factory=AllocationRunCounts)

    contract_run_id: Identifier | None = None
    strategy_run_id: Identifier | None = None
    research_run_id: Identifier | None = None
    versions: SystemVersions
    status_detail: str | None = None

    @model_validator(mode="after")
    def _the_campaign_accounting_balances(self) -> AllocationRunResult:
        committed = sum((a.capital_committed for a in self.allocations if a.approved), Decimal("0"))
        if committed != self.allocated_this_run:
            raise ValueError(
                f"approved allocations total {committed} but the run reports "
                f"{self.allocated_this_run}"
            )
        total = self.allocated_before + self.allocated_this_run + self.available_after
        allocatable = self.budget - self.reserve
        if total != allocatable:
            raise ValueError(
                f"allocated ({self.allocated_before} + {self.allocated_this_run}) plus "
                f"available ({self.available_after}) is {total}, which does not equal the "
                f"allocatable budget {allocatable} (budget {self.budget} less reserve "
                f"{self.reserve})"
            )
        return self

    @model_validator(mode="after")
    def _one_decision_per_opportunity(self) -> AllocationRunResult:
        identifiers = [a.opportunity_id for a in self.allocations]
        duplicates = sorted({i for i in identifiers if identifiers.count(i) > 1})
        if duplicates:
            raise ValueError(
                f"one decision per opportunity per run; duplicated: {', '.join(duplicates)}"
            )
        wrong_run = sorted({a.opportunity_id for a in self.allocations if a.run_id != self.run_id})
        if wrong_run:
            raise ValueError(f"allocations from another run: {', '.join(wrong_run)}")
        return self

    @model_validator(mode="after")
    def _a_dry_run_is_marked_on_every_record(self) -> AllocationRunResult:
        """A diagnostic run must be unmistakable at every level of the record.

        Marking only the run would let one authorisation be lifted out of it
        and read as executable. Every allocation carries the flag too.
        """
        if any(a.dry_run is not self.dry_run for a in self.allocations):
            raise ValueError(
                "every allocation in a run must carry the same dry_run flag as the run; a "
                "diagnostic result must never be mistakable for an authorisation"
            )
        return self

    @property
    def approvals(self) -> list[CampaignAllocation]:
        """Authorisations a downstream stage may act on, in a stable order."""
        return sorted(
            (a for a in self.allocations if a.approved),
            key=lambda a: (a.rank, a.symbol),
        )

    @property
    def symbols(self) -> list[str]:
        return [a.symbol for a in self.allocations]

    def allocation(self, symbol: str) -> CampaignAllocation | None:
        wanted = symbol.strip().upper()
        return next((a for a in self.allocations if a.symbol == wanted), None)

    def by_opportunity(self, opportunity_id: str) -> CampaignAllocation | None:
        return next((a for a in self.allocations if a.opportunity_id == opportunity_id), None)

    def to_allocation_decision(self) -> AllocationDecision:
        """Project onto the Milestone 1 workflow boundary.

        The full run is the audit artifact; this is the narrow contract the
        rest of the chain was built against (``schemas/allocation_decision.json``),
        exactly as a research report projects onto ``research_report.json``.

        ``reserve`` there means *everything not allocated* — the policy
        reserve plus whatever was left unspent — because that model's invariant
        is ``allocated + reserve == total_budget``. The two figures are kept
        apart on this record, where the distinction is meaningful.

        The currency is this run's own: the campaign's **traded** currency, and
        the one every figure on the run is already in. The declared budget and
        the rate that converted it stay on the run, which is the audit artifact;
        the narrow boundary carries what a downstream stage has to spend.
        """
        entries = [a.to_allocation_entry() for a in self.approvals]
        allocated = sum((e.allocated for e in entries), Decimal("0"))
        return AllocationDecision(
            allocation_id=self.run_id,
            campaign_id=self.campaign_id,
            as_of=self.as_of,
            currency=self.currency,
            total_budget=self.budget,
            allocated=allocated + self.allocated_before,
            reserve=self.budget - allocated - self.allocated_before,
            entries=entries,
            versions=self.versions,
        )


# ---------------------------------------------------------------------------
# Derived identifiers
# ---------------------------------------------------------------------------
def allocation_identifier(
    *,
    run_id: str,
    opportunity_id: str,
    outcome: AllocationOutcome,
    quantity: int,
    capital_committed: Decimal,
    schema_version: str = ALLOCATION_SCHEMA_VERSION,
) -> str:
    """Derive an allocation's identifier from what it authorised.

    Content-addressed, and deliberately excluding every runtime measurement:
    two runs reaching an identical authorisation over identical inputs produce
    an identical id, so a re-run is idempotent rather than a second record of
    one event.
    """
    digest = stable_hash(
        [
            "CAMPAIGN_ALLOCATION",
            schema_version,
            run_id,
            opportunity_id,
            outcome.value,
            str(quantity),
            str(capital_committed),
        ]
    )
    return f"allocation-{digest[:20]}"


def campaign_fingerprint(campaign: CampaignSnapshot) -> str:
    """What the campaign already held, as one deterministic string.

    Part of a run's identity for the same reason the account snapshot is: the
    same candidates against a campaign that has since committed capital are a
    *different decision*, and they reach different answers for good reasons. A
    run id that ignored the ledger would collide the two.

    Derived from the reservations rather than from a running total, so it
    changes exactly when the campaign's commitments do.
    """
    return stable_hash(
        [
            campaign.campaign_id,
            str(campaign.budget),
            str(campaign.reserve),
            sorted(
                f"{p.opportunity_id}|{p.capital_committed}|{p.max_loss}"
                for p in campaign.open_positions
            ),
        ]
    )


def allocation_run_identifier(
    *,
    as_of: datetime,
    config_version: str,
    policy_version: str,
    contract_run_id: str | None,
    account_snapshot_id: str | None,
    campaign_id: str,
    campaign_state: str,
    opportunity_ids: list[str],
) -> str:
    """Derive a run identifier from everything that determines the run.

    Includes the account snapshot *and* the campaign's committed state: the
    same candidates against a different balance, or against a campaign that has
    since reserved capital, are a different decision. A run id that ignored
    either would collide two runs that reached different answers.

    A re-run over genuinely unchanged inputs still lands on the same id, which
    is what makes an unchanged re-run idempotent rather than a second record of
    one event.
    """
    digest = stable_hash(
        [
            "ALLOCATION_RUN",
            config_version,
            policy_version,
            campaign_id,
            campaign_state,
            contract_run_id or "",
            account_snapshot_id or "",
            as_of.isoformat(),
            sorted(opportunity_ids),
        ]
    )
    return f"allocation-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"
