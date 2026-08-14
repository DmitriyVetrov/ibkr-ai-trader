"""Persistence: immutable judgements, an append-only history, a folded lifecycle.

The property that matters most here is the restart guarantee. Nothing in exit
management lives in process memory: the trailing level, the lifecycle state and
every past judgement are read from disk on each run, which is what makes a
scheduled monitor safe to kill and restart at any point.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.domain.enums import (
    ExitDecisionType,
    ExitReasonCode,
    PositionLifecycleEventType,
    PositionLifecycleState,
    StrategyType,
    TradingMode,
)
from trading_system.exit.models import (
    ExitDecisionRecord,
    ExitEvaluation,
    PositionLifecycleEvent,
    PositionLifecycleSnapshot,
    lifecycle_event_identifier,
)
from trading_system.exit.store import ExitStoreError, FilesystemExitRepository

pytestmark = pytest.mark.unit

_S = PositionLifecycleState


def _evaluation(**overrides: object) -> ExitEvaluation:
    from trading_system.domain.enums import StructureStatus

    payload: dict[str, object] = {
        "evaluation_id": "exiteval-1",
        "position_id": "strategypos-1",
        "as_of": NOW,
        "evaluated_at": NOW,
        "underlying": "NVDA",
        "strategy": StrategyType.LONG_CALL,
        "lifecycle_state": _S.MONITORING,
        "structure_status": StructureStatus.COMPLETE,
        "open_quantity": 2,
        "valuation": factories.valuation(),
        "policy": factories.policy_snapshot(),
        "content_hash": "hash-1",
        "versions": factories.versions(),
    }
    payload.update(overrides)
    return ExitEvaluation(**payload)


def _decision(evaluation: ExitEvaluation, **overrides: object) -> ExitDecisionRecord:
    payload: dict[str, object] = {
        "decision_id": "exitdec-1",
        "evaluation_id": evaluation.evaluation_id,
        "position_id": evaluation.position_id,
        "as_of": evaluation.as_of,
        "decided_at": evaluation.evaluated_at,
        "decision": ExitDecisionType.WAIT,
        "reason_codes": [ExitReasonCode.POLICY_SATISFIED],
        "underlying": evaluation.underlying,
        "strategy": evaluation.strategy,
        "lifecycle_state": evaluation.lifecycle_state,
        "quantity": 2,
        "summary": "nothing triggered",
        "policy_version": "1.0.0",
        "trading_mode": TradingMode.PAPER,
        "versions": factories.versions(),
    }
    payload.update(overrides)
    return ExitDecisionRecord(**payload)


def _lifecycle(state: _S = _S.OPEN) -> PositionLifecycleSnapshot:
    return PositionLifecycleSnapshot(
        lifecycle_id="lifecycle-1",
        position_id="strategypos-1",
        as_of=NOW,
        updated_at=NOW,
        state=state,
        underlying="NVDA",
        strategy=StrategyType.LONG_CALL,
        open_quantity=2,
        entry_execution_id="execution-entry-1",
    )


def _event(
    state: _S,
    *,
    sequence: int,
    event_type: PositionLifecycleEventType = PositionLifecycleEventType.LIFECYCLE_MONITORED,
) -> PositionLifecycleEvent:
    return PositionLifecycleEvent(
        event_id=lifecycle_event_identifier(
            position_id="strategypos-1", sequence=sequence, event_type=event_type.value
        ),
        position_id="strategypos-1",
        sequence=sequence,
        event_type=event_type,
        state=state,
        occurred_at=NOW + timedelta(minutes=sequence),
        observed_at=NOW + timedelta(minutes=sequence),
        source="test",
    )


# ---------------------------------------------------------------------------
# Evaluations are immutable
# ---------------------------------------------------------------------------
def test_a_judgement_is_written_once_and_read_back(exit_repo) -> None:
    evaluation = _evaluation()
    decision = _decision(evaluation)

    _, is_new = exit_repo.save_evaluation(evaluation, decision)

    assert is_new is True
    assert exit_repo.get_evaluation("exiteval-1") == evaluation
    assert exit_repo.get_decision("exitdec-1") == decision


def test_storing_the_same_judgement_again_is_a_re_observation(exit_repo) -> None:
    evaluation = _evaluation()
    decision = _decision(evaluation)
    exit_repo.save_evaluation(evaluation, decision)

    _, is_new = exit_repo.save_evaluation(evaluation, decision)

    assert is_new is False
    assert len(exit_repo.history()) == 2
    assert sum(1 for entry in exit_repo.history() if entry.reobserved) == 1


def test_a_changed_judgement_under_an_existing_id_is_refused(exit_repo) -> None:
    """A changed judgement is a new evaluation, not an edit."""
    evaluation = _evaluation()
    exit_repo.save_evaluation(evaluation, _decision(evaluation))

    with pytest.raises(ExitStoreError, match="immutable"):
        exit_repo.save_evaluation(_evaluation(open_quantity=1), _decision(evaluation))


def test_a_decision_belonging_to_another_evaluation_is_refused(exit_repo) -> None:
    evaluation = _evaluation()

    with pytest.raises(ExitStoreError, match="belongs to evaluation"):
        exit_repo.save_evaluation(evaluation, _decision(_evaluation(evaluation_id="exiteval-2")))


def test_the_history_can_be_filtered_to_one_position(exit_repo) -> None:
    first = _evaluation()
    second = _evaluation(evaluation_id="exiteval-2", position_id="strategypos-2")
    exit_repo.save_evaluation(first, _decision(first))
    exit_repo.save_evaluation(second, _decision(second, decision_id="exitdec-2"))

    assert len(exit_repo.history(position_id="strategypos-1")) == 1
    assert len(exit_repo.history()) == 2


def test_the_latest_decision_for_a_position_is_findable(exit_repo) -> None:
    evaluation = _evaluation()
    decision = _decision(evaluation)
    exit_repo.save_evaluation(evaluation, decision)

    assert exit_repo.latest_for_position("strategypos-1") == decision
    assert exit_repo.latest_for_position("strategypos-unknown") is None


# ---------------------------------------------------------------------------
# The lifecycle is folded from its events
# ---------------------------------------------------------------------------
def test_the_lifecycle_is_reconstructed_from_the_event_stream(exit_repo) -> None:
    exit_repo.save_lifecycle(_lifecycle())
    exit_repo.append_lifecycle_event(_event(_S.MONITORING, sequence=0))
    exit_repo.append_lifecycle_event(
        _event(
            _S.TRAILING_ACTIVE,
            sequence=1,
            event_type=PositionLifecycleEventType.TRAILING_ACTIVATED,
        )
    )

    folded = exit_repo.lifecycle("strategypos-1")

    assert folded is not None
    assert folded.state is _S.TRAILING_ACTIVE


def test_an_unreplayable_history_raises_rather_than_being_skipped(exit_repo) -> None:
    """A history that contradicts itself would otherwise leave the wrong state
    on screen — which here means showing a position as open while an exit order
    for it may be live."""
    exit_repo.save_lifecycle(_lifecycle())
    exit_repo.append_lifecycle_event(
        _event(_S.CLOSED, sequence=0, event_type=PositionLifecycleEventType.EXIT_CONFIRMED_CLOSED)
    )
    exit_repo.append_lifecycle_event(_event(_S.MONITORING, sequence=1))

    with pytest.raises(ExitStoreError, match="cannot be replayed"):
        exit_repo.lifecycle("strategypos-1")


def test_events_are_ordered_by_sequence_not_by_timestamp(exit_repo) -> None:
    """Two observations can share an instant; the order they were recorded in
    is the order they happened."""
    exit_repo.save_lifecycle(_lifecycle())
    for sequence in (0, 1):
        exit_repo.append_lifecycle_event(
            _event(_S.MONITORING, sequence=sequence).model_copy(
                update={"occurred_at": NOW, "observed_at": NOW}
            )
        )

    events = exit_repo.lifecycle_events("strategypos-1")

    assert [event.sequence for event in events] == [0, 1]


def test_the_base_lifecycle_is_never_rewritten(exit_repo) -> None:
    """An edited anchor would silently change what every stored event means."""
    exit_repo.save_lifecycle(_lifecycle())

    exit_repo.save_lifecycle(_lifecycle(_S.MONITORING))

    stored = exit_repo.lifecycle("strategypos-1")
    assert stored is not None
    assert stored.state is _S.OPEN


def test_every_lifecycle_can_be_listed(exit_repo) -> None:
    exit_repo.save_lifecycle(_lifecycle())
    exit_repo.save_lifecycle(
        _lifecycle().model_copy(update={"position_id": "strategypos-2", "underlying": "SPY"})
    )

    assert len(exit_repo.all_lifecycles()) == 2


# ---------------------------------------------------------------------------
# Trailing state survives the process
# ---------------------------------------------------------------------------
def test_a_trailing_record_round_trips(exit_repo) -> None:
    from trading_system.domain.enums import TrailingStopState

    record = factories.trailing_record(
        state=TrailingStopState.ACTIVE,
        peak_quote=Decimal("12.00"),
        stop_quote=Decimal("8.40"),
    )
    exit_repo.save_trailing(record)

    assert exit_repo.trailing("strategypos-1") == record


def test_a_trailing_record_is_updated_in_place_and_its_history_lives_elsewhere(
    exit_repo,
) -> None:
    """The one deliberately mutable record in this milestone, and the reason:
    a trailing stop is one continuously-updated fact, and an immutable file per
    observation would produce thousands of near-identical records for a level
    that moved three times. The *movements* are lifecycle events."""
    from trading_system.domain.enums import TrailingStopState

    exit_repo.save_trailing(factories.trailing_record())
    exit_repo.save_trailing(
        factories.trailing_record(
            state=TrailingStopState.ACTIVE,
            peak_quote=Decimal("12.00"),
            stop_quote=Decimal("8.40"),
        )
    )

    stored = exit_repo.trailing("strategypos-1")
    assert stored is not None
    assert stored.peak_quote == Decimal("12.00")


def test_an_absent_trailing_record_is_none_rather_than_an_empty_one(exit_repo) -> None:
    assert exit_repo.trailing("strategypos-never-seen") is None


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def test_a_run_is_stored_immutably_and_indexed(exit_repo) -> None:
    from trading_system.domain.enums import ExitRunStatus
    from trading_system.exit.models import ExitRunResult

    result = ExitRunResult(
        run_id="exitrun-1",
        campaign_id="campaign-001",
        as_of=NOW,
        generated_at=NOW,
        status=ExitRunStatus.SUCCESS,
        trading_mode=TradingMode.PAPER,
        policy_version="1.0.0",
        versions=factories.versions(),
    )

    exit_repo.save_run(result)

    assert exit_repo.get_run("exitrun-1") == result
    assert exit_repo.latest_run() == result
    assert len(exit_repo.run_history()) == 1


def test_a_changed_run_under_an_existing_id_is_refused(exit_repo) -> None:
    from trading_system.domain.enums import ExitRunStatus
    from trading_system.exit.models import ExitRunResult

    result = ExitRunResult(
        run_id="exitrun-1",
        campaign_id="campaign-001",
        as_of=NOW,
        generated_at=NOW,
        status=ExitRunStatus.SUCCESS,
        trading_mode=TradingMode.PAPER,
        policy_version="1.0.0",
        versions=factories.versions(),
    )
    exit_repo.save_run(result)

    with pytest.raises(ExitStoreError, match="immutable"):
        exit_repo.save_run(result.model_copy(update={"status": ExitRunStatus.PARTIAL}))


def test_an_empty_store_answers_none_rather_than_raising(tmp_path) -> None:
    repository = FilesystemExitRepository(tmp_path / "exit")

    assert repository.history() == []
    assert repository.run_history() == []
    assert repository.latest_run() is None
    assert repository.all_lifecycles() == []
    assert repository.lifecycle("nothing") is None
