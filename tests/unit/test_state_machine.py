"""Position state machine: legal paths, rejected paths, graph integrity."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_system.domain.enums import TERMINAL_STATES, PositionState
from trading_system.domain.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidStateTransitionError,
    PositionStateMachine,
    can_transition,
    is_terminal,
    validate_transition,
)

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

HAPPY_PATH = [
    PositionState.RESEARCHED,
    PositionState.STRATEGY_SELECTED,
    PositionState.CONTRACT_SELECTED,
    PositionState.ALLOCATED,
    PositionState.RISK_APPROVED,
    PositionState.ORDER_SUBMITTED,
    PositionState.OPEN,
    PositionState.MONITORING,
    PositionState.EXIT_TRIGGERED,
    PositionState.CLOSING,
    PositionState.CLOSED,
]


# ---------------------------------------------------------------------------
# Graph integrity
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_every_state_has_an_entry() -> None:
    """A missing key would raise KeyError at runtime instead of rejecting."""
    assert set(ALLOWED_TRANSITIONS) == set(PositionState)


@pytest.mark.unit
@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_have_no_outgoing_transitions(state: PositionState) -> None:
    assert ALLOWED_TRANSITIONS[state] == frozenset()
    assert is_terminal(state)


@pytest.mark.unit
@pytest.mark.parametrize("state", [s for s in PositionState if s not in TERMINAL_STATES])
def test_non_terminal_states_can_progress(state: PositionState) -> None:
    assert ALLOWED_TRANSITIONS[state], f"{state.value} is a dead end but is not terminal"
    assert not is_terminal(state)


@pytest.mark.unit
def test_every_state_is_reachable_from_discovered() -> None:
    """An unreachable state is dead code that will silently never occur."""
    seen = {PositionState.DISCOVERED}
    frontier = [PositionState.DISCOVERED]
    while frontier:
        current = frontier.pop()
        for nxt in ALLOWED_TRANSITIONS[current]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)

    assert seen == set(PositionState), f"unreachable: {set(PositionState) - seen}"


@pytest.mark.unit
def test_no_state_transitions_to_itself() -> None:
    for state, targets in ALLOWED_TRANSITIONS.items():
        assert state not in targets, f"{state.value} allows a self-transition"


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_a_new_machine_starts_as_discovered() -> None:
    machine = PositionStateMachine("position-001")
    assert machine.state is PositionState.DISCOVERED
    assert machine.history == ()
    assert not machine.is_terminal


@pytest.mark.unit
def test_full_happy_path() -> None:
    machine = PositionStateMachine("position-001")

    for target in HAPPY_PATH:
        machine.transition_to(target, NOW, reason="test")

    assert machine.state is PositionState.CLOSED
    assert machine.is_terminal
    assert len(machine.history) == len(HAPPY_PATH)


@pytest.mark.unit
def test_history_records_each_transition() -> None:
    machine = PositionStateMachine("position-001")
    transition = machine.transition_to(
        PositionState.RESEARCHED, NOW, reason="research complete", actor="market_researcher"
    )

    assert transition.from_state is PositionState.DISCOVERED
    assert transition.to_state is PositionState.RESEARCHED
    assert transition.reason == "research complete"
    assert transition.actor == "market_researcher"
    assert machine.history == (transition,)


@pytest.mark.unit
def test_history_is_an_immutable_view() -> None:
    machine = PositionStateMachine("position-001")
    machine.transition_to(PositionState.RESEARCHED, NOW)
    snapshot = machine.history

    machine.transition_to(PositionState.STRATEGY_SELECTED, NOW)

    assert len(snapshot) == 1, "a previously taken history view must not mutate"
    assert len(machine.history) == 2


@pytest.mark.unit
def test_no_trade_is_reachable_from_every_pre_execution_stage() -> None:
    """NO_TRADE is a first-class outcome, not an error path."""
    for state in (
        PositionState.DISCOVERED,
        PositionState.RESEARCHED,
        PositionState.STRATEGY_SELECTED,
        PositionState.CONTRACT_SELECTED,
        PositionState.ALLOCATED,
    ):
        assert can_transition(state, PositionState.NO_TRADE)


@pytest.mark.unit
def test_partial_fill_can_complete_or_be_cancelled() -> None:
    assert can_transition(PositionState.PARTIALLY_FILLED, PositionState.OPEN)
    assert can_transition(PositionState.PARTIALLY_FILLED, PositionState.CANCELLED)


# ---------------------------------------------------------------------------
# Rejected transitions
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        # Skipping risk approval entirely.
        (PositionState.ALLOCATED, PositionState.ORDER_SUBMITTED),
        # Opening a position without submitting an order.
        (PositionState.RISK_APPROVED, PositionState.OPEN),
        # Jumping straight to closed.
        (PositionState.RESEARCHED, PositionState.CLOSED),
        # Going backwards.
        (PositionState.OPEN, PositionState.RESEARCHED),
        (PositionState.CLOSING, PositionState.OPEN),
        # Research without discovery is not expressible here, but re-researching
        # an open position must not silently rewind the lifecycle.
        (PositionState.MONITORING, PositionState.ALLOCATED),
    ],
)
def test_illegal_transitions_are_rejected(
    from_state: PositionState, to_state: PositionState
) -> None:
    assert not can_transition(from_state, to_state)
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(from_state, to_state)


@pytest.mark.unit
@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_reject_all_transitions(state: PositionState) -> None:
    machine = PositionStateMachine("position-001", state=state)
    for target in PositionState:
        with pytest.raises(InvalidStateTransitionError):
            machine.transition_to(target, NOW)


@pytest.mark.unit
def test_rejected_transition_leaves_state_untouched() -> None:
    machine = PositionStateMachine("position-001")
    machine.transition_to(PositionState.RESEARCHED, NOW)

    with pytest.raises(InvalidStateTransitionError):
        machine.transition_to(PositionState.CLOSED, NOW)

    assert machine.state is PositionState.RESEARCHED
    assert len(machine.history) == 1, "a failed transition must not be recorded"


@pytest.mark.unit
def test_error_names_both_states_and_the_legal_alternatives() -> None:
    with pytest.raises(InvalidStateTransitionError) as excinfo:
        validate_transition(PositionState.OPEN, PositionState.RESEARCHED)

    error = excinfo.value
    assert error.from_state is PositionState.OPEN
    assert error.to_state is PositionState.RESEARCHED
    message = str(error)
    assert "OPEN" in message and "RESEARCHED" in message
    assert "MONITORING" in message, "the error should list what is allowed instead"


@pytest.mark.unit
def test_naive_timestamp_is_rejected() -> None:
    machine = PositionStateMachine("position-001")
    with pytest.raises(ValueError, match="timezone-aware"):
        machine.transition_to(PositionState.RESEARCHED, datetime(2026, 8, 10, 14, 30))
