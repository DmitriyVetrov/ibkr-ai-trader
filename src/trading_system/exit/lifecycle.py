"""Position lifecycle state machine (Milestone 10).

The same discipline as :mod:`trading_system.domain.state_machine` and
:mod:`trading_system.execution.state_machine`, applied to a position that
already exists: transitions are explicit, anything not listed is refused, and a
refused transition leaves both the state and the history untouched. There is no
permissive fallback.

Four properties are what this module exists for, and each of them is a way a
trading system closes a position it did not mean to close, or believes it
closed one it did not:

* **``CLOSED`` is terminal.** Nothing leaves it. A position the broker says is
  gone cannot become open again — if the broker later reports contracts under
  that contract id, that is a *new* position with its own history, or a
  reconciliation finding, and either is better than a record that silently
  reopened.
* **``EXIT_UNKNOWN`` never returns to ``EXIT_SUBMITTED``.** An exit whose
  outcome was never learned may be a live order right now. The graph has no
  edge from it to any state that would permit sending another one; it is left
  by *observing* the broker, which resolves it to ``CLOSED`` (the exit filled)
  or to ``BLOCKED`` (it did not, and a person must look).
* **``BLOCKED`` is a current verdict, not a memory.** The block is re-derived
  from the conditions on every evaluation, so the state is left when a later
  evaluation finds those conditions gone — which is resolution by observation,
  the same discipline every other unknown in this system is resolved by. It is
  deliberately *not* sticky: a position blocked because a research file could
  not be read must still be force-exited at its expiration deadline, and a
  sticky block would let an unrelated missing file disable the most important
  policy in the milestone. What is never retried is a *submission* whose
  outcome is unknown, and ``EXIT_UNKNOWN`` is what expresses that.
* **Nothing returns to ``OPEN``.** ``OPEN`` means "exit management has not
  looked at this yet", and it is true exactly once.

``EXIT_REQUIRED`` is a real state rather than a moment inside a function call:
a decision to exit that was never acted on — because execution was disabled, or
the run was a dry run, or nobody confirmed — is a fact worth keeping, and the
next run must see it rather than rediscover it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType

from pydantic import Field

from trading_system.domain.enums import (
    TERMINAL_LIFECYCLE_STATES,
    ExitReasonCode,
    PositionLifecycleState,
)
from trading_system.domain.models import Identifier, ImmutableModel, UtcDatetime

__all__ = [
    "ALLOWED_LIFECYCLE_TRANSITIONS",
    "InvalidLifecycleTransitionError",
    "LifecycleStateMachine",
    "LifecycleTransition",
    "can_transition",
    "is_terminal",
    "validate_lifecycle_transition",
]

_S = PositionLifecycleState

#: The complete legal transition graph.
#:
#: Read it as four phases. *Observing* — a position is picked up and monitored,
#: and a trailing stop may arm. *Deciding* — a policy triggers and the position
#: is required to exit. *Sending* — an exit reaches, or may have reached, the
#: broker. *Settling* — broker reality confirms closure, or it does not and a
#: person is asked to look.
#:
#: Every state can reach ``BLOCKED``, because every state can turn out to rest
#: on data that cannot be trusted — except ``CLOSED``, which is terminal, and
#: whose absence from that list is the point.
_Graph = Mapping[PositionLifecycleState, frozenset[PositionLifecycleState]]

ALLOWED_LIFECYCLE_TRANSITIONS: _Graph = MappingProxyType(
    {
        # --- observing -------------------------------------------------
        # A position that filled and has never been evaluated. It can be
        # closed directly: the broker may report it gone before this system
        # ever looked, and refusing to record that would leave a position
        # open forever because nobody watched it in time.
        _S.OPEN: frozenset(
            {_S.MONITORING, _S.TRAILING_ACTIVE, _S.EXIT_REQUIRED, _S.CLOSED, _S.BLOCKED}
        ),
        _S.MONITORING: frozenset({_S.TRAILING_ACTIVE, _S.EXIT_REQUIRED, _S.CLOSED, _S.BLOCKED}),
        # A trail can deactivate only by exiting or by the position
        # disappearing. It deliberately cannot fall back to MONITORING: the
        # peak and the level are carried across evaluations, and a state
        # that forgot them would restart the trail from the current price.
        _S.TRAILING_ACTIVE: frozenset({_S.EXIT_REQUIRED, _S.CLOSED, _S.BLOCKED}),
        # --- deciding --------------------------------------------------
        # A required exit that was not acted on stays required. It can also
        # turn out to be unnecessary — the broker reports the position gone
        # — and it can block if the data it rested on stops being usable.
        # EXIT_UNKNOWN is reachable directly: a submission that timed out never
        # reached EXIT_SUBMITTED, because nothing acknowledged it. Refusing
        # that edge would leave the position recorded as merely *requiring* an
        # exit while an order for it may be live at the broker — which is the
        # one state this whole graph exists to keep distinguishable.
        _S.EXIT_REQUIRED: frozenset({_S.EXIT_SUBMITTED, _S.EXIT_UNKNOWN, _S.CLOSED, _S.BLOCKED}),
        # --- sending ---------------------------------------------------
        # Only what the broker reports can move this. There is no edge back
        # to EXIT_REQUIRED: that would permit a second submission.
        _S.EXIT_SUBMITTED: frozenset({_S.CLOSED, _S.EXIT_UNKNOWN, _S.BLOCKED}),
        # Resolved by observation. CLOSED when the broker turns out to hold
        # nothing, BLOCKED when it still does and a person must decide.
        # Deliberately *not* EXIT_SUBMITTED or EXIT_REQUIRED — neither
        # would be a fact we had established, and both lead to a second
        # order for a position that may already have been sold.
        _S.EXIT_UNKNOWN: frozenset({_S.CLOSED, _S.BLOCKED}),
        # --- settling --------------------------------------------------
        # Left when a later evaluation, judging the position afresh, finds no
        # blocking condition. It can go straight to EXIT_REQUIRED: a block is
        # re-derived from current conditions rather than remembered, and a
        # position blocked last run because a research file was unreadable must
        # still be force-exited at its expiration deadline. It deliberately
        # cannot reach EXIT_SUBMITTED directly — sending is a separate act with
        # its own authorisation.
        _S.BLOCKED: frozenset({_S.MONITORING, _S.TRAILING_ACTIVE, _S.EXIT_REQUIRED, _S.CLOSED}),
        _S.CLOSED: frozenset(),
    }
)


class InvalidLifecycleTransitionError(ValueError):
    """Raised when a transition is not permitted by the graph above."""

    def __init__(
        self, from_state: PositionLifecycleState, to_state: PositionLifecycleState
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        allowed = sorted(s.value for s in ALLOWED_LIFECYCLE_TRANSITIONS[from_state])
        allowed_text = ", ".join(allowed) if allowed else "<terminal state>"
        super().__init__(
            f"illegal position lifecycle transition {from_state.value} -> {to_state.value}; "
            f"allowed: {allowed_text}"
        )


class LifecycleTransition(ImmutableModel):
    """A persisted record of one lifecycle state change."""

    position_id: Identifier
    from_state: PositionLifecycleState
    to_state: PositionLifecycleState
    occurred_at: UtcDatetime
    reason_code: ExitReasonCode | None = None
    reason: str | None = None
    actor: Identifier = Field(default="exit")


def is_terminal(state: PositionLifecycleState) -> bool:
    """Whether ``state`` permits no further transition."""
    return state in TERMINAL_LIFECYCLE_STATES


def can_transition(from_state: PositionLifecycleState, to_state: PositionLifecycleState) -> bool:
    """Whether ``from_state -> to_state`` is legal."""
    return to_state in ALLOWED_LIFECYCLE_TRANSITIONS[from_state]


def validate_lifecycle_transition(
    from_state: PositionLifecycleState, to_state: PositionLifecycleState
) -> None:
    """Raise :class:`InvalidLifecycleTransitionError` unless the transition is legal."""
    if not can_transition(from_state, to_state):
        raise InvalidLifecycleTransitionError(from_state, to_state)


class LifecycleStateMachine:
    """Tracks one position's lifecycle state and how it got there.

    The history is the artifact worth keeping: it answers "when did this
    position start trailing, when was an exit required, and what did the broker
    say next" — which is the question an unexplained position, or an exit that
    never filled, turns into.
    """

    def __init__(
        self,
        position_id: str,
        state: PositionLifecycleState = PositionLifecycleState.OPEN,
        history: Sequence[LifecycleTransition] | None = None,
    ) -> None:
        self._position_id = position_id
        self._state = state
        self._history: list[LifecycleTransition] = list(history or [])

    @property
    def position_id(self) -> str:
        return self._position_id

    @property
    def state(self) -> PositionLifecycleState:
        return self._state

    @property
    def history(self) -> tuple[LifecycleTransition, ...]:
        return tuple(self._history)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self._state)

    def can_transition_to(self, to_state: PositionLifecycleState) -> bool:
        return can_transition(self._state, to_state)

    def transition_to(
        self,
        to_state: PositionLifecycleState,
        occurred_at: datetime,
        *,
        reason_code: ExitReasonCode | None = None,
        reason: str | None = None,
        actor: str = "exit",
    ) -> LifecycleTransition:
        """Apply a transition, recording it.

        Raises :class:`InvalidLifecycleTransitionError` and leaves the machine
        **and its history** untouched when the transition is not permitted. The
        history matters as much as the state: a rejected transition that still
        appended a line would leave an audit trail claiming a position moved
        when it did not.
        """
        validate_lifecycle_transition(self._state, to_state)
        transition = LifecycleTransition(
            position_id=self._position_id,
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
            f"LifecycleStateMachine(position_id={self._position_id!r}, "
            f"state={self._state.value!r}, transitions={len(self._history)})"
        )
