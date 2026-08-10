"""Time source abstraction.

Every timestamp in the system comes from a :class:`Clock`, never from a direct
``datetime.now()`` call. Two reasons:

* tests must be deterministic and reproducible;
* historical evaluation must be able to run "as of" a past instant without
  leaking future information into the result (specification section 21).

All clocks return timezone-aware UTC datetimes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FixedClock", "SystemClock", "utc_now"]


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


@runtime_checkable
class Clock(Protocol):
    """Anything that can report the current time."""

    def now(self) -> datetime:
        """Return the current time, timezone-aware and in UTC."""
        ...


class SystemClock:
    """Wall-clock time. The production default."""

    def now(self) -> datetime:
        return utc_now()

    def __repr__(self) -> str:
        return "SystemClock()"


class FixedClock:
    """A clock frozen at a chosen instant, for tests and historical replay.

    ``advance`` exists so a test can step time forward explicitly; time never
    moves on its own.
    """

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def advance(self, *, seconds: float = 0.0, days: float = 0.0) -> datetime:
        """Move the clock forward and return the new instant."""
        from datetime import timedelta

        self._instant = self._instant + timedelta(seconds=seconds, days=days)
        return self._instant

    def __repr__(self) -> str:
        return f"FixedClock({self._instant.isoformat()})"
