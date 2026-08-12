"""The execution state machine (brief section 42.9).

The graph exists to make three mistakes impossible, and the tests are written
against those rather than against the edge list:

* a submission cannot decay back into "not sent yet", which would authorise a
  second order;
* ``UNKNOWN`` cannot resolve to anything meaning *send it again*;
* a refused transition changes nothing — not the state, and not the history,
  because an audit trail that records a transition that did not happen is worse
  than no audit trail.
"""

from __future__ import annotations

import pytest

from trading_system.domain.enums import (
    LIVE_EXECUTION_STATES,
    TERMINAL_EXECUTION_STATES,
    ExecutionState,
)
from trading_system.execution.state_machine import (
    ALLOWED_EXECUTION_TRANSITIONS,
    ExecutionStateMachine,
    InvalidExecutionTransitionError,
    can_transition,
    is_terminal,
    validate_transition,
)

from .conftest import NOW

pytestmark = pytest.mark.unit

_S = ExecutionState


# ---------------------------------------------------------------------------
# The happy paths the brief enumerates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (_S.CREATED, _S.VALIDATED),
        (_S.VALIDATED, _S.SUBMISSION_PENDING),
        (_S.SUBMISSION_PENDING, _S.SUBMITTED),
        (_S.SUBMITTED, _S.PARTIALLY_FILLED),
        (_S.PARTIALLY_FILLED, _S.FILLED),
        (_S.SUBMITTED, _S.CANCEL_PENDING),
        (_S.CANCEL_PENDING, _S.CANCELLED),
        (_S.SUBMISSION_PENDING, _S.REJECTED),
        (_S.SUBMISSION_PENDING, _S.UNKNOWN),
    ],
)
def test_the_documented_transitions_are_legal(from_state, to_state) -> None:
    assert can_transition(from_state, to_state)


def test_every_state_appears_in_the_graph() -> None:
    """A state missing from the graph would raise a KeyError at the worst moment."""
    assert set(ALLOWED_EXECUTION_TRANSITIONS) == set(ExecutionState)


# ---------------------------------------------------------------------------
# The transitions that must never exist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "state",
    [
        _S.SUBMISSION_PENDING,
        _S.SUBMITTED,
        _S.PARTIALLY_FILLED,
        _S.FILLED,
        _S.CANCEL_PENDING,
        _S.CANCELLED,
        _S.UNKNOWN,
    ],
)
@pytest.mark.parametrize("target", [_S.CREATED, _S.VALIDATED])
def test_nothing_returns_to_a_pre_submission_state(state, target) -> None:
    """The edge that would let "we sent this" become "we have not sent this".

    Any such path is a duplicate-order generator: the record would look ready
    to submit again while an order sat live at the broker.
    """
    assert not can_transition(state, target)


@pytest.mark.parametrize("target", [_S.SUBMISSION_PENDING, _S.CREATED, _S.VALIDATED, _S.FAILED])
def test_unknown_never_reaches_a_state_that_means_send_it_again(target) -> None:
    """``UNKNOWN`` is resolved by observation, never by retry.

    ``FAILED`` is included deliberately: it means *provably not sent*, and an
    uncertain submission can never become provably-not-sent by wishing.
    """
    assert not can_transition(_S.UNKNOWN, target)


def test_unknown_can_resolve_to_whatever_the_broker_turns_out_to_hold() -> None:
    for state in (_S.SUBMITTED, _S.PARTIALLY_FILLED, _S.FILLED, _S.CANCELLED, _S.REJECTED):
        assert can_transition(_S.UNKNOWN, state)


def test_a_partial_fill_can_never_become_unfilled() -> None:
    assert not can_transition(_S.PARTIALLY_FILLED, _S.SUBMITTED)
    assert not can_transition(_S.PARTIALLY_FILLED, _S.REJECTED)


def test_a_cancel_can_lose_the_race_with_a_fill() -> None:
    """CANCEL_PENDING is not terminal: the order can still fill."""
    assert can_transition(_S.CANCEL_PENDING, _S.FILLED)
    assert can_transition(_S.CANCEL_PENDING, _S.PARTIALLY_FILLED)
    assert not is_terminal(_S.CANCEL_PENDING)


def test_nothing_before_submission_can_reach_a_fill() -> None:
    """A fill comes from a fill report, and nothing else can produce one."""
    for state in (_S.CREATED, _S.VALIDATED):
        assert not can_transition(state, _S.FILLED)
        assert not can_transition(state, _S.PARTIALLY_FILLED)
        assert not can_transition(state, _S.SUBMITTED)


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", sorted(TERMINAL_EXECUTION_STATES))
def test_terminal_states_permit_nothing(state: ExecutionState) -> None:
    assert ALLOWED_EXECUTION_TRANSITIONS[state] == frozenset()
    assert is_terminal(state)


def test_unknown_is_deliberately_not_terminal() -> None:
    """An unresolvable question would leave capital committed to a phantom order."""
    assert not is_terminal(_S.UNKNOWN)
    assert ALLOWED_EXECUTION_TRANSITIONS[_S.UNKNOWN]


def test_live_states_are_exactly_those_where_an_order_may_exist() -> None:
    """The set idempotency is judged against.

    ``SUBMISSION_PENDING`` and ``UNKNOWN`` belong here precisely because they
    mean *we do not know*: absence of an acknowledgement is not absence of an
    order.
    """
    assert (
        frozenset(
            {
                _S.SUBMISSION_PENDING,
                _S.SUBMITTED,
                _S.PARTIALLY_FILLED,
                _S.FILLED,
                _S.CANCEL_PENDING,
                _S.UNKNOWN,
            }
        )
        == LIVE_EXECUTION_STATES
    )


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------
def test_a_transition_is_recorded_in_history() -> None:
    machine = ExecutionStateMachine("execution-1")

    transition = machine.transition_to(_S.VALIDATED, NOW, reason="preconditions passed")

    assert machine.state is _S.VALIDATED
    assert len(machine.history) == 1
    assert transition.from_state is _S.CREATED
    assert transition.to_state is _S.VALIDATED


def test_an_illegal_transition_leaves_state_and_history_untouched() -> None:
    """Brief section 42.9: a failed transition must change nothing.

    The history matters as much as the state — a rejected transition that still
    appended a line would leave an audit trail claiming something happened.
    """
    machine = ExecutionStateMachine("execution-1", _S.SUBMITTED)
    machine.transition_to(_S.PARTIALLY_FILLED, NOW)
    before_state, before_history = machine.state, machine.history

    with pytest.raises(InvalidExecutionTransitionError):
        machine.transition_to(_S.CREATED, NOW)

    assert machine.state is before_state
    assert machine.history == before_history


def test_a_terminal_machine_refuses_everything() -> None:
    machine = ExecutionStateMachine("execution-1", _S.FILLED)

    assert machine.is_terminal
    for state in ExecutionState:
        with pytest.raises(InvalidExecutionTransitionError):
            machine.transition_to(state, NOW)
    assert machine.history == ()


def test_the_error_names_what_was_allowed() -> None:
    with pytest.raises(InvalidExecutionTransitionError) as error:
        validate_transition(_S.CREATED, _S.FILLED)

    message = str(error.value)
    assert "CREATED -> FILLED" in message
    assert "VALIDATED" in message


def test_every_reachable_state_is_reachable_from_created() -> None:
    """No state is stranded: an unreachable state is a state nothing can record."""
    reachable = {_S.CREATED}
    frontier = [_S.CREATED]
    while frontier:
        for target in ALLOWED_EXECUTION_TRANSITIONS[frontier.pop()]:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)

    assert reachable == set(ExecutionState)
