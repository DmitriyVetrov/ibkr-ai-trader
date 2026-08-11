"""Trading calendar.

"Monday to Friday is a trading day" is wrong about ten days a year, and wrong
in exactly the way that quietly corrupts a dataset: a collector that expects a
snapshot on Thanksgiving records a gap that never existed, and a bar labelled
2026-12-25 is evidence of a bug rather than of a session.

Two design choices matter:

* **Holidays are configuration, not code.** They come from
  ``config/data.yaml``, where they can be reviewed and diffed.
* **The calendar refuses to guess.** Outside the years whose holidays have
  actually been verified, every query answers "unknown" rather than assuming a
  weekday is open. An honest unknown is usable — a confident wrong answer is
  not.

Exchange-local time appears only here and at the presentation boundary;
everything the calendar returns is timezone-aware UTC. US market holidays do
not shift with DST, but the session open does, so the conversion goes through
``zoneinfo`` rather than a fixed offset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trading_system.infrastructure.settings import MarketCalendarConfig

__all__ = [
    "MarketCalendar",
    "MarketCalendarError",
    "MarketSession",
    "TradingDayStatus",
]


class MarketCalendarError(RuntimeError):
    """The calendar cannot answer, and will not invent an answer."""


class TradingDayStatus:
    """Answer to "is this a trading day", including the honest third option."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class MarketSession:
    """One trading session, in UTC.

    ``early_close`` is kept as a separate fact rather than being inferred from
    the close time: a consumer deciding whether a thin afternoon is meaningful
    needs to know the session was short by design.
    """

    session_date: date
    open_at: datetime
    close_at: datetime
    early_close: bool = False

    def contains(self, instant: datetime) -> bool:
        return self.open_at <= instant < self.close_at


def _parse_clock_time(value: str, label: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(int(hour_text), int(minute_text))
    except (ValueError, TypeError) as exc:
        raise MarketCalendarError(f"market calendar {label} is not HH:MM: {value!r}") from exc


class MarketCalendar:
    """Exchange trading calendar built from configuration."""

    def __init__(self, config: MarketCalendarConfig) -> None:
        self._config = config
        try:
            self._zone = ZoneInfo(config.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise MarketCalendarError(
                f"unknown market calendar timezone {config.timezone!r}"
            ) from exc

        self._open_time = _parse_clock_time(config.regular_open, "regular_open")
        self._close_time = _parse_clock_time(config.regular_close, "regular_close")
        self._early_close_time = _parse_clock_time(config.early_close_time, "early_close_time")
        self._holidays = frozenset(config.holidays)
        self._early_closes = frozenset(config.early_closes)
        self._covered_years = frozenset(config.covered_years)

    @property
    def exchange(self) -> str:
        return self._config.exchange

    @property
    def timezone(self) -> ZoneInfo:
        return self._zone

    @property
    def covered_years(self) -> frozenset[int]:
        return self._covered_years

    def covers(self, day: date) -> bool:
        """Whether holidays for this year have actually been verified."""
        return day.year in self._covered_years

    def status(self, day: date) -> str:
        """``OPEN`` / ``CLOSED`` / ``UNKNOWN`` for one calendar day.

        A weekend is ``CLOSED`` even outside the covered years — that much is
        true of every year. A weekday outside the covered years is ``UNKNOWN``,
        because it might be a holiday nobody has entered yet.
        """
        if day.weekday() >= 5:
            return TradingDayStatus.CLOSED
        if not self.covers(day):
            return TradingDayStatus.UNKNOWN
        if day in self._holidays:
            return TradingDayStatus.CLOSED
        return TradingDayStatus.OPEN

    def is_trading_day(self, day: date) -> bool:
        """Whether the market trades on ``day``.

        Raises rather than guessing when the year is not covered. Callers that
        can tolerate uncertainty should use :meth:`status`.
        """
        result = self.status(day)
        if result == TradingDayStatus.UNKNOWN:
            raise MarketCalendarError(
                f"{day.isoformat()} falls outside the verified calendar years "
                f"{sorted(self._covered_years)}; add the year's holidays to "
                f"config/data.yaml rather than assuming the market is open"
            )
        return result == TradingDayStatus.OPEN

    def is_early_close(self, day: date) -> bool:
        return day in self._early_closes

    def session(self, day: date) -> MarketSession | None:
        """The session for ``day``, or ``None`` when the market is closed."""
        if not self.is_trading_day(day):
            return None
        early = self.is_early_close(day)
        close_time = self._early_close_time if early else self._close_time
        return MarketSession(
            session_date=day,
            open_at=self._to_utc(day, self._open_time),
            close_at=self._to_utc(day, close_time),
            early_close=early,
        )

    def is_open(self, instant: datetime) -> bool:
        """Whether regular trading is in progress at ``instant`` (aware UTC)."""
        local = self._require_aware(instant).astimezone(self._zone)
        session = self.session(local.date())
        return session is not None and session.contains(instant.astimezone(UTC))

    def previous_trading_day(self, day: date, *, max_lookback: int = 10) -> date | None:
        """The most recent trading day strictly before ``day``.

        ``max_lookback`` bounds the search so an exhausted or misconfigured
        calendar fails as ``None`` rather than looping.
        """
        from datetime import timedelta

        candidate = day
        for _ in range(max_lookback):
            candidate = candidate - timedelta(days=1)
            if self.status(candidate) == TradingDayStatus.OPEN:
                return candidate
        return None

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """Trading days in ``[start, end]``. Days of unknown status are excluded.

        Excluded rather than assumed open: an unverified day must not silently
        become an expected data point, which would manufacture a gap.
        """
        from datetime import timedelta

        if end < start:
            return []
        days: list[date] = []
        candidate = start
        while candidate <= end:
            if self.status(candidate) == TradingDayStatus.OPEN:
                days.append(candidate)
            candidate += timedelta(days=1)
        return days

    def _to_utc(self, day: date, clock_time: time) -> datetime:
        """Exchange-local wall time to UTC, honouring daylight saving."""
        return datetime.combine(day, clock_time, tzinfo=self._zone).astimezone(UTC)

    @staticmethod
    def _require_aware(instant: datetime) -> datetime:
        if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
            raise MarketCalendarError("market calendar requires a timezone-aware datetime")
        return instant

    def __repr__(self) -> str:
        return (
            f"MarketCalendar(exchange={self._config.exchange}, "
            f"timezone={self._config.timezone}, "
            f"covered_years={sorted(self._covered_years)})"
        )
