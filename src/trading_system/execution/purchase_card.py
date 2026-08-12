"""Mint the Milestone 1 purchase card and risk decision from an authorisation.

Milestone 7 recommended that Milestone 8 do this, because Milestone 7 has no
card to reference: it produces a :class:`~trading_system.allocation.models.CampaignAllocation`,
and the specification (section 12) requires an immutable purchase card *before*
execution. Both artifacts already exist as Milestone 1 definitions with shipped
schemas, so they are reused rather than forked — a second ``PurchaseCard`` would
guarantee the two drifted, and the evaluation milestone reads the Milestone 1
one.

Everything here is a **pure function of its arguments**. Nothing reads a clock,
a repository or a broker, so a card can be re-minted from stored artifacts years
later and come out byte-identical. The service does the loading; this module
does the arithmetic-free copying.

The one rule worth stating plainly: this module *copies*. It does not compute a
quantity, re-derive a maximum loss, re-check a limit or select a contract. Every
number on the card comes from the authorisation, and a mismatch is an error
rather than a correction.
"""

from __future__ import annotations

from datetime import datetime

from trading_system.allocation.models import CampaignAllocation
from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    AllocationOutcome,
    ExecutionReasonCode,
    RiskCheckOutcome,
)
from trading_system.domain.models import (
    ContractSelection,
    OptionLeg,
    PurchaseCard,
    ResearchReport,
    RiskDecision,
    StrategyDecision,
    SystemVersions,
)
from trading_system.execution.models import EXECUTION_SCHEMA_VERSION, ExecutionLeg

__all__ = [
    "PurchaseCardError",
    "build_execution_legs",
    "build_purchase_card",
    "build_risk_decision",
    "purchase_card_identifier",
    "risk_decision_identifier",
]


class PurchaseCardError(RuntimeError):
    """A purchase card could not be minted from the authorisation.

    Carries a machine-readable reason so the caller records *why* rather than a
    message it has to pattern-match on.
    """

    def __init__(self, reason_code: ExecutionReasonCode, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def purchase_card_identifier(
    *,
    allocation_id: str,
    research_report_id: str,
    strategy_decision_id: str,
    quantity: int,
    schema_version: str = EXECUTION_SCHEMA_VERSION,
) -> str:
    """Derive the card's identity from what it authorises.

    Content-derived and clock-free, so minting a card twice from the same
    authorisation produces one card rather than two records of one intention.
    """
    digest = stable_hash(
        [
            "PURCHASE_CARD",
            schema_version,
            allocation_id,
            research_report_id,
            strategy_decision_id,
            quantity,
        ]
    )
    return f"card-{digest[:20]}"


def risk_decision_identifier(
    *, purchase_card_id: str, evaluation_id: str, schema_version: str = EXECUTION_SCHEMA_VERSION
) -> str:
    """Derive the Milestone 1 risk decision's identity from the evaluation it projects."""
    digest = stable_hash(["RISK_DECISION", schema_version, purchase_card_id, evaluation_id])
    return f"risk-{digest[:20]}"


def build_execution_legs(allocation: CampaignAllocation) -> list[ExecutionLeg]:
    """Copy the authorisation's legs into execution legs.

    Refuses rather than repairs. A leg with no broker contract id cannot be
    submitted, because the alternative — rebuilding the contract from symbol,
    strike and expiration — is contract selection performed at execution time
    against a chain nobody looked at, and it produces an order for a contract
    the system never chose.
    """
    if not allocation.legs:
        raise PurchaseCardError(
            ExecutionReasonCode.CONTRACT_INVALID,
            f"allocation {allocation.allocation_id} authorises no legs",
        )

    legs: list[ExecutionLeg] = []
    for leg in sorted(allocation.legs, key=lambda item: item.leg_index):
        if not leg.contract_id or leg.contract_id <= 0:
            raise PurchaseCardError(
                ExecutionReasonCode.CONTRACT_ID_MISSING,
                f"leg {leg.leg_index} of {allocation.symbol} carries no broker contract id. "
                f"A contract id is never derived from symbol, strike and expiration: the "
                f"selected contract is the one Milestone 6 resolved, or there is none",
            )
        if leg.multiplier <= 0:
            raise PurchaseCardError(
                ExecutionReasonCode.MULTIPLIER_MISSING,
                f"leg {leg.leg_index} of {allocation.symbol} has no contract multiplier. "
                f"100 is common for US equity options and is not a default anything may "
                f"assume",
            )
        legs.append(
            ExecutionLeg(
                leg_index=leg.leg_index,
                contract_id=leg.contract_id,
                action=leg.action,
                right=leg.right,
                underlying=leg.underlying,
                expiration=leg.expiration,
                strike=leg.strike,
                multiplier=leg.multiplier,
                ratio=leg.ratio,
                trading_class=leg.trading_class,
                exchange=leg.exchange,
                local_symbol=leg.local_symbol,
                currency=leg.currency,
            )
        )
    return legs


def build_purchase_card(
    allocation: CampaignAllocation,
    *,
    research: ResearchReport,
    strategy: StrategyDecision,
    created_at: datetime,
    versions: SystemVersions,
) -> PurchaseCard:
    """Mint the Milestone 1 purchase card for an approved authorisation.

    ``research`` and ``strategy`` are passed in rather than looked up: this
    function must stay pure, and the *why* of a trade belongs to the artifacts
    that decided it. The card copies their conclusions verbatim — an execution
    layer that restated a hypothesis would be writing research.
    """
    if allocation.outcome is not AllocationOutcome.APPROVED:
        raise PurchaseCardError(
            ExecutionReasonCode.ALLOCATION_NOT_APPROVED,
            f"allocation {allocation.allocation_id} is {allocation.outcome.value}; only an "
            f"APPROVED authorisation can be executed",
        )
    if allocation.dry_run:
        raise PurchaseCardError(
            ExecutionReasonCode.ALLOCATION_IS_DRY_RUN,
            f"allocation {allocation.allocation_id} came from a dry run and is diagnostic; "
            f"it authorises nothing",
        )
    if allocation.quantity < 1:
        raise PurchaseCardError(
            ExecutionReasonCode.INVALID_QUANTITY,
            f"allocation {allocation.allocation_id} authorises {allocation.quantity} contracts",
        )
    if allocation.dte is None or allocation.expiration is None:
        raise PurchaseCardError(
            ExecutionReasonCode.CONTRACT_INVALID,
            f"allocation {allocation.allocation_id} carries no expiration or DTE",
        )
    if research.ticker != allocation.symbol or strategy.ticker != allocation.symbol:
        raise PurchaseCardError(
            ExecutionReasonCode.PROVENANCE_UNAVAILABLE,
            f"provenance describes {research.ticker}/{strategy.ticker}, not {allocation.symbol}",
        )
    if strategy.strategy_type is not allocation.strategy:
        raise PurchaseCardError(
            ExecutionReasonCode.PROVENANCE_UNAVAILABLE,
            f"strategy decision {strategy.decision_id} chose "
            f"{strategy.strategy_type.value if strategy.strategy_type else 'NO_TRADE'}, but the "
            f"authorisation is for {allocation.strategy.value}",
        )

    legs = build_execution_legs(allocation)
    card_id = purchase_card_identifier(
        allocation_id=allocation.allocation_id,
        research_report_id=research.report_id,
        strategy_decision_id=strategy.decision_id,
        quantity=allocation.quantity,
    )

    return PurchaseCard(
        card_id=card_id,
        created_at=created_at,
        underlying=allocation.symbol,
        strategy_type=allocation.strategy,
        contract=ContractSelection(
            underlying=allocation.symbol,
            strategy_type=allocation.strategy,
            as_of=allocation.as_of,
            legs=[
                OptionLeg(
                    underlying=leg.underlying,
                    right=leg.right,
                    strike=leg.strike,
                    expiration=leg.expiration,
                    action=leg.action,
                    ratio=leg.ratio,
                    multiplier=leg.multiplier,
                    occ_symbol=leg.local_symbol,
                    broker_contract_id=leg.contract_id,
                )
                for leg in legs
            ],
            dte=allocation.dte,
            selection_rules_version=allocation.strategy_version,
        ),
        # --- why: copied from research, never restated -----------------------
        hypothesis=research.hypothesis,
        confidence=research.confidence,
        expected_magnitude=research.expected_magnitude,
        expected_horizon_days=research.expected_horizon_days,
        # --- how much: copied from the authorisation, never recomputed -------
        quantity=allocation.quantity,
        requested_allocation_eur=allocation.capital_committed,
        risk_limits=_limits_of(allocation),
        thesis_invalidation_conditions=list(research.invalidation_conditions),
        research_report_id=research.report_id,
        strategy_decision_id=strategy.decision_id,
        sources=list(research.sources),
        versions=versions,
    )


def build_risk_decision(
    allocation: CampaignAllocation,
    *,
    purchase_card_id: str,
    versions: SystemVersions,
) -> RiskDecision:
    """Project the authorisation's risk evaluation onto the Milestone 1 boundary.

    The verdict is Milestone 7's and is copied, not re-reached. This function
    cannot approve anything: it reads ``risk_evaluation.outcome`` and would
    raise before inventing one.
    """
    evaluation = allocation.risk_evaluation
    return RiskDecision(
        decision_id=risk_decision_identifier(
            purchase_card_id=purchase_card_id, evaluation_id=evaluation.evaluation_id
        ),
        purchase_card_id=purchase_card_id,
        as_of=evaluation.as_of,
        outcome=evaluation.outcome,
        reason_codes=list(evaluation.reason_codes),
        evaluated_limits={
            check.name: check.describe()
            for check in evaluation.checks
            if check.outcome is not RiskCheckOutcome.NOT_EVALUATED
        },
        trading_mode=allocation.trading_mode,
        versions=versions,
    )


def _limits_of(allocation: CampaignAllocation) -> dict[str, str]:
    """The limits that actually applied, recorded on the card as strings.

    Taken from the stored evaluation rather than re-resolved from configuration:
    a card must stay explainable after ``risk.yaml`` has moved on.
    """
    limits = allocation.risk_evaluation.limits
    return {
        "campaign_budget": str(limits.campaign_budget),
        "max_allocation_per_trade": str(limits.max_allocation_per_trade),
        "max_risk_per_trade": str(limits.max_risk_per_trade),
        "max_contracts_per_trade": str(limits.max_contracts_per_trade),
        "max_open_positions": str(limits.max_open_positions),
        "total_max_loss": str(allocation.total_max_loss),
    }
