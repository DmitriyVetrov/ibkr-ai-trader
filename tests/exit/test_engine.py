"""Precedence, and the one rule that combines the policies.

    The first policy in precedence order that does not say WAIT decides.

Two tempting alternatives are wrong, and both are tested here: a later block
must not veto an earlier exit (or a missing research file could disable the
expiration policy), and an earlier block must beat a later exit (or a profit
computed from a disputed quantity would sell the position).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.data.market_calendar import MarketCalendar
from trading_system.domain.enums import (
    EXIT_POLICY_PRECEDENCE,
    BrokerReadStatus,
    ExitDecisionType,
    ExitPolicyKind,
    ExitReasonCode,
    MaxLossBasis,
    PositionLifecycleState,
    StrategyType,
    StructureStatus,
    TradingMode,
)
from trading_system.exit.engine import ExitInputs, ExitPolicyEngine
from trading_system.exit.expiration import expiration_view
from trading_system.exit.thesis import ThesisView, check_conditions
from trading_system.infrastructure.settings import SystemConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(system_config: SystemConfig) -> ExitPolicyEngine:
    return ExitPolicyEngine(system_config.exit)


@pytest.fixture
def calendar(system_config: SystemConfig) -> MarketCalendar:
    return MarketCalendar(system_config.data.market_calendar)


def _inputs(
    calendar: MarketCalendar,
    *,
    expiration: date = factories.EXPIRATION,
    exit_quote: Decimal | None = Decimal("6.50"),
    entry_quote: Decimal | None = Decimal("6.00"),
    observed_quantity: int | None = 2,
    expected_quantity: int = 2,
    structure_status: StructureStatus = StructureStatus.COMPLETE,
    lifecycle_state: PositionLifecycleState = PositionLifecycleState.MONITORING,
    broker_read_status: BrokerReadStatus = BrokerReadStatus.OK,
    max_loss_basis: MaxLossBasis | None = MaxLossBasis.NET_DEBIT_PAID,
    trailing: object | None = None,
    thesis: ThesisView | None = None,
    quote_age_seconds: float | None = 30.0,
    **overrides: object,
) -> ExitInputs:
    view = thesis if thesis is not None else ThesisView(conditions=(("prose only", None),))
    valuation = factories.valuation(
        exit_quote=exit_quote, entry_quote=entry_quote, quote_age_seconds=quote_age_seconds
    )
    payload: dict[str, object] = {
        "position_id": "strategypos-1",
        "underlying": "NVDA",
        "strategy": StrategyType.LONG_CALL,
        "as_of": NOW,
        "evaluated_at": NOW,
        "lifecycle_state": lifecycle_state,
        "structure_status": structure_status,
        "expected_quantity": expected_quantity,
        "observed_quantity": observed_quantity,
        "broker_read_status": broker_read_status,
        "valuation": valuation,
        "expiration": expiration_view([expiration], as_of=NOW, calendar=calendar),
        "policy": factories.policy_snapshot(),
        "versions": factories.versions(),
        "trailing": trailing or factories.trailing_record(),
        "thesis": view,
        "thesis_checks": tuple(check_conditions(view, at=NOW)),
        "max_loss_basis": max_loss_basis,
        "max_loss_total": (
            entry_quote * Decimal(100) * Decimal(2) if entry_quote is not None else None
        ),
    }
    payload.update(overrides)
    return ExitInputs(**payload)  # type: ignore[arg-type]


def _decide(engine: ExitPolicyEngine, inputs: ExitInputs):
    evaluation = engine.evaluate(inputs)
    return evaluation, engine.decide(evaluation, trading_mode=TradingMode.PAPER)


# ---------------------------------------------------------------------------
# Every policy runs, in the reviewable order
# ---------------------------------------------------------------------------
def test_the_outcomes_come_back_in_precedence_order(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    """The reviewable list and the executed order cannot drift apart."""
    evaluation = engine.evaluate(_inputs(calendar))

    assert [outcome.policy for outcome in evaluation.outcomes] == list(EXIT_POLICY_PRECEDENCE)


def test_every_policy_still_runs_after_a_block_is_found(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    """Resolving a block should not mean discovering the next problem one run later."""
    evaluation = engine.evaluate(
        _inputs(calendar, structure_status=StructureStatus.PARTIAL, observed_quantity=1)
    )

    assert len(evaluation.outcomes) == len(EXIT_POLICY_PRECEDENCE)
    assert evaluation.outcome_for(ExitPolicyKind.TAKE_PROFIT) is not None


# ---------------------------------------------------------------------------
# The one rule
# ---------------------------------------------------------------------------
def test_nothing_triggered_is_a_wait(engine: ExitPolicyEngine, calendar: MarketCalendar) -> None:
    _, decision = _decide(engine, _inputs(calendar))

    assert decision.decision is ExitDecisionType.WAIT


def test_an_earlier_block_beats_a_later_exit(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    """A position at its take-profit whose quantity the broker disputes blocks:
    the profit figure was computed from a quantity nobody confirmed."""
    _, decision = _decide(
        engine,
        _inputs(
            calendar,
            observed_quantity=1,
            expected_quantity=2,
            exit_quote=Decimal("18.00"),
        ),
    )

    assert decision.decision is ExitDecisionType.BLOCK
    assert decision.primary_reason is ExitReasonCode.POSITION_QUANTITY_MISMATCH


def test_a_later_block_does_not_veto_an_earlier_exit(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    """The safety property this ordering exists for.

    A position one day from expiry whose research report cannot be read must
    still be force-exited. Letting the missing file suppress the deadline would
    mean the most important policy in the milestone could be disabled by
    deleting something unrelated to it.
    """
    _, decision = _decide(
        engine,
        _inputs(
            calendar,
            expiration=date(2026, 8, 11),
            thesis=ThesisView(unavailable=True, detail="not in the store"),
        ),
    )

    assert decision.decision is ExitDecisionType.EXIT
    assert decision.primary_reason is ExitReasonCode.EXPIRATION_FORCE_EXIT
    assert decision.triggering_policy is ExitPolicyKind.EXPIRATION


def test_expiration_beats_take_profit(engine: ExitPolicyEngine, calendar: MarketCalendar) -> None:
    """Both trigger; the deadline is recorded, because it comes first."""
    evaluation, decision = _decide(
        engine, _inputs(calendar, expiration=date(2026, 8, 11), exit_quote=Decimal("18.00"))
    )

    take_profit = evaluation.outcome_for(ExitPolicyKind.TAKE_PROFIT)
    assert take_profit is not None
    assert take_profit.decision is ExitDecisionType.EXIT

    assert decision.primary_reason is ExitReasonCode.EXPIRATION_FORCE_EXIT
    assert len(decision.reason_codes) == 1


def test_max_loss_beats_take_profit_and_trailing(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    _, decision = _decide(engine, _inputs(calendar, exit_quote=Decimal("2.00")))

    assert decision.decision is ExitDecisionType.EXIT
    assert decision.primary_reason is ExitReasonCode.MAX_LOSS_REACHED


def test_an_unpriced_structure_blocks_before_any_money_policy_is_believed(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    evaluation, decision = _decide(engine, _inputs(calendar, exit_quote=None))

    assert decision.decision is ExitDecisionType.BLOCK
    assert decision.primary_reason is ExitReasonCode.MARKET_DATA_UNAVAILABLE
    for kind in (ExitPolicyKind.MAX_LOSS, ExitPolicyKind.TAKE_PROFIT):
        outcome = evaluation.outcome_for(kind)
        assert outcome is not None
        assert outcome.evaluated is False


def test_a_block_reports_every_block_not_only_the_decisive_one(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    evaluation, decision = _decide(
        engine,
        _inputs(
            calendar,
            broker_read_status=BrokerReadStatus.UNAVAILABLE,
            exit_quote=None,
        ),
    )

    assert decision.decision is ExitDecisionType.BLOCK
    assert len(decision.reason_codes) == len(evaluation.blocking_outcomes)
    assert ExitReasonCode.BROKER_DATA_UNAVAILABLE in decision.reason_codes
    assert ExitReasonCode.MARKET_DATA_UNAVAILABLE in decision.reason_codes


def test_a_block_names_what_to_do_and_never_names_a_trade(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    _, decision = _decide(engine, _inputs(calendar, exit_quote=None))

    assert decision.recommended_action is not None
    assert "ACTION REQUIRED" in decision.recommended_action
    assert "no exit order was built" in decision.recommended_action


# ---------------------------------------------------------------------------
# The engine is pure
# ---------------------------------------------------------------------------
def test_the_engine_holds_only_configuration() -> None:
    """No repository, no broker, no clock, no client."""
    import inspect

    parameters = set(inspect.signature(ExitPolicyEngine.__init__).parameters)

    assert parameters == {"self", "config"}


def test_the_same_inputs_produce_a_byte_identical_evaluation(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    """The determinism claim, checked rather than asserted."""
    inputs = _inputs(calendar)

    first = engine.evaluate(inputs)
    second = engine.evaluate(inputs)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.evaluation_id == second.evaluation_id
    assert first.content_hash == second.content_hash


def test_the_content_hash_ignores_the_observation_clock(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    """Re-evaluating unchanged state is a repeat, not a second judgement."""
    from dataclasses import replace

    inputs = _inputs(calendar)
    later = replace(inputs, evaluated_at=datetime(2026, 8, 10, 15, 0, tzinfo=UTC))

    assert engine.evaluate(inputs).content_hash == engine.evaluate(later).content_hash


def test_a_changed_price_changes_the_content_hash(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    first = engine.evaluate(_inputs(calendar, exit_quote=Decimal("6.50")))
    second = engine.evaluate(_inputs(calendar, exit_quote=Decimal("6.60")))

    assert first.content_hash != second.content_hash


# ---------------------------------------------------------------------------
# A position whose inputs could not be assembled
# ---------------------------------------------------------------------------
def test_a_fatal_input_problem_becomes_one_named_block(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    """Running nine policies against inputs known to be wrong would produce
    nine confident verdicts computed from bad data."""
    evaluation, decision = _decide(
        engine,
        _inputs(
            calendar,
            fatal_reason=ExitReasonCode.POINT_IN_TIME_ERROR,
            fatal_detail="a stored quote was not knowable at the evaluation instant",
        ),
    )

    assert len(evaluation.outcomes) == 1
    assert decision.decision is ExitDecisionType.BLOCK
    assert decision.primary_reason is ExitReasonCode.POINT_IN_TIME_ERROR


# ---------------------------------------------------------------------------
# The decision carries the figures it rested on
# ---------------------------------------------------------------------------
def test_a_decision_records_the_numbers_a_person_would_otherwise_reconstruct(
    engine: ExitPolicyEngine, calendar: MarketCalendar
) -> None:
    _, decision = _decide(engine, _inputs(calendar))

    assert decision.exit_quote == Decimal("6.50")
    assert decision.exit_value == Decimal("650.00")
    assert decision.entry_cost == Decimal("600.00")
    assert decision.unrealized_pnl == Decimal("100.00")
    assert decision.days_to_expiration == 39
    assert decision.quantity == 2
    assert decision.close_whole_strategy is True


def test_an_evaluation_submits_nothing(engine: ExitPolicyEngine, calendar: MarketCalendar) -> None:
    assert engine.evaluate(_inputs(calendar)).orders_submitted == 0
