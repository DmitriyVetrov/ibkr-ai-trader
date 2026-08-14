"""The exit vocabulary and the shapes that enforce it.

These are the tests that make the milestone's claims structural rather than
stylistic. The models *refuse* to express a decision this system must not make:
a WAIT that carries an exit reason, an EXIT that names no policy, a request
that closes half a straddle, an evaluation that submitted an order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.domain.enums import (
    EXIT_BLOCK_REASONS,
    EXIT_POLICY_PRECEDENCE,
    EXIT_SUBMISSION_BLOCKED_STATES,
    EXIT_TRIGGER_REASONS,
    EXIT_WAIT_REASONS,
    TERMINAL_LIFECYCLE_STATES,
    ExitAction,
    ExitDecisionType,
    ExitPolicyKind,
    ExitQuoteField,
    ExitReason,
    ExitReasonCode,
    OrderType,
    PositionLifecycleState,
    StrategyType,
    StructureStatus,
    TimeInForce,
    TradingMode,
    TrailingStopState,
)
from trading_system.exit.models import (
    ExitDecisionRecord,
    ExitEvaluation,
    ExitPolicyOutcome,
    ExitPolicySnapshot,
    ExitRequest,
    ExitRunResult,
    PositionValuation,
    ThesisConditionCheck,
    exit_request_identifier,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The vocabulary is closed, and partitioned
# ---------------------------------------------------------------------------
def test_the_three_reason_vocabularies_partition_the_enum() -> None:
    """Every reason code belongs to exactly one verdict.

    A member added without being classified would silently become a block,
    which is safe but invisible — and a member that appeared in two sets would
    let a WAIT carry an EXIT's reason.
    """
    everything = set(ExitReasonCode)
    assert everything == EXIT_WAIT_REASONS | EXIT_TRIGGER_REASONS | EXIT_BLOCK_REASONS
    assert set() == EXIT_WAIT_REASONS & EXIT_TRIGGER_REASONS
    assert set() == EXIT_WAIT_REASONS & EXIT_BLOCK_REASONS
    assert set() == EXIT_TRIGGER_REASONS & EXIT_BLOCK_REASONS


def test_every_policy_appears_exactly_once_in_the_precedence_list() -> None:
    assert len(EXIT_POLICY_PRECEDENCE) == len(set(ExitPolicyKind))
    assert set(EXIT_POLICY_PRECEDENCE) == set(ExitPolicyKind)


def test_safety_policies_precede_profit_taking() -> None:
    """The ordering claim, asserted rather than described."""
    order = list(EXIT_POLICY_PRECEDENCE)
    for safety in (
        ExitPolicyKind.POSITION_CONSISTENCY,
        ExitPolicyKind.BROKER_OBSERVATION,
        ExitPolicyKind.EXECUTION_STATE,
        ExitPolicyKind.CONTRACT_VALIDITY,
        ExitPolicyKind.EXPIRATION,
        ExitPolicyKind.DATA_QUALITY,
        ExitPolicyKind.MAX_LOSS,
    ):
        assert order.index(safety) < order.index(ExitPolicyKind.TAKE_PROFIT)
        assert order.index(safety) < order.index(ExitPolicyKind.TRAILING_STOP)


def test_closed_is_the_only_terminal_lifecycle_state() -> None:
    assert {PositionLifecycleState.CLOSED} == TERMINAL_LIFECYCLE_STATES


def test_an_unresolved_exit_blocks_a_second_submission() -> None:
    """The two states that mean *an order may be live* both block."""
    assert PositionLifecycleState.EXIT_SUBMITTED in EXIT_SUBMISSION_BLOCKED_STATES
    assert PositionLifecycleState.EXIT_UNKNOWN in EXIT_SUBMISSION_BLOCKED_STATES


def test_a_blocked_position_may_still_be_force_exited() -> None:
    """A block is a current verdict, not a memory.

    Including ``BLOCKED`` here would mean a position blocked once — because a
    research file was unreadable, say — could never afterwards be force-exited
    at its expiration deadline.
    """
    assert PositionLifecycleState.BLOCKED not in EXIT_SUBMISSION_BLOCKED_STATES


# ---------------------------------------------------------------------------
# A policy outcome cannot mislabel its own verdict
# ---------------------------------------------------------------------------
def test_a_wait_cannot_carry_a_trigger_reason() -> None:
    with pytest.raises(ValidationError, match="not a WAIT reason"):
        ExitPolicyOutcome(
            policy=ExitPolicyKind.TRAILING_STOP,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.TRAILING_STOP_TRIGGERED,
            summary="this must not be constructible",
        )


def test_an_exit_cannot_carry_a_block_reason() -> None:
    with pytest.raises(ValidationError, match="not a EXIT reason"):
        ExitPolicyOutcome(
            policy=ExitPolicyKind.DATA_QUALITY,
            decision=ExitDecisionType.EXIT,
            reason_code=ExitReasonCode.MARKET_DATA_UNAVAILABLE,
            summary="this must not be constructible",
        )


# ---------------------------------------------------------------------------
# The decision record
# ---------------------------------------------------------------------------
def _decision(**overrides: object) -> ExitDecisionRecord:
    payload: dict[str, object] = {
        "decision_id": "exitdec-1",
        "evaluation_id": "exiteval-1",
        "position_id": "strategypos-1",
        "as_of": NOW,
        "decided_at": NOW,
        "decision": ExitDecisionType.WAIT,
        "reason_codes": [ExitReasonCode.POLICY_SATISFIED],
        "underlying": "NVDA",
        "strategy": StrategyType.LONG_CALL,
        "lifecycle_state": PositionLifecycleState.MONITORING,
        "quantity": 2,
        "summary": "nothing triggered",
        "policy_version": "1.0.0",
        "trading_mode": TradingMode.PAPER,
        "versions": factories.versions(),
    }
    payload.update(overrides)
    return ExitDecisionRecord(**payload)


def test_an_exit_names_exactly_one_triggering_policy() -> None:
    with pytest.raises(ValidationError, match="names exactly one triggering policy"):
        _decision(
            decision=ExitDecisionType.EXIT,
            reason_codes=[
                ExitReasonCode.TRAILING_STOP_TRIGGERED,
                ExitReasonCode.TAKE_PROFIT_REACHED,
            ],
            triggering_policy=ExitPolicyKind.TRAILING_STOP,
        )


def test_an_exit_without_a_policy_behind_it_is_not_constructible() -> None:
    with pytest.raises(ValidationError, match="must name the policy that triggered it"):
        _decision(
            decision=ExitDecisionType.EXIT,
            reason_codes=[ExitReasonCode.MAX_LOSS_REACHED],
            triggering_policy=None,
        )


def test_an_exit_for_nothing_held_is_not_an_exit() -> None:
    with pytest.raises(ValidationError, match="is not an exit"):
        _decision(
            decision=ExitDecisionType.EXIT,
            reason_codes=[ExitReasonCode.MAX_LOSS_REACHED],
            triggering_policy=ExitPolicyKind.MAX_LOSS,
            quantity=0,
        )


def test_a_decision_cannot_close_part_of_a_structure() -> None:
    """There is no independent-leg exit path, and this is the field that would lie."""
    with pytest.raises(ValidationError, match="cannot close part of a structure"):
        _decision(close_whole_strategy=False)


def test_a_wait_cannot_carry_an_exit_reason() -> None:
    with pytest.raises(ValidationError, match="belong to a different verdict"):
        _decision(reason_codes=[ExitReasonCode.THESIS_INVALIDATED])


# ---------------------------------------------------------------------------
# Projection onto the Milestone 1 boundary
# ---------------------------------------------------------------------------
def test_a_wait_projects_onto_hold() -> None:
    projected = _decision().to_exit_decision()

    assert projected.decision is ExitAction.HOLD
    assert projected.reason is None
    assert projected.close_whole_strategy is True


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ExitReasonCode.TRAILING_STOP_TRIGGERED, ExitReason.TRAILING_STOP),
        (ExitReasonCode.EXPIRATION_FORCE_EXIT, ExitReason.EXPIRATION_POLICY),
        (ExitReasonCode.THESIS_INVALIDATED, ExitReason.THESIS_INVALIDATION),
        (ExitReasonCode.MAX_LOSS_REACHED, ExitReason.RISK_LIMIT),
        (ExitReasonCode.TAKE_PROFIT_REACHED, ExitReason.TAKE_PROFIT),
    ],
)
def test_every_trigger_projects_onto_a_milestone_1_reason(
    code: ExitReasonCode, expected: ExitReason
) -> None:
    projected = _decision(
        decision=ExitDecisionType.EXIT,
        reason_codes=[code],
        triggering_policy=ExitPolicyKind.TRAILING_STOP,
    ).to_exit_decision()

    assert projected.decision is ExitAction.SELL
    assert projected.reason is expected


def test_a_block_raises_rather_than_projecting_onto_hold() -> None:
    """``HOLD`` would claim a decision to keep this position; none was made."""
    decision = _decision(
        decision=ExitDecisionType.BLOCK,
        reason_codes=[ExitReasonCode.MARKET_DATA_UNAVAILABLE],
    )

    with pytest.raises(ValueError, match="cannot express"):
        decision.to_exit_decision()


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------
def test_a_structure_cannot_report_a_price_while_a_leg_is_unpriced() -> None:
    from trading_system.exit.models import ExitLegValuation

    with pytest.raises(ValidationError, match="priced from the legs that happened"):
        PositionValuation(
            as_of=NOW,
            quote_field=ExitQuoteField.BID,
            multiplier=100,
            open_quantity=1,
            legs=[
                ExitLegValuation(
                    leg_index=0, key="cid:1", quote_field=ExitQuoteField.BID, price=Decimal("6.00")
                ),
                ExitLegValuation(leg_index=1, key="cid:2", quote_field=ExitQuoteField.BID),
            ],
            exit_quote=Decimal("6.00"),
        )


def test_return_pct_is_none_without_both_sides() -> None:
    assert factories.valuation(exit_quote=None).return_pct is None
    assert factories.valuation(entry_quote=None).return_pct is None


def test_return_pct_is_the_same_in_quoted_terms_and_in_money() -> None:
    """Both sides carry the same multiplier, so the ratio is unaffected."""
    priced = factories.valuation(exit_quote=Decimal("9.00"), entry_quote=Decimal("6.00"))

    assert priced.return_pct == Decimal("50")
    assert priced.exit_value == Decimal("900.00")
    assert priced.entry_cost == Decimal("600.00")
    assert priced.exit_total == Decimal("1800.00")


# ---------------------------------------------------------------------------
# The thesis check
# ---------------------------------------------------------------------------
def test_a_verdict_must_name_the_fact_that_settled_it() -> None:
    from trading_system.domain.enums import ThesisConditionOutcome

    with pytest.raises(ValidationError, match="without naming"):
        ThesisConditionCheck(
            condition="the guidance is cut",
            outcome=ThesisConditionOutcome.VIOLATED,
        )


def test_an_unevaluated_condition_carries_no_evidence() -> None:
    with pytest.raises(ValidationError, match="nothing was checked"):
        ThesisConditionCheck(condition="the story changes", evidence="something")


# ---------------------------------------------------------------------------
# The request handed to Milestone 8
# ---------------------------------------------------------------------------
def _request(**overrides: object) -> ExitRequest:
    payload: dict[str, object] = {
        "exit_request_id": "exit-req-1",
        "position_id": "strategypos-1",
        "decision_id": "exitdec-1",
        "evaluation_id": "exiteval-1",
        "created_at": NOW,
        "exit_authorized": True,
        "underlying": "NVDA",
        "strategy": StrategyType.LONG_CALL,
        "quantity": 2,
        "exit_reason": ExitReasonCode.MAX_LOSS_REACHED,
        "triggering_policy": ExitPolicyKind.MAX_LOSS,
        "reference_quote": Decimal("6.50"),
        "quote_field": ExitQuoteField.BID,
        "order_type": OrderType.LIMIT,
        "time_in_force": TimeInForce.DAY,
        "trading_mode": TradingMode.PAPER,
        "entry_execution_id": "execution-entry-1",
        "allocation_id": "allocation-1",
        "campaign_id": "campaign-001",
        "opportunity_id": "opportunity-1",
        "policy_version": "1.0.0",
        "versions": factories.versions(),
    }
    payload.update(overrides)
    return ExitRequest(**payload)


def test_an_unauthorised_exit_request_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError, match="exit_authorized=True"):
        _request(exit_authorized=False)


def test_an_exit_request_cannot_close_part_of_a_structure() -> None:
    with pytest.raises(ValidationError, match="cannot close part of a structure"):
        _request(close_whole_strategy=False)


def test_only_a_policy_that_triggered_may_produce_a_request() -> None:
    with pytest.raises(ValidationError, match="not a reason to exit"):
        _request(exit_reason=ExitReasonCode.THESIS_INTACT)


def test_the_exit_request_identity_excludes_the_reason_and_the_clock() -> None:
    """An exit triggered twice for different reasons is one order for one position."""
    first = exit_request_identifier(
        position_id="strategypos-1",
        entry_execution_id="execution-entry-1",
        trading_mode=TradingMode.PAPER,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        policy_version="1.0.0",
    )
    second = exit_request_identifier(
        position_id="strategypos-1",
        entry_execution_id="execution-entry-1",
        trading_mode=TradingMode.PAPER,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        policy_version="1.0.0",
    )

    assert first == second
    assert first.startswith("exit-req-")


def test_an_exit_request_identity_never_collides_with_an_entry_one() -> None:
    """Different prefixes, so the two can never be confused in the ledger."""
    from trading_system.execution.models import execution_request_identifier

    entry = execution_request_identifier(
        allocation_id="allocation-1",
        trading_mode=TradingMode.PAPER,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        policy_version="1.0.0",
    )
    exit_identity = exit_request_identifier(
        position_id="strategypos-1",
        entry_execution_id="execution-entry-1",
        trading_mode=TradingMode.PAPER,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        policy_version="1.0.0",
    )

    assert entry.startswith("exec-req-")
    assert exit_identity.startswith("exit-req-")
    assert entry != exit_identity


# ---------------------------------------------------------------------------
# Evaluations and runs submit nothing
# ---------------------------------------------------------------------------
def _evaluation(**overrides: object) -> ExitEvaluation:
    payload: dict[str, object] = {
        "evaluation_id": "exiteval-1",
        "position_id": "strategypos-1",
        "as_of": NOW,
        "evaluated_at": NOW,
        "underlying": "NVDA",
        "strategy": StrategyType.LONG_CALL,
        "lifecycle_state": PositionLifecycleState.MONITORING,
        "structure_status": StructureStatus.COMPLETE,
        "open_quantity": 2,
        "valuation": factories.valuation(),
        "policy": factories.policy_snapshot(),
        "content_hash": "abc",
        "versions": factories.versions(),
    }
    payload.update(overrides)
    return ExitEvaluation(**payload)


def test_an_evaluation_cannot_report_a_submitted_order() -> None:
    with pytest.raises(ValidationError, match="never places an order"):
        _evaluation(orders_submitted=1)


def test_an_unauthorised_run_cannot_report_a_submission() -> None:
    with pytest.raises(ValidationError, match="never closes one"):
        ExitRunResult(
            run_id="exitrun-1",
            campaign_id="campaign-001",
            as_of=NOW,
            generated_at=NOW,
            status="SUCCESS",
            trading_mode=TradingMode.PAPER,
            policy_version="1.0.0",
            execution_authorized=False,
            orders_submitted=1,
            versions=factories.versions(),
        )


def test_an_unauthorised_run_cannot_have_created_an_exit_execution() -> None:
    with pytest.raises(ValidationError, match="separate acts"):
        ExitRunResult(
            run_id="exitrun-1",
            campaign_id="campaign-001",
            as_of=NOW,
            generated_at=NOW,
            status="SUCCESS",
            trading_mode=TradingMode.PAPER,
            policy_version="1.0.0",
            execution_authorized=False,
            exit_execution_ids=["execution-exit-1"],
            versions=factories.versions(),
        )


# ---------------------------------------------------------------------------
# The policy snapshot
# ---------------------------------------------------------------------------
def test_a_policy_snapshot_cannot_permit_an_independent_leg_exit() -> None:
    with pytest.raises(ValidationError, match="independent leg exit"):
        ExitPolicySnapshot(
            policy_version="1.0.0",
            strategy=StrategyType.LONG_STRADDLE,
            expiration_warning_dte=10,
            expiration_force_exit_dte=5,
            trailing_activation_return_pct=25.0,
            trailing_distance_pct=30.0,
            trailing_min_improvement_pct=1.0,
            max_loss_pct=50.0,
            max_quote_age_seconds=900,
            allow_independent_leg_exit=True,
        )


# ---------------------------------------------------------------------------
# The trailing record's own invariants
# ---------------------------------------------------------------------------
def test_an_inactive_trail_cannot_carry_a_level() -> None:
    with pytest.raises(ValidationError, match="nothing to trail from"):
        factories.trailing_record(stop_quote=Decimal("5.00"))


def test_an_active_trail_without_a_level_is_not_a_stop() -> None:
    with pytest.raises(ValidationError, match="not a stop"):
        factories.trailing_record(state=TrailingStopState.ACTIVE)


def test_a_level_can_never_sit_above_its_peak() -> None:
    with pytest.raises(ValidationError, match="better than it ever reached"):
        factories.trailing_record(
            state=TrailingStopState.ACTIVE,
            peak_quote=Decimal("8.00"),
            stop_quote=Decimal("9.00"),
        )


def test_a_triggered_trail_records_the_observation_that_crossed_it() -> None:
    with pytest.raises(ValidationError, match="whole explanation"):
        factories.trailing_record(
            state=TrailingStopState.TRIGGERED,
            peak_quote=Decimal("8.00"),
            stop_quote=Decimal("5.60"),
        )


# ---------------------------------------------------------------------------
# There is no money, no sizing and no model anywhere in these shapes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "forbidden",
    ["budget", "allocation_amount", "position_size", "model_name", "prompt", "rationale"],
)
def test_no_exit_artifact_can_express_a_budget_a_size_or_a_model(forbidden: str) -> None:
    """Milestone 10 decides no money and consults no model.

    Enforced on the shapes rather than in prose: a field that does not exist
    cannot be filled in by a later change that looked reasonable in review.
    """
    for model in (
        ExitDecisionRecord,
        ExitEvaluation,
        ExitRequest,
        ExitPolicySnapshot,
        PositionValuation,
    ):
        assert forbidden not in model.model_fields, f"{model.__name__} exposes {forbidden}"


def test_an_evaluation_id_is_content_derived_and_clock_free() -> None:
    """Two evaluations of the same state at the same instant share an id."""
    from trading_system.exit.models import exit_evaluation_identifier

    first = exit_evaluation_identifier(
        position_id="p", as_of=NOW, content_digest="d", policy_version="1.0.0"
    )
    second = exit_evaluation_identifier(
        position_id="p", as_of=NOW, content_digest="d", policy_version="1.0.0"
    )
    different = exit_evaluation_identifier(
        position_id="p", as_of=NOW, content_digest="other", policy_version="1.0.0"
    )

    assert first == second
    assert first != different


def test_a_trailing_state_id_does_not_move_with_the_clock() -> None:
    """A position has one trailing stop for its whole life, and the fold finds it by id."""
    from trading_system.exit.models import trailing_state_identifier

    assert trailing_state_identifier(position_id="p") == trailing_state_identifier(position_id="p")


def test_the_fixed_instant_is_inside_a_verified_session() -> None:
    """A guard on the suite's own premise, not on the code.

    Several expiration tests would pass for the wrong reason if NOW drifted
    outside the calendar's covered years.
    """
    assert NOW.tzinfo is UTC
    assert datetime(2026, 8, 10, 14, 30, tzinfo=UTC) == NOW
