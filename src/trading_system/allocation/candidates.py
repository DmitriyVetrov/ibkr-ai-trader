"""Turning a Milestone 6 contract selection into an allocation candidate.

The seam between the two milestones. Milestone 6 ends at a purchase candidate —
legs, and the cost of *one unit* of the structure — and this module carries that
across without adding anything the earlier stage did not establish:

.. code-block:: text

    ContractSelectionResult + StrategyDecisionRecord + StrategySpecification
                                    |
                        AllocationCandidate
                    (unit cost, max-loss basis, score, provenance)

Three rules govern the translation, and each has tests that fail loudly:

* **Nothing is upgraded on the way through.** A selection whose cost estimate
  was unavailable becomes a candidate whose price is unavailable, with the same
  reason attached. No midpoint is conjured from one side, no last price is
  substituted, and no zero appears where a measurement did not.
* **The strategy's own risk semantics travel with it.** ``max_loss_basis``
  comes from the strategy's structure, not from an assumption made here, so the
  first strategy whose loss is not bounded by its debit is refused rather than
  sized as though it were.
* **Provenance is by id.** The candidate names the research report, the
  strategy decision and the contract selection it descends from; it does not
  copy them. The one exception is the legs, which are carried because Milestone
  8 has to build an order from an authorisation without re-running selection.

This is also the module that computes the deterministic priority score, because
this is where all of its inputs are in scope at once — and the score is stored
on the candidate, so ordering is reproducible from the record rather than
recomputed from configuration that may since have changed.
"""

from __future__ import annotations

from trading_system.allocation.scorer import score_opportunity
from trading_system.domain.enums import (
    DataQuality,
    ExpectedMagnitude,
    PriceSource,
)
from trading_system.infrastructure.settings import CampaignRankingConfig
from trading_system.risk.models import (
    AllocationCandidate,
    CandidateLeg,
    CandidatePrice,
    StrategyRiskProfile,
    opportunity_identifier,
)
from trading_system.strategies.models import (
    ContractSelectionResult,
    StrategyDecisionRecord,
)
from trading_system.strategies.registry import StrategySpecification

__all__ = ["CandidateBuildError", "build_candidate", "risk_profile_of"]


class CandidateBuildError(RuntimeError):
    """A contract selection could not be carried across, and will not be patched."""


def risk_profile_of(specification: StrategySpecification) -> StrategyRiskProfile:
    """The strategy-level risk semantics that will apply to this candidate.

    Copied onto the candidate rather than looked up inside the engines. Two
    reasons, and the second is the one that matters: the engines stay pure
    functions of their arguments, and the limits that *actually applied* end up
    on the stored artifact, so a past authorisation is still explainable after
    the strategy configuration has changed.
    """
    return StrategyRiskProfile(
        strategy=specification.strategy_id,
        strategy_version=specification.version,
        max_loss_basis=specification.max_loss_basis,
        directional_view=specification.directional_view,
        single_position=specification.structure.single_position,
        leg_count=specification.structure.leg_count,
        dte_min=specification.dte_min,
        dte_max=specification.dte_max,
        min_option_price=specification.min_option_price,
        max_option_price=specification.max_option_price,
        max_bid_ask_spread_pct=specification.max_bid_ask_spread_pct,
    )


def build_candidate(
    selection: ContractSelectionResult,
    specification: StrategySpecification,
    ranking: CampaignRankingConfig,
    *,
    decision: StrategyDecisionRecord | None = None,
    expected_magnitude: ExpectedMagnitude | None = None,
    research_usable: bool = True,
    data_quality: DataQuality = DataQuality.OK,
    price_source: PriceSource = PriceSource.ASK_DEBIT,
) -> AllocationCandidate:
    """Carry one successful contract selection across the milestone boundary.

    ``expected_magnitude``, ``research_usable`` and ``data_quality`` are passed
    in rather than looked up. They live on the Milestone 5 report, and reading
    that here would put a second repository in this module's import graph for
    the sake of one scoring term and one verdict — the service already holds
    the research run and can supply both. Absent values are handled honestly
    downstream: the scorer treats a missing band as the least favourable, never
    as a zero and never as the benefit of the doubt.

    Raises :class:`CandidateBuildError` for a selection that produced no
    contract. There is deliberately no way to build a candidate from a failed
    selection: a stage that selected nothing has nothing to allocate against,
    and manufacturing a candidate from it would put capital behind a contract
    that was never chosen.
    """
    if not selection.succeeded:
        raise CandidateBuildError(
            f"{selection.symbol}: cannot allocate against a "
            f"{selection.selection_status.value} selection; no contract was selected"
        )
    if selection.expiration is None or selection.dte is None:
        raise CandidateBuildError(
            f"{selection.symbol}: a successful selection must state its expiration and DTE"
        )

    legs = [
        CandidateLeg(
            leg_index=leg.leg_index,
            action=leg.action,
            right=leg.right,
            ratio=leg.ratio,
            underlying=leg.underlying,
            expiration=leg.expiration,
            strike=leg.strike,
            multiplier=leg.multiplier,
            contract_id=leg.contract_id,
            trading_class=leg.trading_class,
            exchange=leg.exchange,
            local_symbol=leg.local_symbol,
            currency=leg.currency,
            bid=leg.bid,
            ask=leg.ask,
            quote_as_of=leg.quote_as_of,
            quote_snapshot_id=leg.quote_snapshot_id,
        )
        for leg in sorted(selection.legs, key=lambda leg: leg.leg_index)
    ]

    price = _price_of(selection, price_source)
    profile = risk_profile_of(specification)

    research_confidence = decision.research_confidence if decision else None
    strategy_confidence = decision.confidence if decision else None

    score = score_opportunity(
        ranking,
        research_confidence=research_confidence,
        strategy_confidence=strategy_confidence,
        expected_magnitude=expected_magnitude,
        max_leg_spread_pct=price.max_leg_spread_pct,
        spread_ceiling_pct=profile.max_bid_ask_spread_pct,
        research_usable=research_usable,
    )

    return AllocationCandidate(
        opportunity_id=opportunity_identifier(
            contract_selection_id=selection.selection_id,
            strategy_decision_id=selection.strategy_decision_id,
            research_report_id=selection.research_report_id,
        ),
        symbol=selection.symbol,
        as_of=selection.as_of,
        strategy=specification.strategy_id,
        risk_profile=profile,
        legs=legs,
        expiration=selection.expiration,
        dte=selection.dte,
        price=price,
        score=score,
        hypothesis=decision.hypothesis if decision else None,
        research_confidence=research_confidence,
        strategy_confidence=strategy_confidence,
        expected_magnitude=expected_magnitude,
        horizon_days=decision.research_horizon_days if decision else None,
        research_usable=research_usable,
        data_quality=data_quality,
        contract_selection_id=selection.selection_id,
        contract_run_id=selection.run_id,
        strategy_decision_id=selection.strategy_decision_id,
        strategy_run_id=selection.strategy_run_id,
        research_report_id=selection.research_report_id,
        research_run_id=decision.research_run_id if decision else None,
        universe_run_id=decision.universe_run_id if decision else None,
        input_snapshot_ids=sorted(selection.input_snapshot_ids),
    )


def _price_of(selection: ContractSelectionResult, source: PriceSource) -> CandidatePrice:
    """The cost of one unit, or an honest record that it is unknown.

    The debit is taken from whichever field configuration names, and the field
    is recorded. Falling back from an unavailable ask to a midpoint would be a
    silent change of claim — "what it would cost to buy" and "what it might
    cost if someone met us in the middle" are different numbers, and a position
    sized on the second is sized on a fill nobody has been offered.
    """
    cost = selection.cost
    if cost is None or not cost.available:
        return CandidatePrice(
            available=False,
            currency=cost.currency if cost else None,
            unavailable_reason=(
                cost.unavailable_reason
                if cost and cost.unavailable_reason
                else "the contract selection established no cost for this structure"
            ),
        )

    figure = cost.estimated_debit if source is PriceSource.ASK_DEBIT else cost.estimated_mid_debit
    if figure is None:
        return CandidatePrice(
            available=False,
            currency=cost.currency,
            unavailable_reason=(
                f"configuration prices candidates from {source.value}, which this selection "
                f"did not establish. Substituting the other side would change the claim, not "
                f"complete it"
            ),
        )

    quote_as_of = min(
        (leg.quote_as_of for leg in selection.legs if leg.quote_as_of is not None),
        default=None,
    )
    return CandidatePrice(
        available=True,
        source=source,
        currency=cost.currency,
        unit_cost=figure,
        max_leg_spread_pct=cost.max_leg_spread_pct,
        quote_as_of=quote_as_of,
    )
