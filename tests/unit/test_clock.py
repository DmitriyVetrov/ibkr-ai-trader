"""Clock abstraction: aware UTC everywhere, and deterministic under test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from trading_system.infrastructure.clock import Clock, FixedClock, SystemClock, utc_now


@pytest.mark.unit
def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


@pytest.mark.unit
def test_utc_now_is_aware() -> None:
    assert utc_now().tzinfo is UTC


@pytest.mark.unit
def test_fixed_clock_does_not_move_on_its_own(fixed_clock: FixedClock) -> None:
    assert fixed_clock.now() == fixed_clock.now()


@pytest.mark.unit
def test_fixed_clock_advances_only_when_told(fixed_clock: FixedClock) -> None:
    start = fixed_clock.now()
    fixed_clock.advance(days=1)
    assert fixed_clock.now() - start == timedelta(days=1)


@pytest.mark.unit
def test_fixed_clock_normalises_to_utc() -> None:
    madrid = timezone(timedelta(hours=2))
    clock = FixedClock(datetime(2026, 8, 10, 16, 30, tzinfo=madrid))
    assert clock.now() == datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


@pytest.mark.unit
def test_fixed_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 8, 10, 14, 30))


@pytest.mark.unit
@pytest.mark.parametrize("clock", [SystemClock(), FixedClock(utc_now())])
def test_clocks_satisfy_the_protocol(clock: Clock) -> None:
    assert isinstance(clock, Clock)
