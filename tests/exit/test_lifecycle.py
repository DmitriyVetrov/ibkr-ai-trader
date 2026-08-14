"""The position lifecycle state machine.

Four properties, and each of them is a way a trading system closes a position
it did not mean to close, or believes it closed one it did not.
"""

from __future__ import annotations

import pytest

from tests.exit.factories import NOW
from trading_system.domain.enums import (
    PositionLifecycleEventType,
    PositionLifecycleState,
    StrategyType,
)
from trading_system.exit.lifecycle import (
    ALLOWED_LIFECYCLE_TRANSITIONS,
    InvalidLifecycleTransitionError,
    LifecycleStateMachine,
    can_transition,
    is_terminal,
    validate_lifecycle_transition,
)
from trading_system.exit.models import (
    PositionLifecycleEvent,
    PositionLifecycleSnapshot,
    lifecycle_event_identifier,
)

pytestmark = pytest.mark.unit

_S = PositionLifecycleState


def _snapshot(state: _S = _S.OPEN, *, quantity: int = 2) -> PositionLifecycleSnapshot:
    return PositionLifecycleSnapshot(
        lifecycle_id="lifecycle-1",
        position_id="strategypos-1",
        as_of=NOW,
        updated_at=NOW,
        state=state,
        underlying="NVDA",
        strategy=StrategyType.LONG_CALL,
        open_quantity=quantity,
        entry_execution_id="execution-entry-1",
        exit_execution_id=(
            "execution-exit-1" if state in (_S.EXIT_SUBMITTED, _S.EXIT_UNKNOWN) else None
        ),
        blocked_reason=(
            None
            if state is not _S.BLOCKED
            else __import__(
                "trading_system.domain.enums", fromlist=["ExitReasonCode"]
            ).ExitReasonCode.MARKET_DATA_UNAVAILABLE
        ),
    )


def _event(
    state: _S,
    *,
    sequence: int = 0,
    event_type: PositionLifecycleEventType = PositionLifecycleEventType.LIFECYCLE_MONITORED,
    quantity: int | None = None,
    execution_id: str | None = None,
) -> PositionLifecycleEvent:
    from trading_system.domain.enums import ExitReasonCode

    return PositionLifecycleEvent(
        event_id=lifecycle_event_identifier(
            position_id="strategypos-1", sequence=sequence, event_type=event_type.value
        ),
        position_id="strategypos-1",
        sequence=sequence,
        event_type=event_type,
        state=state,
        occurred_at=NOW,
        observed_at=NOW,
        source="test",
        reason_code=ExitReasonCode.MARKET_DATA_UNAVAILABLE if state is _S.BLOCKED else None,
        open_quantity=quantity,
        exit_execution_id=execution_id,
    )


# ---------------------------------------------------------------------------
# The graph is total and closed
# ---------------------------------------------------------------------------
def test_every_state_has_an_entry_in_the_graph() -> None:
    """A missing key would raise ``KeyError`` at the worst possible moment."""
    assert set(ALLOWED_LIFECYCLE_TRANSITIONS) == set(PositionLifecycleState)


def test_closed_is_terminal_and_nothing_leaves_it() -> None:
    assert ALLOWED_LIFECYCLE_TRANSITIONS[_S.CLOSED] == frozenset()
    assert is_terminal(_S.CLOSED)
    for state in PositionLifecycleState:
        assert not can_transition(_S.CLOSED, state), f"CLOSED must not reach {state.value}"


def test_nothing_ever_returns_to_open() -> None:
    """``OPEN`` means "exit management has not looked at this yet"; true once."""
    for state in PositionLifecycleState:
        assert not can_transition(state, _S.OPEN)


def test_an_unknown_exit_never_returns_to_a_state_that_permits_sending() -> None:
    """The single most important edge in the graph, asserted as an absence.

    ``EXIT_UNKNOWN`` means an order may be live at the broker right now. Every
    edge out of it must be something the broker turned out to be; none of them
    may be "try again".
    """
    reachable = ALLOWED_LIFECYCLE_TRANSITIONS[_S.EXIT_UNKNOWN]

    assert _S.EXIT_SUBMITTED not in reachable
    assert _S.EXIT_REQUIRED not in reachable
    assert _S.MONITORING not in reachable
    assert _S.TRAILING_ACTIVE not in reachable
    assert reachable == frozenset({_S.CLOSED, _S.BLOCKED})


def test_a_submitted_exit_cannot_return_to_required() -> None:
    """That edge would permit a second submission for one position."""
    assert not can_transition(_S.EXIT_SUBMITTED, _S.EXIT_REQUIRED)


def test_a_required_exit_stays_required_until_it_is_sent_or_the_position_is_gone() -> None:
    """``EXIT_UNKNOWN`` is reachable directly: a submission that timed out never
    reached ``EXIT_SUBMITTED``, because nothing acknowledged it, and recording
    it as merely *required* would hide that an order may be live."""
    assert ALLOWED_LIFECYCLE_TRANSITIONS[_S.EXIT_REQUIRED] == frozenset(
        {_S.EXIT_SUBMITTED, _S.EXIT_UNKNOWN, _S.CLOSED, _S.BLOCKED}
    )


def test_a_blocked_position_can_still_reach_a_required_exit() -> None:
    """A block is re-derived every run, so a later force-exit is not suppressed."""
    assert can_transition(_S.BLOCKED, _S.EXIT_REQUIRED)
    assert not can_transition(_S.BLOCKED, _S.EXIT_SUBMITTED)


def test_a_trail_cannot_quietly_deactivate() -> None:
    """Returning to MONITORING would restart the trail from the current price."""
    assert not can_transition(_S.TRAILING_ACTIVE, _S.MONITORING)


# ---------------------------------------------------------------------------
# Refusals leave state and history untouched
# ---------------------------------------------------------------------------
def test_an_illegal_transition_raises_and_names_what_is_allowed() -> None:
    with pytest.raises(InvalidLifecycleTransitionError, match="allowed: "):
        validate_lifecycle_transition(_S.CLOSED, _S.MONITORING)


def test_a_refused_transition_does_not_append_to_the_history() -> None:
    """A rejected transition that still logged would claim something happened."""
    machine = LifecycleStateMachine("strategypos-1", _S.EXIT_UNKNOWN)

    with pytest.raises(InvalidLifecycleTransitionError):
        machine.transition_to(_S.EXIT_SUBMITTED, NOW)

    assert machine.state is _S.EXIT_UNKNOWN
    assert machine.history == ()


def test_a_permitted_transition_is_recorded() -> None:
    machine = LifecycleStateMachine("strategypos-1", _S.MONITORING)

    transition = machine.transition_to(_S.EXIT_REQUIRED, NOW, reason="max loss")

    assert machine.state is _S.EXIT_REQUIRED
    assert transition.from_state is _S.MONITORING
    assert len(machine.history) == 1


# ---------------------------------------------------------------------------
# Folding an event onto a snapshot revalidates
# ---------------------------------------------------------------------------
def test_folding_an_event_validates_the_transition() -> None:
    closed = _snapshot(_S.CLOSED, quantity=0)

    with pytest.raises(InvalidLifecycleTransitionError):
        closed.with_event(_event(_S.MONITORING))


def test_a_closed_record_that_still_holds_contracts_cannot_be_constructed() -> None:
    """The invariant the fold reconstructs through the model to enforce.

    ``with_event`` rebuilds through ``model_validate`` rather than
    ``model_copy`` precisely so a record that cannot be true fails here rather
    than surfacing later as a wrong screen. This asserts the validator that
    makes that worth doing.
    """
    from pydantic import ValidationError

    payload = _snapshot(_S.MONITORING).model_dump()
    payload.update({"state": _S.CLOSED, "open_quantity": 2})

    with pytest.raises(ValidationError, match="CLOSED while the broker reports"):
        PositionLifecycleSnapshot.model_validate(payload)


def test_closing_zeroes_the_held_quantity() -> None:
    folded = _snapshot(_S.MONITORING).with_event(
        _event(_S.CLOSED, event_type=PositionLifecycleEventType.EXIT_CONFIRMED_CLOSED)
    )

    assert folded.state is _S.CLOSED
    assert folded.open_quantity == 0
    assert folded.closed_at == NOW
    assert folded.terminal


def test_an_event_for_another_position_is_refused() -> None:
    with pytest.raises(ValueError, match="belongs to position"):
        _snapshot().with_event(
            _event(_S.MONITORING).model_copy(update={"position_id": "strategypos-other"})
        )


def test_a_block_is_cleared_when_the_position_moves_on() -> None:
    """The reason is re-derived per evaluation, so leaving the state clears it."""
    blocked = _snapshot(_S.BLOCKED)
    assert blocked.blocked_reason is not None

    resumed = blocked.with_event(_event(_S.MONITORING))

    assert resumed.state is _S.MONITORING
    assert resumed.blocked_reason is None


def test_a_submitted_exit_must_name_its_execution() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="names no exit execution"):
        PositionLifecycleSnapshot(
            lifecycle_id="lifecycle-1",
            position_id="strategypos-1",
            as_of=NOW,
            updated_at=NOW,
            state=_S.EXIT_SUBMITTED,
            underlying="NVDA",
            strategy=StrategyType.LONG_CALL,
            open_quantity=2,
        )


def test_a_position_waiting_on_an_exit_may_not_submit_another() -> None:
    for state in (_S.EXIT_SUBMITTED, _S.EXIT_UNKNOWN, _S.CLOSED):
        assert not _snapshot(state, quantity=0 if state is _S.CLOSED else 2).may_submit_exit
    for state in (_S.OPEN, _S.MONITORING, _S.TRAILING_ACTIVE, _S.EXIT_REQUIRED, _S.BLOCKED):
        assert _snapshot(state).may_submit_exit
