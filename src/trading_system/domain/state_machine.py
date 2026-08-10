"""Position lifecycle state machine (specification section 16).

Transitions are deterministic, explicit and persisted. Anything not listed in
:data:`ALLOWED_TRANSITIONS` is rejected — there is no permissive fallback, and
no state may be left by an unlisted edge.

The machine is pure: it records what happened, it does not decide what should
happen. Exit policy, risk and reconciliation live in their own modules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType

from pydantic import Field

from trading_system.domain.enums import TERMINAL_STATES, PositionState
from trading_system.domain.models import Identifier, ImmutableModel, UtcDatetime

__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidStateTransitionError",
    "PositionStateMachine",
    "StateTransition",
    "can_transition",
    "is_terminal",
    "validate_transition",
]

_S = PositionState

#: The complete legal transition graph.
#:
#: Every non-terminal state can fail (``FAILED``); every pre-execution stage can
#: end in ``NO_TRADE`` or ``REJECTED``, because a no-trade outcome is a
#: first-class result at each decision point (specification section 1.5).
ALLOWED_TRANSITIONS: Mapping[PositionState, frozenset[PositionState]] = MappingProxyType(
    {
        _S.DISCOVERED: frozenset({_S.RESEARCHED, _S.NO_TRADE, _S.REJECTED, _S.FAILED}),
        _S.RESEARCHED: frozenset({_S.STRATEGY_SELECTED, _S.NO_TRADE, _S.REJECTED, _S.FAILED}),
        _S.STRATEGY_SELECTED: frozenset(
            {_S.CONTRACT_SELECTED, _S.NO_TRADE, _S.REJECTED, _S.FAILED}
        ),
        _S.CONTRACT_SELECTED: frozenset({_S.ALLOCATED, _S.NO_TRADE, _S.REJECTED, _S.FAILED}),
        _S.ALLOCATED: frozenset({_S.RISK_APPROVED, _S.NO_TRADE, _S.REJECTED, _S.FAILED}),
        # Risk approval may still be abandoned before submission (e.g. stale
        # market data at execution time) but can no longer be "NO_TRADE"d by an
        # agent — only cancelled or failed.
        _S.RISK_APPROVED: frozenset({_S.ORDER_SUBMITTED, _S.CANCELLED, _S.FAILED}),
        _S.ORDER_SUBMITTED: frozenset(
            {_S.PARTIALLY_FILLED, _S.OPEN, _S.CANCELLED, _S.REJECTED, _S.FAILED}
        ),
        # A partially filled order either completes, or its remainder is
        # cancelled leaving a real (smaller) position to manage.
        _S.PARTIALLY_FILLED: frozenset({_S.OPEN, _S.CANCELLED, _S.FAILED}),
        _S.OPEN: frozenset({_S.MONITORING, _S.EXIT_TRIGGERED, _S.EXPIRED, _S.FAILED}),
        _S.MONITORING: frozenset({_S.EXIT_TRIGGERED, _S.EXPIRED, _S.FAILED}),
        _S.EXIT_TRIGGERED: frozenset({_S.CLOSING, _S.FAILED}),
        _S.CLOSING: frozenset({_S.CLOSED, _S.FAILED}),
        # Terminal states.
        _S.CLOSED: frozenset(),
        _S.NO_TRADE: frozenset(),
        _S.REJECTED: frozenset(),
        _S.CANCELLED: frozenset(),
        _S.FAILED: frozenset(),
        _S.EXPIRED: frozenset(),
    }
)


class InvalidStateTransitionError(ValueError):
    """Raised when a transition is not permitted by :data:`ALLOWED_TRANSITIONS`."""

    def __init__(self, from_state: PositionState, to_state: PositionState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[from_state])
        allowed_text = ", ".join(allowed) if allowed else "<terminal state>"
        super().__init__(
            f"illegal transition {from_state.value} -> {to_state.value}; allowed: {allowed_text}"
        )


class StateTransition(ImmutableModel):
    """A persisted record of one state change."""

    position_id: Identifier
    from_state: PositionState
    to_state: PositionState
    occurred_at: UtcDatetime
    reason: str | None = None
    actor: Identifier = Field(default="system")


def is_terminal(state: PositionState) -> bool:
    """Return whether ``state`` permits no further transition."""
    return state in TERMINAL_STATES


def can_transition(from_state: PositionState, to_state: PositionState) -> bool:
    """Return whether ``from_state -> to_state`` is legal."""
    return to_state in ALLOWED_TRANSITIONS[from_state]


def validate_transition(from_state: PositionState, to_state: PositionState) -> None:
    """Raise :class:`InvalidStateTransitionError` unless the transition is legal."""
    if not can_transition(from_state, to_state):
        raise InvalidStateTransitionError(from_state, to_state)


class PositionStateMachine:
    """Tracks the state of one position and the history of how it got there.

    The history is the persistable artifact: it answers "when did this become
    a real position, and why did it stop being one".
    """

    def __init__(
        self,
        position_id: str,
        state: PositionState = PositionState.DISCOVERED,
        history: Sequence[StateTransition] | None = None,
    ) -> None:
        self._position_id = position_id
        self._state = state
        self._history: list[StateTransition] = list(history or [])

    @property
    def position_id(self) -> str:
        return self._position_id

    @property
    def state(self) -> PositionState:
        return self._state

    @property
    def history(self) -> tuple[StateTransition, ...]:
        """Immutable view of the transitions applied so far."""
        return tuple(self._history)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self._state)

    def can_transition_to(self, to_state: PositionState) -> bool:
        return can_transition(self._state, to_state)

    def transition_to(
        self,
        to_state: PositionState,
        occurred_at: datetime,
        reason: str | None = None,
        actor: str = "system",
    ) -> StateTransition:
        """Apply a transition, recording it in history.

        Raises :class:`InvalidStateTransitionError` and leaves the machine
        untouched if the transition is not permitted. ``occurred_at`` must be
        timezone-aware; the model rejects naive datetimes.
        """
        validate_transition(self._state, to_state)
        transition = StateTransition(
            position_id=self._position_id,
            from_state=self._state,
            to_state=to_state,
            occurred_at=occurred_at,
            reason=reason,
            actor=actor,
        )
        self._state = to_state
        self._history.append(transition)
        return transition

    def __repr__(self) -> str:
        return (
            f"PositionStateMachine(position_id={self._position_id!r}, "
            f"state={self._state.value!r}, transitions={len(self._history)})"
        )
