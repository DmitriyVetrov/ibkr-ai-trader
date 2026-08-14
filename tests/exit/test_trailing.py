"""The trailing-stop state machine, and the invariant that makes it a stop.

The monotonicity tests are the heart of this file. A trailing level that
follows a position down is not a trailing stop: it guarantees the position is
never sold, however much of its peak it has given back, and it fails *silently*
— no error, no log line, just a position that never exits.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.domain.enums import (
    ExitDecisionType,
    ExitQuoteField,
    ExitReasonCode,
    PositionLifecycleEventType,
    TrailingStopState,
)
from trading_system.exit.trailing import (
    activation_quote,
    evaluate_trailing,
    level_for,
    new_trailing_record,
    observe,
)
from trading_system.infrastructure.settings import SystemConfig

pytestmark = pytest.mark.unit


def _record(config: SystemConfig, **overrides: object):
    defaults: dict[str, object] = {
        "position_id": "strategypos-1",
        "entry_quote": Decimal("6.00"),
        "config": config.exit.trailing,
        "quote_field": ExitQuoteField.BID,
        "distance_pct": 30.0,
        "activation_return_pct": 25.0,
        "created_at": NOW,
    }
    defaults.update(overrides)
    return new_trailing_record(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Arithmetic, exactly
# ---------------------------------------------------------------------------
def test_the_activation_threshold_is_exact() -> None:
    """Computed through ``str``: ``Decimal(0.25)`` is not 0.25."""
    assert activation_quote(Decimal("6.00"), 25.0) == Decimal("7.50")
    assert activation_quote(Decimal("6.00"), 0.0) == Decimal("6.00")


def test_the_level_sits_exactly_the_configured_distance_below_the_peak() -> None:
    assert level_for(Decimal("10.00"), 30.0) == Decimal("7.00")
    assert level_for(Decimal("10.00"), 100.0) == Decimal("0.00")


def test_a_level_is_never_negative() -> None:
    """A negative level is a stop that can never be hit — the same as none."""
    assert level_for(Decimal("1.00"), 150.0) == Decimal("0")


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------
def test_a_trail_does_not_arm_below_the_activation_threshold(system_config: SystemConfig) -> None:
    outcome = observe(_record(system_config), observed_quote=Decimal("7.00"), at=NOW)

    assert outcome.record.state is TrailingStopState.INACTIVE
    assert outcome.record.stop_quote is None
    assert not outcome.changed


def test_a_trail_arms_from_the_observation_that_reached_the_threshold(
    system_config: SystemConfig,
) -> None:
    """The level is set from *this* observation, not back-computed from a peak
    the system never saw."""
    outcome = observe(_record(system_config), observed_quote=Decimal("8.00"), at=NOW)

    assert outcome.record.state is TrailingStopState.ARMED
    assert outcome.record.peak_quote == Decimal("8.00")
    assert outcome.record.activation_quote == Decimal("8.00")
    assert outcome.record.stop_quote == Decimal("5.60")
    assert outcome.event_type is PositionLifecycleEventType.TRAILING_ARMED


def test_a_trail_with_no_entry_reference_never_arms(system_config: SystemConfig) -> None:
    """Arming from an assumed cost would invent the threshold."""
    outcome = observe(
        _record(system_config, entry_quote=None), observed_quote=Decimal("99.00"), at=NOW
    )

    assert outcome.record.state is TrailingStopState.INACTIVE
    assert "no entry reference" in outcome.detail


# ---------------------------------------------------------------------------
# Monotonicity — the invariant this module exists for
# ---------------------------------------------------------------------------
def test_a_rising_price_raises_the_peak_and_the_level(system_config: SystemConfig) -> None:
    armed = observe(_record(system_config), observed_quote=Decimal("8.00"), at=NOW).record

    raised = observe(armed, observed_quote=Decimal("10.00"), at=NOW).record

    assert raised.peak_quote == Decimal("10.00")
    assert raised.stop_quote == Decimal("7.00")
    assert raised.state is TrailingStopState.ACTIVE


def test_a_falling_price_moves_neither_the_peak_nor_the_level(
    system_config: SystemConfig,
) -> None:
    """The whole point. A stop that followed the position down would never fire."""
    armed = observe(_record(system_config), observed_quote=Decimal("10.00"), at=NOW).record
    assert armed.stop_quote == Decimal("7.00")

    for price in ("9.50", "9.00", "8.00", "7.50", "7.01"):
        armed = observe(armed, observed_quote=Decimal(price), at=NOW).record
        assert armed.peak_quote == Decimal("10.00"), f"peak moved at {price}"
        assert armed.stop_quote == Decimal("7.00"), f"level moved at {price}"
        assert armed.state is TrailingStopState.ACTIVE


def test_a_long_sequence_never_lowers_the_level(system_config: SystemConfig) -> None:
    """Property-style: over any walk, the level is non-decreasing."""
    record = _record(system_config)
    walk = ["6.10", "7.60", "8.00", "7.20", "9.00", "8.10", "12.00", "11.00", "11.90"]
    levels: list[Decimal] = []

    for price in walk:
        record = observe(record, observed_quote=Decimal(price), at=NOW).record
        if record.stop_quote is not None:
            levels.append(record.stop_quote)
        if record.state is TrailingStopState.TRIGGERED:
            break

    assert levels == sorted(levels), f"the trailing level fell: {levels}"


def test_a_new_high_below_the_improvement_threshold_holds_the_level(
    system_config: SystemConfig,
) -> None:
    """The peak still moves; the level does not, so the history is not noise."""
    armed = observe(_record(system_config), observed_quote=Decimal("10.00"), at=NOW).record

    nudged = observe(armed, observed_quote=Decimal("10.05"), at=NOW).record

    assert nudged.peak_quote == Decimal("10.05")
    assert nudged.stop_quote == Decimal("7.00")


# ---------------------------------------------------------------------------
# Triggering
# ---------------------------------------------------------------------------
def test_a_price_at_the_level_triggers(system_config: SystemConfig) -> None:
    armed = observe(_record(system_config), observed_quote=Decimal("10.00"), at=NOW).record

    fired = observe(armed, observed_quote=Decimal("7.00"), at=NOW)

    assert fired.record.state is TrailingStopState.TRIGGERED
    assert fired.record.trigger_quote == Decimal("7.00")
    assert fired.triggered
    assert fired.event_type is PositionLifecycleEventType.TRAILING_TRIGGERED


def test_a_triggered_trail_is_terminal_and_absorbs_later_observations(
    system_config: SystemConfig,
) -> None:
    """A stop that re-armed after firing would sell a position twice."""
    armed = observe(_record(system_config), observed_quote=Decimal("10.00"), at=NOW).record
    fired = observe(armed, observed_quote=Decimal("6.00"), at=NOW).record

    again = observe(fired, observed_quote=Decimal("20.00"), at=NOW)

    assert again.record.state is TrailingStopState.TRIGGERED
    assert again.record.peak_quote == Decimal("10.00")
    assert not again.changed


# ---------------------------------------------------------------------------
# The verdict, separate from the observation
# ---------------------------------------------------------------------------
def test_an_inactive_trail_waits_and_says_what_it_is_waiting_for(
    system_config: SystemConfig,
) -> None:
    outcome = evaluate_trailing(
        _record(system_config), observed_quote=Decimal("6.10"), enabled=True
    )

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.TRAILING_NOT_ACTIVE
    assert Decimal(outcome.threshold or "0") == Decimal("7.50")


def test_a_triggered_trail_exits(system_config: SystemConfig) -> None:
    armed = observe(_record(system_config), observed_quote=Decimal("10.00"), at=NOW).record
    fired = observe(armed, observed_quote=Decimal("6.00"), at=NOW).record

    outcome = evaluate_trailing(fired, observed_quote=Decimal("6.00"), enabled=True)

    assert outcome.decision is ExitDecisionType.EXIT
    assert outcome.reason_code is ExitReasonCode.TRAILING_STOP_TRIGGERED


def test_an_unpriced_observation_is_not_evaluated_rather_than_passed(
    system_config: SystemConfig,
) -> None:
    """Exactly as an untested risk limit is NOT_EVALUATED rather than PASS."""
    outcome = evaluate_trailing(_record(system_config), observed_quote=None, enabled=True)

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.NOT_EVALUATED
    assert outcome.evaluated is False


def test_a_disabled_trail_is_not_evaluated(system_config: SystemConfig) -> None:
    outcome = evaluate_trailing(
        _record(system_config), observed_quote=Decimal("6.00"), enabled=False
    )

    assert outcome.reason_code is ExitReasonCode.NOT_EVALUATED


# ---------------------------------------------------------------------------
# Restart: the state survives the process
# ---------------------------------------------------------------------------
def test_a_reloaded_trail_reproduces_the_same_state_and_decision(
    system_config: SystemConfig, exit_repo
) -> None:
    """Persist, discard the object, reload from disk, replay the observation.

    Shares only the *filesystem* with the first run, so anything the second
    knows came off disk. This is the restart guarantee, checked rather than
    asserted.
    """
    record = _record(system_config)
    record = observe(record, observed_quote=Decimal("10.00"), at=NOW).record
    record = observe(record, observed_quote=Decimal("12.00"), at=NOW + timedelta(minutes=5)).record
    exit_repo.save_trailing(record)

    reloaded = exit_repo.trailing("strategypos-1")

    assert reloaded is not None
    assert reloaded == record
    assert reloaded.peak_quote == Decimal("12.00")
    assert reloaded.stop_quote == Decimal("8.40")

    before = observe(record, observed_quote=Decimal("8.40"), at=NOW + timedelta(minutes=10))
    after = observe(reloaded, observed_quote=Decimal("8.40"), at=NOW + timedelta(minutes=10))

    assert before.record == after.record
    assert after.record.state is TrailingStopState.TRIGGERED


def test_a_restart_does_not_restart_the_trail_from_the_current_price(
    system_config: SystemConfig, exit_repo
) -> None:
    """The failure a memory-only trailing stop has: after a restart it would
    re-arm from wherever the price happens to be, and the peak would be lost."""
    record = observe(_record(system_config), observed_quote=Decimal("12.00"), at=NOW).record
    exit_repo.save_trailing(record)

    reloaded = exit_repo.trailing("strategypos-1")
    assert reloaded is not None

    outcome = observe(reloaded, observed_quote=Decimal("9.00"), at=NOW)

    assert outcome.record.peak_quote == Decimal("12.00")
    assert outcome.record.stop_quote == Decimal("8.40")
    assert outcome.record.state is TrailingStopState.ACTIVE


def test_the_quote_field_travels_with_the_trail(system_config: SystemConfig) -> None:
    """Which price the level was measured against is part of the record."""
    record = _record(system_config, quote_field=ExitQuoteField.MID)

    assert record.quote_field is ExitQuoteField.MID
    assert factories.trailing_record().quote_field is ExitQuoteField.BID
