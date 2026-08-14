"""The remaining deterministic policies, one at a time.

The two that carry the most weight are ``data_quality`` — because every money
policy is a function of the one number it guards — and ``max_loss``, because it
reuses Milestone 7's declared basis rather than inventing a second formula.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.domain.enums import (
    BrokerReadStatus,
    ExitDecisionType,
    ExitPolicyKind,
    ExitQuoteField,
    ExitReasonCode,
    MaxLossBasis,
    PositionLifecycleState,
    StructureStatus,
)
from trading_system.exit.models import ExitLegValuation, PositionValuation
from trading_system.exit.policies import (
    broker_observation,
    contract_validity,
    data_quality,
    evaluate_max_loss,
    evaluate_take_profit,
    execution_state,
    position_consistency,
)
from trading_system.infrastructure.settings import (
    ExitDataQualityConfig,
    SystemConfig,
    UnusableQuotePolicy,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Position consistency
# ---------------------------------------------------------------------------
def test_agreement_between_the_ledger_and_the_broker_waits() -> None:
    outcome = position_consistency(
        lifecycle_state=PositionLifecycleState.MONITORING,
        structure_status=StructureStatus.COMPLETE,
        expected_quantity=2,
        observed_quantity=2,
    )

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.POLICY_SATISFIED


def test_a_broker_holding_none_of_the_structure_is_closed() -> None:
    outcome = position_consistency(
        lifecycle_state=PositionLifecycleState.MONITORING,
        structure_status=StructureStatus.MISSING,
        expected_quantity=2,
        observed_quantity=0,
    )

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.POSITION_CLOSED


def test_an_unreadable_position_blocks_rather_than_reading_as_empty() -> None:
    """``None`` and ``0`` are different claims and must not be collapsed."""
    outcome = position_consistency(
        lifecycle_state=PositionLifecycleState.MONITORING,
        structure_status=StructureStatus.UNKNOWN,
        expected_quantity=2,
        observed_quantity=None,
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.POSITION_STATE_UNKNOWN


def test_a_partial_structure_blocks() -> None:
    """A straddle with one leg held is a naked long option, and no exit policy
    in this milestone was written for one."""
    outcome = position_consistency(
        lifecycle_state=PositionLifecycleState.MONITORING,
        structure_status=StructureStatus.PARTIAL,
        expected_quantity=2,
        observed_quantity=1,
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.PARTIAL_STRUCTURE


def test_a_quantity_disagreement_blocks_and_shows_both_sides() -> None:
    outcome = position_consistency(
        lifecycle_state=PositionLifecycleState.MONITORING,
        structure_status=StructureStatus.COMPLETE,
        expected_quantity=2,
        observed_quantity=1,
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.POSITION_QUANTITY_MISMATCH
    assert outcome.measured == "1"
    assert outcome.threshold == "2"


def test_an_unresolved_reconciliation_finding_blocks() -> None:
    outcome = position_consistency(
        lifecycle_state=PositionLifecycleState.MONITORING,
        structure_status=StructureStatus.COMPLETE,
        expected_quantity=2,
        observed_quantity=2,
        has_reconciliation_findings=True,
    )

    assert outcome.reason_code is ExitReasonCode.RECONCILIATION_REQUIRED


# ---------------------------------------------------------------------------
# 2. Broker observation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [BrokerReadStatus.OK, BrokerReadStatus.EMPTY])
def test_a_broker_that_answered_may_be_judged_against(status: BrokerReadStatus) -> None:
    assert broker_observation(read_status=status).decision is ExitDecisionType.WAIT


@pytest.mark.parametrize(
    "status",
    [BrokerReadStatus.UNAVAILABLE, BrokerReadStatus.TIMEOUT, BrokerReadStatus.MALFORMED],
)
def test_a_failed_read_blocks_and_is_not_an_empty_account(status: BrokerReadStatus) -> None:
    outcome = broker_observation(read_status=status)

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.BROKER_DATA_UNAVAILABLE
    assert "NOT an empty account" in (outcome.detail or "")


# ---------------------------------------------------------------------------
# 3. Execution state — the idempotency gate
# ---------------------------------------------------------------------------
def test_an_unknown_exit_blocks_and_says_it_may_be_live() -> None:
    outcome = execution_state(
        lifecycle_state=PositionLifecycleState.EXIT_UNKNOWN,
        exit_execution_id="execution-exit-1",
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.EXIT_OUTCOME_UNKNOWN
    assert "may be live at the broker" in (outcome.detail or "")
    assert "never by sending again" in (outcome.detail or "")


def test_a_working_exit_waits_rather_than_blocking() -> None:
    """Nothing is wrong: waiting for a working order is the correct behaviour."""
    outcome = execution_state(
        lifecycle_state=PositionLifecycleState.EXIT_SUBMITTED,
        exit_execution_id="execution-exit-1",
    )

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.EXIT_ALREADY_SUBMITTED


def test_a_blocked_lifecycle_is_not_itself_a_block() -> None:
    """A stale block must not suppress the expiration policy, which precedes
    every other trigger."""
    outcome = execution_state(lifecycle_state=PositionLifecycleState.BLOCKED)

    assert outcome.decision is ExitDecisionType.WAIT


# ---------------------------------------------------------------------------
# 4. Contract validity
# ---------------------------------------------------------------------------
def test_a_leg_without_a_contract_id_blocks() -> None:
    """Re-deriving one would send a closing order for a contract nobody holds."""
    valuation = factories.valuation(
        legs=[
            ExitLegValuation(
                leg_index=0, key="sym:x", quote_field=ExitQuoteField.BID, price=Decimal("6.00")
            )
        ]
    )

    outcome = contract_validity(valuation)

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.CONTRACT_METADATA_UNAVAILABLE


def test_a_missing_multiplier_blocks_and_is_never_assumed_to_be_a_hundred() -> None:
    outcome = contract_validity(factories.valuation(multiplier=None))

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.MULTIPLIER_UNAVAILABLE
    assert "never assumed to be 100" in (outcome.detail or "")


def test_legs_with_different_multipliers_block() -> None:
    """A combo's net price is only defined when they share one."""
    valuation = factories.valuation(
        legs=[
            ExitLegValuation(
                leg_index=0,
                key="cid:1",
                contract_id=1,
                multiplier=100,
                quote_field=ExitQuoteField.BID,
                price=Decimal("6.00"),
            ),
            ExitLegValuation(
                leg_index=1,
                key="cid:2",
                contract_id=2,
                multiplier=10,
                quote_field=ExitQuoteField.BID,
                price=Decimal("4.00"),
            ),
        ]
    )

    outcome = contract_validity(valuation)

    assert outcome.reason_code is ExitReasonCode.CONTRACT_METADATA_UNAVAILABLE
    assert "different multipliers" in outcome.summary


def test_a_complete_structure_passes() -> None:
    assert contract_validity(factories.valuation()).decision is ExitDecisionType.WAIT


# ---------------------------------------------------------------------------
# 5. Data quality
# ---------------------------------------------------------------------------
def test_an_unpriced_structure_blocks(system_config: SystemConfig) -> None:
    outcome = data_quality(
        factories.valuation(exit_quote=None), config=system_config.exit.data_quality
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.MARKET_DATA_UNAVAILABLE


def test_a_quote_missing_only_the_configured_field_is_named_precisely(
    system_config: SystemConfig,
) -> None:
    """``MARKET_DATA_UNAVAILABLE`` means collect some data;
    ``QUOTE_FIELD_UNAVAILABLE`` means it arrived without the side we trade out on."""
    valuation = PositionValuation(
        as_of=NOW,
        quote_field=ExitQuoteField.BID,
        multiplier=100,
        open_quantity=1,
        legs=[
            ExitLegValuation(
                leg_index=0,
                key="cid:1",
                contract_id=1,
                quote_field=ExitQuoteField.BID,
                price=None,
                bid=None,
                ask=Decimal("6.70"),
                last=Decimal("6.60"),
            )
        ],
        unpriced_legs=[0],
    )

    outcome = data_quality(valuation, config=system_config.exit.data_quality)

    assert outcome.reason_code is ExitReasonCode.QUOTE_FIELD_UNAVAILABLE
    assert "No other field is substituted" in (outcome.detail or "")


def test_a_stale_quote_blocks(system_config: SystemConfig) -> None:
    outcome = data_quality(
        factories.valuation(quote_age_seconds=5000.0), config=system_config.exit.data_quality
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.MARKET_DATA_STALE
    assert outcome.threshold == "900"


def test_the_stalest_leg_binds(system_config: SystemConfig) -> None:
    """A structure is only as fresh as its stalest leg."""
    valuation = factories.valuation(quote_age_seconds=1200.0)

    assert data_quality(valuation, config=system_config.exit.data_quality).decision is (
        ExitDecisionType.BLOCK
    )


def test_the_configured_response_to_an_unusable_quote_can_be_wait() -> None:
    """Two honest answers. There is no third that substitutes a field."""
    config = ExitDataQualityConfig(on_unavailable=UnusableQuotePolicy.WAIT)

    outcome = data_quality(factories.valuation(exit_quote=None), config=config)

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.NOT_EVALUATED


def test_field_substitution_cannot_be_switched_on() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="allow_quote_field_substitution"):
        ExitDataQualityConfig(allow_quote_field_substitution=True)


def test_a_fresh_priced_structure_passes(system_config: SystemConfig) -> None:
    outcome = data_quality(factories.valuation(), config=system_config.exit.data_quality)

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.POLICY_SATISFIED


# ---------------------------------------------------------------------------
# 6. Maximum loss — Milestone 7's basis, reused
# ---------------------------------------------------------------------------
def test_an_undefined_basis_blocks_rather_than_being_estimated(
    system_config: SystemConfig,
) -> None:
    """An unquantified loss is not a small one."""
    outcome = evaluate_max_loss(
        factories.valuation(),
        basis=MaxLossBasis.NOT_DEFINED,
        max_loss_total=None,
        effective_loss_pct=50.0,
        config=system_config.exit.max_loss,
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.RISK_BASIS_UNAVAILABLE


def test_the_loss_is_measured_against_what_was_paid(system_config: SystemConfig) -> None:
    """``NET_DEBIT_PAID`` means the most that can be lost is the debit, so the
    percentage is of the entry cost — Milestone 7's own arithmetic."""
    outcome = evaluate_max_loss(
        factories.valuation(exit_quote=Decimal("3.00"), entry_quote=Decimal("6.00")),
        basis=MaxLossBasis.NET_DEBIT_PAID,
        max_loss_total=Decimal("1200.00"),
        effective_loss_pct=50.0,
        config=system_config.exit.max_loss,
    )

    assert outcome.decision is ExitDecisionType.EXIT
    assert outcome.reason_code is ExitReasonCode.MAX_LOSS_REACHED
    assert Decimal(outcome.measured or "0") == Decimal("50")


def test_a_position_in_profit_has_lost_nothing(system_config: SystemConfig) -> None:
    """Zero rather than a negative number: "lost -30%" is unreadable."""
    outcome = evaluate_max_loss(
        factories.valuation(exit_quote=Decimal("9.00")),
        basis=MaxLossBasis.NET_DEBIT_PAID,
        max_loss_total=Decimal("1200.00"),
        effective_loss_pct=50.0,
        config=system_config.exit.max_loss,
    )

    assert outcome.decision is ExitDecisionType.WAIT
    assert Decimal(outcome.measured or "-1") == Decimal("0")


def test_a_strategy_may_narrow_the_loss_limit(system_config: SystemConfig) -> None:
    valuation = factories.valuation(exit_quote=Decimal("4.20"), entry_quote=Decimal("6.00"))

    loose = evaluate_max_loss(
        valuation,
        basis=MaxLossBasis.NET_DEBIT_PAID,
        max_loss_total=Decimal("1200.00"),
        effective_loss_pct=50.0,
        config=system_config.exit.max_loss,
    )
    tight = evaluate_max_loss(
        valuation,
        basis=MaxLossBasis.NET_DEBIT_PAID,
        max_loss_total=Decimal("1200.00"),
        effective_loss_pct=25.0,
        config=system_config.exit.max_loss,
    )

    assert loose.decision is ExitDecisionType.WAIT
    assert tight.decision is ExitDecisionType.EXIT


def test_an_unpriced_structure_leaves_the_loss_unevaluated(
    system_config: SystemConfig,
) -> None:
    outcome = evaluate_max_loss(
        factories.valuation(exit_quote=None),
        basis=MaxLossBasis.NET_DEBIT_PAID,
        max_loss_total=Decimal("1200.00"),
        effective_loss_pct=50.0,
        config=system_config.exit.max_loss,
    )

    assert outcome.reason_code is ExitReasonCode.NOT_EVALUATED
    assert outcome.evaluated is False


# ---------------------------------------------------------------------------
# 7. Take profit
# ---------------------------------------------------------------------------
def test_a_reached_target_exits(system_config: SystemConfig) -> None:
    outcome = evaluate_take_profit(
        factories.valuation(exit_quote=Decimal("12.00"), entry_quote=Decimal("6.00")),
        effective_return_pct=100.0,
        config=system_config.exit.take_profit,
    )

    assert outcome.decision is ExitDecisionType.EXIT
    assert outcome.reason_code is ExitReasonCode.TAKE_PROFIT_REACHED
    assert Decimal(outcome.measured or "0") == Decimal("100")


def test_a_strategy_with_no_target_is_not_evaluated(system_config: SystemConfig) -> None:
    """Permitted: take profit is not a safety limit, and the position is still
    bounded by the trailing stop, the maximum loss and the expiration policy."""
    outcome = evaluate_take_profit(
        factories.valuation(exit_quote=Decimal("60.00")),
        effective_return_pct=None,
        config=system_config.exit.take_profit,
    )

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.NOT_EVALUATED


def test_take_profit_and_maximum_loss_use_the_same_basis(system_config: SystemConfig) -> None:
    """So "how far is this position from each of its bounds" is answerable."""
    valuation = factories.valuation(exit_quote=Decimal("9.00"), entry_quote=Decimal("6.00"))

    profit = evaluate_take_profit(
        valuation, effective_return_pct=100.0, config=system_config.exit.take_profit
    )
    loss = evaluate_max_loss(
        valuation,
        basis=MaxLossBasis.NET_DEBIT_PAID,
        max_loss_total=Decimal("1200.00"),
        effective_loss_pct=50.0,
        config=system_config.exit.max_loss,
    )

    assert Decimal(profit.measured or "0") == Decimal("50")
    assert Decimal(loss.measured or "0") == Decimal("0")


# ---------------------------------------------------------------------------
# Every policy reports which one it is
# ---------------------------------------------------------------------------
def test_each_policy_labels_its_own_outcome() -> None:
    """The engine keys on this, so a mislabelled outcome would silently swap
    two policies' verdicts."""
    assert (
        position_consistency(
            lifecycle_state=PositionLifecycleState.MONITORING,
            structure_status=StructureStatus.COMPLETE,
            expected_quantity=1,
            observed_quantity=1,
        ).policy
        is ExitPolicyKind.POSITION_CONSISTENCY
    )
    assert (
        broker_observation(read_status=BrokerReadStatus.OK).policy
        is ExitPolicyKind.BROKER_OBSERVATION
    )
    assert (
        execution_state(lifecycle_state=PositionLifecycleState.MONITORING).policy
        is ExitPolicyKind.EXECUTION_STATE
    )
    assert contract_validity(factories.valuation()).policy is ExitPolicyKind.CONTRACT_VALIDITY
