"""Execution lifecycle state machine (brief section 11).

The same discipline as :mod:`trading_system.domain.state_machine`, applied to a
submission rather than to a position: transitions are explicit, anything not
listed is refused, and a refused transition leaves both the state and the
history untouched. There is no permissive fallback.

Three properties are what this module exists for, and each is a way a trading
system places an order it did not mean to place:

* **An acknowledgement is not a fill.** ``SUBMITTED`` can reach ``FILLED`` and
  ``PARTIALLY_FILLED``, but nothing *derives* those from the fact of having
  submitted — only an execution report does, in :mod:`.fill_tracker`.
* **Nothing returns to a pre-submission state.** ``SUBMISSION_PENDING`` and
  everything after it can never reach ``CREATED`` or ``VALIDATED`` again, so
  there is no edge along which "we already sent this" could decay into "we have
  not sent this yet" and authorise a second order.
* **``UNKNOWN`` is resolved by observation, never by retry.** It can move to
  any state the broker turns out to be in, and to no state that means *send it
  again*.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType

from pydantic import Field

from trading_system.domain.enums import (
    TERMINAL_EXECUTION_STATES,
    ExecutionReasonCode,
    ExecutionState,
)
from trading_system.domain.models import Identifier, ImmutableModel, UtcDatetime

__all__ = [
    "ALLOWED_EXECUTION_TRANSITIONS",
    "ExecutionStateMachine",
    "ExecutionTransition",
    "InvalidExecutionTransitionError",
    "can_transition",
    "is_terminal",
    "validate_transition",
]

_S = ExecutionState

#: The complete legal transition graph.
#:
#: Read it as three phases. Before submission, a request can only be refused
#: (``FAILED``/``REJECTED``) — never lost. Around submission, every outcome
#: including "we do not know" is expressible. After submission, only what the
#: broker reports can move the record, and the record can never travel back to
#: a state that would permit a second submission.
ALLOWED_EXECUTION_TRANSITIONS: Mapping[ExecutionState, frozenset[ExecutionState]] = (
    MappingProxyType(
        {
            # --- before anything is sent --------------------------------------
            _S.CREATED: frozenset({_S.VALIDATED, _S.REJECTED, _S.FAILED}),
            _S.VALIDATED: frozenset({_S.SUBMISSION_PENDING, _S.REJECTED, _S.FAILED}),
            # --- the submission itself ----------------------------------------
            #
            # FAILED is reachable only where the attempt provably did not leave
            # this process (a broker that refused to be constructed, a connection
            # that was never established). Anything that might have reached the
            # broker goes to UNKNOWN instead, which is the whole point of the
            # distinction.
            _S.SUBMISSION_PENDING: frozenset(
                {_S.SUBMITTED, _S.PARTIALLY_FILLED, _S.FILLED, _S.REJECTED, _S.UNKNOWN, _S.FAILED}
            ),
            # --- after the broker has it ---------------------------------------
            _S.SUBMITTED: frozenset(
                {
                    _S.PARTIALLY_FILLED,
                    _S.FILLED,
                    _S.CANCEL_PENDING,
                    _S.CANCELLED,
                    _S.REJECTED,
                    _S.EXPIRED,
                    _S.UNKNOWN,
                }
            ),
            # A partial fill either completes, or its remainder stops working and
            # leaves a real — smaller — structure behind. It can never become
            # "not filled".
            _S.PARTIALLY_FILLED: frozenset(
                {_S.FILLED, _S.CANCEL_PENDING, _S.CANCELLED, _S.EXPIRED, _S.UNKNOWN}
            ),
            # A cancel request can lose the race with a fill, so this is not a
            # terminal state and both outcomes stay expressible.
            _S.CANCEL_PENDING: frozenset(
                {_S.CANCELLED, _S.PARTIALLY_FILLED, _S.FILLED, _S.EXPIRED, _S.UNKNOWN}
            ),
            # Resolved only by observing the broker. Every edge here is something
            # the broker turned out to be; none of them is "try again".
            _S.UNKNOWN: frozenset(
                {
                    _S.SUBMITTED,
                    _S.PARTIALLY_FILLED,
                    _S.FILLED,
                    _S.CANCEL_PENDING,
                    _S.CANCELLED,
                    _S.REJECTED,
                    _S.EXPIRED,
                }
            ),
            # --- terminal -------------------------------------------------------
            _S.FILLED: frozenset(),
            _S.CANCELLED: frozenset(),
            _S.REJECTED: frozenset(),
            _S.EXPIRED: frozenset(),
            _S.FAILED: frozenset(),
        }
    )
)


class InvalidExecutionTransitionError(ValueError):
    """Raised when a transition is not permitted by the graph above."""

    def __init__(self, from_state: ExecutionState, to_state: ExecutionState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        allowed = sorted(s.value for s in ALLOWED_EXECUTION_TRANSITIONS[from_state])
        allowed_text = ", ".join(allowed) if allowed else "<terminal state>"
        super().__init__(
            f"illegal execution transition {from_state.value} -> {to_state.value}; "
            f"allowed: {allowed_text}"
        )


class ExecutionTransition(ImmutableModel):
    """A persisted record of one execution state change."""

    execution_id: Identifier
    from_state: ExecutionState
    to_state: ExecutionState
    occurred_at: UtcDatetime
    reason_code: ExecutionReasonCode | None = None
    reason: str | None = None
    actor: Identifier = Field(default="execution")


def is_terminal(state: ExecutionState) -> bool:
    """Whether ``state`` permits no further transition."""
    return state in TERMINAL_EXECUTION_STATES


def can_transition(from_state: ExecutionState, to_state: ExecutionState) -> bool:
    """Whether ``from_state -> to_state`` is legal."""
    return to_state in ALLOWED_EXECUTION_TRANSITIONS[from_state]


def validate_transition(from_state: ExecutionState, to_state: ExecutionState) -> None:
    """Raise :class:`InvalidExecutionTransitionError` unless the transition is legal."""
    if not can_transition(from_state, to_state):
        raise InvalidExecutionTransitionError(from_state, to_state)


class ExecutionStateMachine:
    """Tracks one execution's state and how it got there.

    The history is the artifact worth keeping: it answers "when did we believe
    this order existed, and what did the broker say next", which is the
    question a duplicate order or an unexplained position turns into.
    """

    def __init__(
        self,
        execution_id: str,
        state: ExecutionState = ExecutionState.CREATED,
        history: Sequence[ExecutionTransition] | None = None,
    ) -> None:
        self._execution_id = execution_id
        self._state = state
        self._history: list[ExecutionTransition] = list(history or [])

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def state(self) -> ExecutionState:
        return self._state

    @property
    def history(self) -> tuple[ExecutionTransition, ...]:
        return tuple(self._history)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self._state)

    def can_transition_to(self, to_state: ExecutionState) -> bool:
        return can_transition(self._state, to_state)

    def transition_to(
        self,
        to_state: ExecutionState,
        occurred_at: datetime,
        *,
        reason_code: ExecutionReasonCode | None = None,
        reason: str | None = None,
        actor: str = "execution",
    ) -> ExecutionTransition:
        """Apply a transition, recording it.

        Raises :class:`InvalidExecutionTransitionError` and leaves the machine
        **and its history** untouched when the transition is not permitted. The
        history matters as much as the state: a rejected transition that still
        appended a line would leave an audit trail claiming something happened
        that did not.
        """
        validate_transition(self._state, to_state)
        transition = ExecutionTransition(
            execution_id=self._execution_id,
            from_state=self._state,
            to_state=to_state,
            occurred_at=occurred_at,
            reason_code=reason_code,
            reason=reason,
            actor=actor,
        )
        self._state = to_state
        self._history.append(transition)
        return transition

    def __repr__(self) -> str:
        return (
            f"ExecutionStateMachine(execution_id={self._execution_id!r}, "
            f"state={self._state.value!r}, transitions={len(self._history)})"
        )
