"""Minting the Milestone 1 purchase card from an authorisation (brief 42.2).

The card is the specification's required "why we trade" artifact (section 12),
and Milestone 7 recommended that Milestone 8 mint it. The rule these tests
enforce is that minting is *copying*: nothing here computes a quantity,
re-derives a maximum loss, re-checks a limit or picks a contract, and a figure
that disagreed with the authorisation would be a card authorising a trade
nobody approved.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    AllocationOutcome,
    ExecutionReasonCode,
    RiskOutcome,
    StrategyType,
)
from trading_system.domain.models import PurchaseCard
from trading_system.execution.purchase_card import (
    PurchaseCardError,
    build_execution_legs,
    build_purchase_card,
    build_risk_decision,
    purchase_card_identifier,
)

from .conftest import NOW

pytestmark = pytest.mark.unit


@pytest.fixture
def card(approved_allocation, research_report, strategy_decision, versions) -> PurchaseCard:
    return build_purchase_card(
        approved_allocation,
        research=research_report,
        strategy=strategy_decision,
        created_at=NOW,
        versions=versions,
    )


# ---------------------------------------------------------------------------
# What is copied
# ---------------------------------------------------------------------------
def test_a_card_is_created_from_an_approved_allocation(card, approved_allocation) -> None:
    assert card.underlying == approved_allocation.symbol
    assert card.strategy_type is approved_allocation.strategy


def test_the_exact_quantity_is_preserved(card, approved_allocation) -> None:
    """Never recomputed. Milestone 7 decided this number."""
    assert card.quantity == approved_allocation.quantity


def test_the_capital_commitment_is_preserved(card, approved_allocation) -> None:
    assert card.requested_allocation_eur == approved_allocation.capital_committed


def test_the_maximum_loss_is_carried_from_the_authorisation(card, approved_allocation) -> None:
    """Recorded on the card's limits, not recalculated from a formula.

    "Max loss is the premium" is true of the four long-debit strategies shipped
    today and false of the first credit spread anyone adds.
    """
    assert card.risk_limits["total_max_loss"] == str(approved_allocation.total_max_loss)


def test_the_exact_legs_and_contract_ids_are_preserved(card, approved_allocation) -> None:
    assert len(card.contract.legs) == len(approved_allocation.legs)
    minted = [leg.broker_contract_id for leg in card.contract.legs]
    authorised = [leg.contract_id for leg in approved_allocation.legs]
    assert minted == authorised


def test_the_multiplier_is_carried_rather_than_assumed(card, approved_allocation) -> None:
    assert [leg.multiplier for leg in card.contract.legs] == [
        leg.multiplier for leg in approved_allocation.legs
    ]


def test_the_why_comes_from_research_verbatim(card, research_report) -> None:
    """An execution layer that restated a hypothesis would be writing research."""
    assert card.hypothesis is research_report.hypothesis
    assert card.confidence == research_report.confidence
    assert card.expected_magnitude is research_report.expected_magnitude
    assert card.expected_horizon_days == research_report.expected_horizon_days
    assert card.thesis_invalidation_conditions == list(research_report.invalidation_conditions)


def test_the_card_names_the_artifacts_it_descends_from(
    card, research_report, strategy_decision
) -> None:
    assert card.research_report_id == research_report.report_id
    assert card.strategy_decision_id == strategy_decision.decision_id


# ---------------------------------------------------------------------------
# Identity and immutability
# ---------------------------------------------------------------------------
def test_the_card_id_is_deterministic(
    approved_allocation, research_report, strategy_decision, versions
) -> None:
    """Minting twice from one authorisation produces one card, not two."""
    first = build_purchase_card(
        approved_allocation,
        research=research_report,
        strategy=strategy_decision,
        created_at=NOW,
        versions=versions,
    )
    second = build_purchase_card(
        approved_allocation,
        research=research_report,
        strategy=strategy_decision,
        created_at=NOW,
        versions=versions,
    )
    assert first.card_id == second.card_id


def test_the_card_id_ignores_the_clock(
    approved_allocation, research_report, strategy_decision, versions
) -> None:
    """A clock-dependent id would make every re-mint look like a new intention."""
    from datetime import timedelta

    later = build_purchase_card(
        approved_allocation,
        research=research_report,
        strategy=strategy_decision,
        created_at=NOW + timedelta(hours=3),
        versions=versions,
    )
    assert later.card_id == purchase_card_identifier(
        allocation_id=approved_allocation.allocation_id,
        research_report_id=research_report.report_id,
        strategy_decision_id=strategy_decision.decision_id,
        quantity=approved_allocation.quantity,
    )


def test_a_different_quantity_is_a_different_card(
    approved_allocation, research_report, strategy_decision
) -> None:
    one = purchase_card_identifier(
        allocation_id=approved_allocation.allocation_id,
        research_report_id=research_report.report_id,
        strategy_decision_id=strategy_decision.decision_id,
        quantity=1,
    )
    two = purchase_card_identifier(
        allocation_id=approved_allocation.allocation_id,
        research_report_id=research_report.report_id,
        strategy_decision_id=strategy_decision.decision_id,
        quantity=2,
    )
    assert one != two


def test_the_card_is_immutable(card) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        card.quantity = 99


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------
def test_a_rejected_allocation_cannot_be_minted(
    approved_allocation, research_report, strategy_decision, versions
) -> None:
    """REJECTED authorises nothing, so there is nothing to mint a card for."""
    rejected = approved_allocation.model_copy(
        update={
            "outcome": AllocationOutcome.REJECTED,
            "risk_outcome": RiskOutcome.REJECTED,
            "quantity": 0,
            "capital_committed": Decimal("0"),
            "total_max_loss": Decimal("0"),
        }
    )

    with pytest.raises(PurchaseCardError) as error:
        build_purchase_card(
            rejected,
            research=research_report,
            strategy=strategy_decision,
            created_at=NOW,
            versions=versions,
        )
    assert error.value.reason_code is ExecutionReasonCode.ALLOCATION_NOT_APPROVED


def test_a_dry_run_allocation_authorises_nothing(
    approved_allocation, research_report, strategy_decision, versions
) -> None:
    diagnostic = approved_allocation.model_copy(update={"dry_run": True})

    with pytest.raises(PurchaseCardError) as error:
        build_purchase_card(
            diagnostic,
            research=research_report,
            strategy=strategy_decision,
            created_at=NOW,
            versions=versions,
        )
    assert error.value.reason_code is ExecutionReasonCode.ALLOCATION_IS_DRY_RUN


def test_a_leg_without_a_contract_id_is_refused(approved_allocation) -> None:
    """Never re-derived from symbol, strike and expiration.

    Rebuilding one would be selecting a contract at execution time, against a
    chain nobody looked at — an order for something the system never chose.
    """
    legs = [approved_allocation.legs[0].model_copy(update={"contract_id": 0})]
    broken = approved_allocation.model_copy(update={"legs": legs})

    with pytest.raises(PurchaseCardError) as error:
        build_execution_legs(broken)
    assert error.value.reason_code is ExecutionReasonCode.CONTRACT_ID_MISSING


def test_provenance_about_a_different_symbol_is_refused(
    approved_allocation, research_report, strategy_decision, versions
) -> None:
    other = research_report.model_copy(update={"ticker": "AAPL"})

    with pytest.raises(PurchaseCardError) as error:
        build_purchase_card(
            approved_allocation,
            research=other,
            strategy=strategy_decision,
            created_at=NOW,
            versions=versions,
        )
    assert error.value.reason_code is ExecutionReasonCode.PROVENANCE_UNAVAILABLE


def test_a_strategy_decision_for_another_strategy_is_refused(
    approved_allocation, research_report, strategy_decision, versions
) -> None:
    mismatched = strategy_decision.model_copy(update={"strategy_type": StrategyType.LONG_PUT})

    with pytest.raises(PurchaseCardError) as error:
        build_purchase_card(
            approved_allocation,
            research=research_report,
            strategy=mismatched,
            created_at=NOW,
            versions=versions,
        )
    assert error.value.reason_code is ExecutionReasonCode.PROVENANCE_UNAVAILABLE


# ---------------------------------------------------------------------------
# The projected risk decision
# ---------------------------------------------------------------------------
def test_the_risk_decision_copies_the_verdict_rather_than_reaching_one(
    approved_allocation, card, versions
) -> None:
    decision = build_risk_decision(
        approved_allocation, purchase_card_id=card.card_id, versions=versions
    )

    assert decision.outcome is approved_allocation.risk_evaluation.outcome
    assert decision.reason_codes == list(approved_allocation.risk_evaluation.reason_codes)
    assert decision.purchase_card_id == card.card_id


def test_the_risk_decision_is_deterministic(approved_allocation, card, versions) -> None:
    first = build_risk_decision(
        approved_allocation, purchase_card_id=card.card_id, versions=versions
    )
    second = build_risk_decision(
        approved_allocation, purchase_card_id=card.card_id, versions=versions
    )
    assert first.decision_id == second.decision_id


def test_the_risk_decision_records_only_evaluated_limits(
    approved_allocation, card, versions
) -> None:
    """An unevaluated limit is not a satisfied one, so it is not listed as one."""
    from trading_system.domain.enums import RiskCheckOutcome

    decision = build_risk_decision(
        approved_allocation, purchase_card_id=card.card_id, versions=versions
    )
    unevaluated = {
        check.name
        for check in approved_allocation.risk_evaluation.checks
        if check.outcome is RiskCheckOutcome.NOT_EVALUATED
    }
    assert not (unevaluated & set(decision.evaluated_limits))
