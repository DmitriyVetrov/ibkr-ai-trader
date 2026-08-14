"""The trailing stop: a state machine, not a mutable price.

Pure functions over a stored :class:`~trading_system.exit.models.TrailingStopRecord`
and one market observation. Nothing here reads a clock, opens a connection or
writes a file, which is what lets a trailing level be *reproduced* from stored
artifacts long after the exit it caused.

.. code-block:: text

    INACTIVE   below the activation threshold; no level exists
       |  gain over entry cost reaches activation_return_pct
    ARMED      first level set, from the observation that armed it
       |  carried across evaluations
    ACTIVE     level ratchets upward with the peak, never downward
       |  observed price <= level
    TRIGGERED  terminal; the crossing observation is recorded

The invariant everything else rests on is **monotonicity**:

.. code-block:: text

    favourable price rises   ->  peak rises, level may rise
    favourable price falls   ->  peak unchanged, level UNCHANGED

A level that followed the position down would not be a stop. It would
guarantee the position is never sold, however much of its peak it had given
back, and it would fail *silently* — there is no error, no log line, just a
position that never exits. ``exit.trailing.allow_level_to_fall: true`` fails to
load precisely so that this cannot be configured, and
:func:`observe` enforces it again on every observation.

**Units.** Everything here is in the broker's *quoted* terms — 6.05, not
605.00. That is what a market observation and a limit price are in, and doing
the multiplier conversion on every comparison would be a factor of 100 waiting
to be forgotten. Percentages are unaffected: both sides of every ratio carry
the same multiplier.

**Restart safety.** The record carries everything needed to continue: the peak,
the level, the entry reference, when each moved and what moved it. Reloading it
and replaying the same observation produces the same state and the same
decision, which ``tests/exit/test_trailing.py`` asserts by round-tripping
through the repository rather than by copying the object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    ExitDecisionType,
    ExitPolicyKind,
    ExitQuoteField,
    ExitReasonCode,
    PositionLifecycleEventType,
    TrailingStopState,
)
from trading_system.exit.models import (
    ExitPolicyOutcome,
    TrailingStopRecord,
    trailing_state_identifier,
)
from trading_system.infrastructure.settings import ExitTrailingConfig

__all__ = [
    "TrailingObservation",
    "activation_quote",
    "evaluate_trailing",
    "level_for",
    "new_trailing_record",
    "observe",
]


def _pct(value: float) -> Decimal:
    """A configured percentage as an exact decimal.

    Via ``str`` deliberately: ``Decimal(0.3)`` is 0.299999999999999988897769…,
    and a trailing distance that is not the number in the configuration file is
    a trailing distance nobody can reconcile against the file.
    """
    return Decimal(str(value))


def activation_quote(entry_quote: Decimal, activation_return_pct: float) -> Decimal:
    """The quoted price at which the trail arms.

    The entry cost plus the configured gain. Exact: an activation threshold
    computed in binary floating point would arm one tick early or late in a way
    that is invisible in the record.
    """
    return entry_quote * (Decimal(1) + _pct(activation_return_pct) / Decimal(100))


def level_for(peak_quote: Decimal, distance_pct: float) -> Decimal:
    """The level that sits ``distance_pct`` below a peak.

    Never below zero: a negative level would be a stop that can never be hit,
    which is the same failure as no stop at all.
    """
    level = peak_quote * (Decimal(1) - _pct(distance_pct) / Decimal(100))
    return level if level > 0 else Decimal("0")


def new_trailing_record(
    *,
    position_id: str,
    entry_quote: Decimal | None,
    config: ExitTrailingConfig,
    quote_field: ExitQuoteField,
    distance_pct: float,
    activation_return_pct: float,
    created_at: datetime,
) -> TrailingStopRecord:
    """The state a position starts with: inactive, with no level.

    ``distance_pct`` and ``activation_return_pct`` are the *effective* values —
    the strategy's where it narrowed the global ones — and are passed in rather
    than read from ``config``, so this function has no opinion about layering.
    Configuration loading already refused any strategy that widened them.
    """
    return TrailingStopRecord(
        trailing_state_id=trailing_state_identifier(position_id=position_id),
        position_id=position_id,
        state=TrailingStopState.INACTIVE,
        quote_field=quote_field,
        activation_return_pct=activation_return_pct,
        distance_pct=distance_pct,
        min_improvement_pct=config.min_improvement_pct,
        entry_quote=entry_quote,
        created_at=created_at,
        updated_at=created_at,
        detail="no observation yet; the trail arms when the position gains enough",
    )


@dataclass(frozen=True, slots=True)
class TrailingObservation:
    """What one observation did to the trail.

    ``event_type`` is ``None`` when nothing moved — the ordinary case, and one
    worth distinguishing from a level change, because a history that recorded
    an event per tick would bury the three that matter.
    """

    record: TrailingStopRecord
    changed: bool = False
    event_type: PositionLifecycleEventType | None = None
    detail: str = ""

    @property
    def triggered(self) -> bool:
        return self.record.state is TrailingStopState.TRIGGERED


def observe(
    record: TrailingStopRecord,
    *,
    observed_quote: Decimal,
    at: datetime,
) -> TrailingObservation:
    """Apply one market observation to the trail, returning a new record.

    The order of the branches is the whole algorithm and is worth reading in
    order:

    1. A ``TRIGGERED`` trail is terminal and absorbs further observations
       without changing. A stop that re-armed after firing would sell a
       position twice.
    2. Below the activation threshold, nothing happens and no level exists.
    3. At or above it for the first time, the trail arms from *this*
       observation — not from a level back-computed from a peak we never saw.
    4. A new high raises the peak, and raises the level with it if the
       improvement clears ``min_improvement_pct``.
    5. At or below the level, it triggers, and the crossing observation is
       recorded because it is the explanation.

    A falling price that has not reached the level falls through every branch
    and returns the record unchanged. That is the monotonicity guarantee, and
    it is structural: there is no branch that lowers a peak or a level.
    """
    if record.state is TrailingStopState.TRIGGERED:
        return TrailingObservation(
            record=record,
            detail="the trailing stop has already triggered; it is terminal",
        )

    entry = record.entry_quote
    if entry is None or entry <= 0:
        return TrailingObservation(
            record=record,
            detail=(
                "no entry reference in quoted terms, so no activation threshold can be "
                "computed. The trail stays inactive rather than arming from an assumed cost"
            ),
        )

    threshold = activation_quote(entry, record.activation_return_pct)

    # --- not yet armed -----------------------------------------------------
    if record.state is TrailingStopState.INACTIVE:
        if observed_quote < threshold:
            # Returned *unchanged*, deliberately. An observation that moved
            # nothing is not a change to the trail, and stamping a clock and a
            # counter on it would make two evaluations of identical state
            # produce different artifacts — which is exactly what the
            # idempotency guarantee says they must not do. That we looked is
            # recorded in the lifecycle history, where it belongs.
            return TrailingObservation(record=record, detail="below the activation threshold")
        level = level_for(observed_quote, record.distance_pct)
        return TrailingObservation(
            record=record.model_copy(
                update={
                    "state": TrailingStopState.ARMED,
                    "peak_quote": observed_quote,
                    "stop_quote": level,
                    "activation_quote": observed_quote,
                    "activated_at": at,
                    "peak_at": at,
                    "level_updated_at": at,
                    "updated_at": at,
                    "observations": record.observations + 1,
                    "detail": (
                        f"armed at {observed_quote} (threshold {threshold}); level set "
                        f"{record.distance_pct}% below at {level}"
                    ),
                }
            ),
            changed=True,
            event_type=PositionLifecycleEventType.TRAILING_ARMED,
            detail=f"trailing armed at {observed_quote}, level {level}",
        )

    # --- armed or active ---------------------------------------------------
    peak = record.peak_quote
    stop = record.stop_quote
    assert peak is not None and stop is not None  # the model validator guarantees both
    level = stop

    if observed_quote <= level:
        return TrailingObservation(
            record=record.model_copy(
                update={
                    "state": TrailingStopState.TRIGGERED,
                    "trigger_quote": observed_quote,
                    "triggered_at": at,
                    "updated_at": at,
                    "observations": record.observations + 1,
                    "detail": (
                        f"observed {observed_quote} at or below the trailing level {level}, "
                        f"which was set {record.distance_pct}% below a peak of {peak}"
                    ),
                }
            ),
            changed=True,
            event_type=PositionLifecycleEventType.TRAILING_TRIGGERED,
            detail=f"trailing stop triggered at {observed_quote} against level {level}",
        )

    if observed_quote > peak:
        improvement = (observed_quote - peak) / peak * Decimal(100)
        if improvement < _pct(record.min_improvement_pct):
            # A new high, but not one worth rewriting the level for. The peak
            # still moves — otherwise a long series of tiny highs would never
            # raise the level at all — but the level does not, so the history
            # is not filled with events that changed nothing material.
            return TrailingObservation(
                record=record.model_copy(
                    update={
                        "state": TrailingStopState.ACTIVE,
                        "peak_quote": observed_quote,
                        "peak_at": at,
                        "updated_at": at,
                        "observations": record.observations + 1,
                        "detail": (
                            f"new peak {observed_quote}, improvement {improvement:.4f}% below "
                            f"the {record.min_improvement_pct}% threshold; level held at {level}"
                        ),
                    }
                ),
                changed=True,
                detail="peak raised, level held",
            )
        raised = level_for(observed_quote, record.distance_pct)
        # Belt and braces. ``level_for`` is monotone in its argument and the
        # peak only rises, so this can only fire if the distance changed under
        # a running position — a configuration edit mid-life. Holding the
        # higher level is the safe answer; lowering it would give back more
        # than the position was ever managed for.
        new_level = raised if raised > level else level
        return TrailingObservation(
            record=record.model_copy(
                update={
                    "state": TrailingStopState.ACTIVE,
                    "peak_quote": observed_quote,
                    "stop_quote": new_level,
                    "peak_at": at,
                    "level_updated_at": at if new_level != level else record.level_updated_at,
                    "updated_at": at,
                    "observations": record.observations + 1,
                    "detail": (
                        f"new peak {observed_quote}; level raised from {level} to {new_level}"
                    ),
                }
            ),
            changed=True,
            event_type=PositionLifecycleEventType.TRAILING_LEVEL_RAISED,
            detail=f"peak {observed_quote}, level {new_level}",
        )

    # Between the level and the peak: the position has given something back but
    # not enough. Nothing moves, and that is the invariant — so nothing on the
    # record moves either, for the same idempotency reason as above. The one
    # exception is ``ARMED -> ACTIVE``, which is a real state change: the trail
    # has now been carried across an evaluation rather than only just set.
    if record.state is TrailingStopState.ARMED:
        return TrailingObservation(
            record=record.model_copy(
                update={
                    "state": TrailingStopState.ACTIVE,
                    "updated_at": at,
                    "observations": record.observations + 1,
                    "detail": (
                        f"observed {observed_quote} between the level {level} and the peak "
                        f"{peak}; neither moves — a trailing level never follows a position down"
                    ),
                }
            ),
            changed=True,
            detail="holding",
        )
    return TrailingObservation(record=record, detail="holding")


def evaluate_trailing(
    record: TrailingStopRecord, *, observed_quote: Decimal | None, enabled: bool
) -> ExitPolicyOutcome:
    """Turn the trail's state into a verdict.

    Separate from :func:`observe` on purpose: observing mutates the trail and
    deciding reads it, and a function that did both would make "what would this
    have decided" impossible to ask without changing the answer.
    """
    if not enabled:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.TRAILING_STOP,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            summary="the trailing stop is switched off in configuration",
            evaluated=False,
        )
    if observed_quote is None:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.TRAILING_STOP,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            summary="no usable exit price, so the trailing stop was not evaluated",
            detail=(
                "recorded as unevaluated rather than as passed, for the same reason an "
                "untested risk limit is NOT_EVALUATED rather than PASS. The data-quality "
                "policy decides what an unusable price means"
            ),
            evaluated=False,
        )

    if record.state is TrailingStopState.TRIGGERED:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.TRAILING_STOP,
            decision=ExitDecisionType.EXIT,
            reason_code=ExitReasonCode.TRAILING_STOP_TRIGGERED,
            measured=str(record.trigger_quote if record.trigger_quote is not None else ""),
            threshold=str(record.stop_quote if record.stop_quote is not None else ""),
            summary=(
                f"trailing stop triggered: {record.trigger_quote} at or below the level "
                f"{record.stop_quote}"
            ),
            detail=record.detail,
        )

    if record.state is TrailingStopState.INACTIVE:
        threshold = (
            activation_quote(record.entry_quote, record.activation_return_pct)
            if record.entry_quote
            else None
        )
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.TRAILING_STOP,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.TRAILING_NOT_ACTIVE,
            measured=str(observed_quote),
            threshold=str(threshold) if threshold is not None else None,
            summary=(
                f"the trailing stop is not armed: {observed_quote} is below the activation "
                f"threshold {threshold}"
            ),
            detail=record.detail,
        )

    return ExitPolicyOutcome(
        policy=ExitPolicyKind.TRAILING_STOP,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.TRAILING_ABOVE_STOP,
        measured=str(observed_quote),
        threshold=str(record.stop_quote),
        summary=(
            f"trailing stop {record.state.value.lower()}: {observed_quote} is above the level "
            f"{record.stop_quote} (peak {record.peak_quote})"
        ),
        detail=record.detail,
    )
