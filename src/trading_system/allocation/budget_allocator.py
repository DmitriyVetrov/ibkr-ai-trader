"""The deterministic allocation engine.

Answers the second question — *given that this position is permitted, how many
units may we commit?* The first question is the risk engine's, and a candidate
reaches this module only after it has been approved there. No layer here can
override a risk rejection; a rejected candidate is recorded as rejected and
never sized.

The policy is ``PRIORITY_FIRST_FIT`` and it is deliberately the simplest thing
that is correct:

1. score every candidate deterministically and order by that score;
2. evaluate risk for each in turn, against the campaign as it stands *including
   what this run has already committed*;
3. take the floor of the tightest quantity ceiling;
4. commit, and carry the reduced budget into the next candidate;
5. stop when the budget, the position count or the per-run cap is exhausted.

Sequential rather than simultaneous, on purpose. A solver that optimised the
whole set at once would produce an allocation that is better by some measure
nobody stated and impossible to explain one line at a time — and every
opportunity to explain an allocation one line at a time is worth more than the
marginal efficiency at this stage.

Two invariants are enforced by construction rather than by care:

* **Quantity never rounds up.** :func:`max_units` computes an exact floor and
  then *verifies* it by multiplication, so a division that rounded the wrong
  way in the last digit cannot produce a position one contract larger than any
  limit authorised.
* **The accounting stays consistent.** ``allocated + available`` is recomputed
  from the committed positions after every step, and the run record refuses to
  be constructed if it does not equal the allocatable budget.

Nothing here reads a clock, opens a socket, touches a repository or consults a
model. The decision instant is passed in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from trading_system.allocation.models import QuantityCalculation
from trading_system.domain.enums import (
    AllocationOutcome,
    AllocationReason,
    RiskOutcome,
    RiskReasonCode,
    TradingMode,
)
from trading_system.risk.engine import RiskEngine
from trading_system.risk.models import (
    AccountSnapshot,
    AllocationCandidate,
    CampaignPosition,
    CampaignSnapshot,
    RiskEvaluation,
    RiskLimits,
)

__all__ = ["AllocationEngine", "CandidateAllocation", "max_units"]


def max_units(limit: Decimal, unit: Decimal) -> int:
    """How many whole units of ``unit`` fit inside ``limit``. Never rounds up.

    Exact, and verified rather than trusted. ``Decimal`` division is correctly
    rounded to the working precision, which for realistic money is exact — but
    "realistic" is an assumption, and the cost of it being wrong is a position
    one contract larger than any limit permitted. So the quotient is floored,
    then corrected by multiplication in both directions: decrement while the
    product exceeds the limit, increment while one more still fits. Both loops
    are bounded by a single step for any input this system can produce; they
    exist so the result is a fact rather than an inference.

    A non-positive ``unit`` returns 0. A structure that costs nothing is a data
    fault the guards have already refused, and dividing by it here would raise
    inside an engine that must always return an answer.
    """
    if unit <= 0 or limit <= 0:
        return 0
    quotient = int((limit / unit).to_integral_value(rounding=ROUND_FLOOR))
    while quotient > 0 and Decimal(quotient) * unit > limit:
        quotient -= 1
    while Decimal(quotient + 1) * unit <= limit:
        quotient += 1
    return quotient


@dataclass(frozen=True, slots=True)
class CandidateAllocation:
    """What the engine decided about one candidate, before it becomes a record.

    A plain dataclass rather than a model: this is the engine's return value,
    and the persisted artifact is assembled from it by the service, which is
    also the layer that knows the run id and the version stamps.
    """

    candidate: AllocationCandidate
    outcome: AllocationOutcome
    evaluation: RiskEvaluation
    rank: int
    quantity: int = 0
    calculation: QuantityCalculation | None = None
    reasons: tuple[AllocationReason, ...] = ()
    detail: str | None = None

    @property
    def capital_committed(self) -> Decimal:
        return self.calculation.total_cost if self.calculation else Decimal("0")

    @property
    def total_max_loss(self) -> Decimal:
        return self.calculation.total_max_loss if self.calculation else Decimal("0")


class AllocationEngine:
    """Deterministic capital allocation across a finite campaign budget.

    Constructed from limits and a risk engine. There is nowhere here a broker,
    a repository or a model could be reached from — the composition root itself
    is the evidence, which is what makes "allocation submits no orders" a
    structural property rather than something a test has to catch.
    """

    def __init__(self, limits: RiskLimits, risk_engine: RiskEngine) -> None:
        self._limits = limits
        self._risk = risk_engine

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    def allocate(
        self,
        candidates: Sequence[AllocationCandidate],
        campaign: CampaignSnapshot,
        *,
        as_of: datetime,
        account: AccountSnapshot | None = None,
        trading_mode: TradingMode = TradingMode.PAPER,
        live_guards_satisfied: bool = False,
    ) -> list[CandidateAllocation]:
        """Distribute what is left of the campaign budget across candidates.

        The campaign snapshot passed in is the state *before* this run. As
        candidates are funded, a working snapshot is advanced so each later
        candidate is evaluated against a campaign that already reflects the
        earlier commitments — otherwise a run could authorise the same euro
        several times over, which is exactly the failure the whole accounting
        exists to prevent.
        """
        ordered = order_candidates(candidates)
        working = campaign
        results: list[CandidateAllocation] = []
        funded = 0

        for rank, candidate in enumerate(ordered, start=1):
            evaluation = self._risk.evaluate(
                candidate,
                working,
                as_of=as_of,
                account=account,
                trading_mode=trading_mode,
                live_guards_satisfied=live_guards_satisfied,
                new_positions_this_run=funded,
            )

            if evaluation.outcome is RiskOutcome.REJECTED:
                results.append(
                    CandidateAllocation(
                        candidate=candidate,
                        outcome=_rejection_outcome(evaluation),
                        evaluation=evaluation,
                        rank=rank,
                        detail=_first_detail(evaluation),
                    )
                )
                continue

            decision = self._size(candidate, working, account, evaluation, rank, as_of)
            results.append(decision)

            if decision.outcome is AllocationOutcome.APPROVED:
                working = _with_reservation(working, decision, as_of=as_of)
                funded += 1

        return results

    # --- sizing ------------------------------------------------------------
    def _size(
        self,
        candidate: AllocationCandidate,
        campaign: CampaignSnapshot,
        account: AccountSnapshot | None,
        evaluation: RiskEvaluation,
        rank: int,
        as_of: datetime,
    ) -> CandidateAllocation:
        """Compute the quantity as the floor of the tightest ceiling.

        Every ceiling here is in the campaign's traded currency. The limits
        arrived converted, the campaign's own figures are already in it, and
        the account's balance - the one input still in another currency - is
        converted below at the rate captured with it.
        """
        limits = self._limits
        unit_cost = evaluation.unit_cost
        unit_max_loss = evaluation.unit_max_loss
        # Both are guaranteed by an APPROVED evaluation — the risk engine
        # cannot approve a candidate whose price or loss model is missing.
        assert unit_cost is not None and unit_max_loss is not None

        budget_room = campaign.available
        risk_room = limits.max_total_open_risk - campaign.open_risk
        trade_cap = limits.max_allocation_per_trade
        per_trade_risk_units = max_units(limits.max_risk_per_trade, unit_max_loss)

        underlying_room = limits.concentration_cap(
            limits.max_underlying_concentration_pct
        ) - campaign.committed_to(candidate.symbol)
        strategy_room = limits.concentration_cap(
            limits.max_strategy_concentration_pct
        ) - campaign.committed_to_strategy(candidate.strategy)
        view = candidate.risk_profile.directional_view
        directional_room = limits.concentration_cap(
            limits.max_directional_exposure_pct
        ) - campaign.committed_to_direction(view)

        units_by_budget = max_units(budget_room, unit_cost)
        units_by_risk = min(max_units(risk_room, unit_max_loss), per_trade_risk_units)
        units_by_trade_cap = max_units(trade_cap, unit_cost)
        units_by_underlying = max_units(underlying_room, unit_cost)
        units_by_strategy = max_units(strategy_room, unit_cost)
        units_by_direction = max_units(directional_room, unit_cost)
        units_by_contract_cap = limits.max_contracts_per_trade

        # The account's balance is in the account's own currency and the unit
        # cost is in the traded one. Dividing one by the other without a rate
        # is how a position gets sized by the exchange rate: EUR 5,000 of
        # buying power would authorise as many contracts as USD 5,000 does,
        # which is wrong by 17% at today's rate and wrong by an unbounded
        # amount at some future one.
        #
        # A conversion that failed produces no ceiling *and no size*: the risk
        # engine has already rejected this candidate with FX_RATE_UNAVAILABLE,
        # and sizing it against an unconverted balance here would compute a
        # quantity for a trade that is not permitted.
        units_by_buying_power: int | None = None
        if account is not None and account.spendable is not None:
            conversion = account.spendable_in(
                limits.target_currency,
                as_of=as_of,
                max_rate_age_seconds=float(limits.max_fx_rate_age_seconds),
            )
            units_by_buying_power = (
                max_units(conversion.value, unit_cost)
                if conversion is not None and conversion.ok
                else 0
            )

        ceilings: list[tuple[int, AllocationReason]] = [
            (units_by_budget, AllocationReason.LIMITED_BY_BUDGET),
            (units_by_risk, AllocationReason.LIMITED_BY_RISK),
            (units_by_trade_cap, AllocationReason.LIMITED_BY_TRADE_CAP),
            (units_by_underlying, AllocationReason.LIMITED_BY_CONCENTRATION),
            (units_by_strategy, AllocationReason.LIMITED_BY_CONCENTRATION),
            (units_by_direction, AllocationReason.LIMITED_BY_CONCENTRATION),
            (units_by_contract_cap, AllocationReason.LIMITED_BY_CONTRACT_CAP),
        ]
        if units_by_buying_power is not None:
            ceilings.append((units_by_buying_power, AllocationReason.LIMITED_BY_BUYING_POWER))

        quantity = min(units for units, _ in ceilings)
        binding = _binding_constraint(ceilings, quantity)

        calculation = QuantityCalculation(
            quantity=quantity,
            unit_cost=unit_cost,
            unit_max_loss=unit_max_loss,
            max_loss_basis=candidate.risk_profile.max_loss_basis,
            units_by_budget=units_by_budget,
            units_by_risk=units_by_risk,
            units_by_trade_cap=units_by_trade_cap,
            units_by_underlying_concentration=units_by_underlying,
            units_by_strategy_concentration=units_by_strategy,
            units_by_directional_exposure=units_by_direction,
            units_by_contract_cap=units_by_contract_cap,
            units_by_buying_power=units_by_buying_power,
            binding_constraint=binding,
        )

        # Defence in depth rather than an expected path. Every ceiling divided
        # by above is also checked against one whole unit by the risk engine,
        # so an APPROVED evaluation implies at least one unit fits — which is
        # why a candidate that cannot afford a single contract comes back with
        # the limit that bound named, rather than as a bare zero nobody can act
        # on. If that invariant is ever broken, failing closed here turns an
        # impossible state into NO_TRADE instead of a position of unknown size.
        if quantity < 1:
            return CandidateAllocation(
                candidate=candidate,
                outcome=AllocationOutcome.NO_TRADE,
                evaluation=evaluation,
                rank=rank,
                reasons=(binding,),
                detail=(
                    f"no whole contract fits: one unit costs {unit_cost} and the tightest "
                    f"constraint ({binding.value}) permits none. A valid strategy is not an "
                    f"entitlement to capital"
                ),
            )

        total_cost = calculation.total_cost
        if total_cost < limits.min_allocation_per_trade:
            return CandidateAllocation(
                candidate=candidate,
                outcome=AllocationOutcome.NO_TRADE,
                evaluation=evaluation,
                rank=rank,
                reasons=(binding,),
                detail=(
                    f"{quantity} contract(s) at {unit_cost} is {total_cost}, below the "
                    f"{limits.min_allocation_per_trade} minimum allocation. A position too "
                    f"small to be worth holding is not held"
                ),
            )

        # The binding constraint is always recorded. ``FULL_ALLOCATION``
        # accompanies it only when the *size ceiling* bound — that is, nothing
        # scarce limited this position and it reached the configured maximum
        # contract count. Every other case was limited by something, and saying
        # which is more useful than saying that it was.
        reasons: tuple[AllocationReason, ...] = (
            (AllocationReason.FULL_ALLOCATION, binding)
            if binding is AllocationReason.LIMITED_BY_CONTRACT_CAP
            else (binding,)
        )

        return CandidateAllocation(
            candidate=candidate,
            outcome=AllocationOutcome.APPROVED,
            evaluation=evaluation,
            rank=rank,
            quantity=quantity,
            calculation=calculation,
            reasons=reasons,
            detail=(
                f"{quantity} contract(s) at {unit_cost} commits {total_cost} and risks "
                f"{calculation.total_max_loss}; bound by {binding.value}"
            ),
        )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def order_candidates(candidates: Sequence[AllocationCandidate]) -> list[AllocationCandidate]:
    """Highest score first, ties broken explicitly.

    Every tie breaks on a stated key — score, then symbol, then opportunity id
    — and never on the order the candidates happened to arrive in. Two runs
    over the same set therefore fund the same candidates in the same order,
    which is the whole of what "deterministic allocation" means when the budget
    cannot cover everything.
    """
    return sorted(
        candidates,
        key=lambda c: (-c.score.total, c.symbol, c.opportunity_id),
    )


def _binding_constraint(
    ceilings: list[tuple[int, AllocationReason]], quantity: int
) -> AllocationReason:
    """Which ceiling actually bound, in a fixed order for reproducibility.

    Several ceilings can permit the same number. The first one in the declared
    order wins, so the answer is stable rather than dependent on dictionary
    iteration or on which comparison happened to run last.
    """
    for units, reason in ceilings:
        if units == quantity:
            return reason
    return AllocationReason.FULL_ALLOCATION


def _rejection_outcome(evaluation: RiskEvaluation) -> AllocationOutcome:
    """Distinguish an already-held opportunity from a genuine refusal.

    Running the stage twice over the same upstream artifacts must not reserve
    the capital twice, and the second run should say so plainly rather than
    reporting a limit breach that only exists because of its own first run.
    """
    if RiskReasonCode.DUPLICATE_OPPORTUNITY in evaluation.reason_codes:
        return AllocationOutcome.ALREADY_ALLOCATED
    return AllocationOutcome.REJECTED


def _first_detail(evaluation: RiskEvaluation) -> str | None:
    failed = evaluation.failed_checks
    return failed[0].describe() if failed else None


def _with_reservation(
    campaign: CampaignSnapshot, decision: CandidateAllocation, *, as_of: datetime
) -> CampaignSnapshot:
    """The campaign as it stands once this authorisation is counted.

    A new snapshot rather than a mutation: the record of what the campaign
    looked like *before* the run must survive the run, and a snapshot that
    changed underneath the decisions citing it would make every one of them
    unexplainable.

    Constructed rather than copied, so the accounting validators run again on
    every step. ``model_copy`` would skip them, and the one thing worth
    re-checking after each commitment is that the campaign has not just been
    over-committed.
    """
    candidate = decision.candidate
    reservation = CampaignPosition(
        opportunity_id=candidate.opportunity_id,
        allocation_id=f"pending-{candidate.opportunity_id}",
        symbol=candidate.symbol,
        strategy=candidate.strategy,
        direction=candidate.risk_profile.directional_view,
        quantity=decision.quantity,
        capital_committed=decision.capital_committed,
        max_loss=decision.total_max_loss,
        authorized_at=as_of,
        expiration=candidate.expiration,
        contract_selection_id=candidate.contract_selection_id,
        research_report_id=candidate.research_report_id,
    )
    return CampaignSnapshot(
        campaign_id=campaign.campaign_id,
        as_of=campaign.as_of,
        currency=campaign.currency,
        schema_version=campaign.schema_version,
        budget=campaign.budget,
        reserve=campaign.reserve,
        budget_source=campaign.budget_source,
        open_positions=[*campaign.open_positions, reservation],
        realized_pnl_today=campaign.realized_pnl_today,
    )
