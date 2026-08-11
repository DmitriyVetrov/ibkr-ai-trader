"""Market calendar.

"Monday to Friday is a trading day" is wrong about ten days a year, and wrong
in the way that quietly corrupts a dataset: the collector records a gap on
Thanksgiving that never existed.

The calendar's other job is to refuse to guess. Outside the years whose
holidays have been verified it answers ``UNKNOWN`` rather than assuming a
weekday is open — an honest unknown is usable, a confident wrong answer is not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from trading_system.data.market_calendar import (
    MarketCalendar,
    MarketCalendarError,
    TradingDayStatus,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def calendar(data_config) -> MarketCalendar:
    return MarketCalendar(data_config.market_calendar)


# ---------------------------------------------------------------------------
# Weekends and holidays
# ---------------------------------------------------------------------------
def test_a_weekday_is_a_trading_day(calendar) -> None:
    assert calendar.is_trading_day(date(2026, 8, 10))  # Monday


@pytest.mark.parametrize("day", [date(2026, 8, 8), date(2026, 8, 9)])
def test_weekends_are_closed(calendar, day: date) -> None:
    assert not calendar.is_trading_day(day)


@pytest.mark.parametrize(
    ("day", "label"),
    [
        (date(2026, 1, 1), "New Year's Day"),
        (date(2026, 1, 19), "Martin Luther King Jr. Day"),
        (date(2026, 4, 3), "Good Friday"),
        (date(2026, 5, 25), "Memorial Day"),
        (date(2026, 6, 19), "Juneteenth"),
        (date(2026, 7, 3), "Independence Day observed"),
        (date(2026, 9, 7), "Labor Day"),
        (date(2026, 11, 26), "Thanksgiving"),
        (date(2026, 12, 25), "Christmas"),
    ],
)
def test_holidays_are_closed_even_though_they_are_weekdays(calendar, day: date, label: str) -> None:
    """The whole reason weekday-equals-open is wrong."""
    assert day.weekday() < 5, f"{label} should be a weekday for this test to mean anything"
    assert not calendar.is_trading_day(day)


def test_a_holiday_has_no_session(calendar) -> None:
    assert calendar.session(date(2026, 11, 26)) is None


# ---------------------------------------------------------------------------
# Sessions and daylight saving
# ---------------------------------------------------------------------------
def test_a_session_is_returned_in_utc(calendar) -> None:
    session = calendar.session(date(2026, 8, 10))

    assert session is not None
    assert session.open_at.tzinfo is not None
    assert session.open_at == datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    assert session.close_at == datetime(2026, 8, 10, 20, 0, tzinfo=UTC)


def test_daylight_saving_shifts_the_session(calendar) -> None:
    """US markets open at 09:30 local; the UTC offset is not fixed."""
    summer = calendar.session(date(2026, 8, 10))
    winter = calendar.session(date(2026, 12, 10))

    assert summer is not None and winter is not None
    assert summer.open_at.hour == 13  # EDT, UTC-4
    assert winter.open_at.hour == 14  # EST, UTC-5


def test_an_early_close_is_shorter_and_says_so(calendar) -> None:
    normal = calendar.session(date(2026, 11, 25))
    early = calendar.session(date(2026, 11, 27))

    assert normal is not None and early is not None
    assert not normal.early_close
    assert early.early_close
    assert early.close_at < normal.close_at.replace(day=27)


def test_is_open_tracks_the_session(calendar) -> None:
    assert calendar.is_open(datetime(2026, 8, 10, 15, 0, tzinfo=UTC))
    assert not calendar.is_open(datetime(2026, 8, 10, 12, 0, tzinfo=UTC))
    assert not calendar.is_open(datetime(2026, 8, 10, 21, 0, tzinfo=UTC))


def test_the_market_is_closed_on_a_weekend_instant(calendar) -> None:
    assert not calendar.is_open(datetime(2026, 8, 8, 15, 0, tzinfo=UTC))


# ---------------------------------------------------------------------------
# It refuses to guess
# ---------------------------------------------------------------------------
def test_an_uncovered_year_is_unknown_not_open(calendar) -> None:
    """A weekday in an unverified year might be a holiday nobody entered."""
    assert calendar.status(date(2031, 3, 12)) == TradingDayStatus.UNKNOWN

    with pytest.raises(MarketCalendarError, match="outside the verified calendar"):
        calendar.is_trading_day(date(2031, 3, 12))


def test_a_weekend_is_closed_even_in_an_uncovered_year(calendar) -> None:
    """Some things are true of every year."""
    assert calendar.status(date(2031, 3, 15)) == TradingDayStatus.CLOSED


def test_uncovered_days_are_excluded_from_a_range_not_assumed_open(calendar) -> None:
    """An unverified day must not become an expected data point.

    The range straddles the end of coverage. 2026 and 2027 are verified; 2028
    is not, so its weekdays are excluded rather than assumed open — which is
    what stops the collector manufacturing a gap it then reports as missing
    data.
    """
    days = calendar.trading_days_between(date(2027, 12, 27), date(2028, 1, 7))

    assert days, "the covered part of the range still yields sessions"
    assert all(day.year == 2027 for day in days)


def test_a_naive_instant_is_refused(calendar) -> None:
    with pytest.raises(MarketCalendarError, match="timezone-aware"):
        calendar.is_open(datetime(2026, 8, 10, 15, 0))


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------
def test_a_week_containing_a_holiday_has_four_trading_days(calendar) -> None:
    days = calendar.trading_days_between(date(2026, 11, 23), date(2026, 11, 27))

    assert date(2026, 11, 26) not in days
    assert len(days) == 4


def test_the_previous_trading_day_skips_the_weekend(calendar) -> None:
    assert calendar.previous_trading_day(date(2026, 8, 10)) == date(2026, 8, 7)


def test_the_previous_trading_day_skips_a_holiday(calendar) -> None:
    assert calendar.previous_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)


def test_an_inverted_range_is_empty(calendar) -> None:
    assert calendar.trading_days_between(date(2026, 8, 10), date(2026, 8, 1)) == []


# ---------------------------------------------------------------------------
# Configuration, not hard-coding
# ---------------------------------------------------------------------------
def test_the_calendar_is_built_from_configuration(data_config) -> None:
    assert data_config.market_calendar.holidays
    assert data_config.market_calendar.covered_years == [2026, 2027]
    assert data_config.market_calendar.timezone == "America/New_York"


def test_an_unknown_timezone_is_refused(data_config) -> None:
    broken = data_config.market_calendar.model_copy(update={"timezone": "Mars/Olympus"})

    with pytest.raises(MarketCalendarError, match="timezone"):
        MarketCalendar(broken)


def test_a_malformed_session_time_is_refused(data_config) -> None:
    broken = data_config.market_calendar.model_copy(update={"regular_open": "half past nine"})

    with pytest.raises(MarketCalendarError, match="HH:MM"):
        MarketCalendar(broken)


# ---------------------------------------------------------------------------
# 2027 coverage (Milestone 4 brief section 46)
#
# Transcribed from the NYSE holiday and early-closings calendar published at
# https://www.nyse.com/markets/hours-calendars — not derived. The observance
# rules have edge cases and the early-close list does not follow from them at
# all, so a "computed" 2027 would be invented data wearing a formula.
# ---------------------------------------------------------------------------
NYSE_2027_HOLIDAYS = (
    (date(2027, 1, 1), "New Year's Day"),
    (date(2027, 1, 18), "Martin Luther King Jr. Day"),
    (date(2027, 2, 15), "Washington's Birthday"),
    (date(2027, 3, 26), "Good Friday"),
    (date(2027, 5, 31), "Memorial Day"),
    (date(2027, 6, 18), "Juneteenth observed"),
    (date(2027, 7, 5), "Independence Day observed"),
    (date(2027, 9, 6), "Labor Day"),
    (date(2027, 11, 25), "Thanksgiving"),
    (date(2027, 12, 24), "Christmas observed"),
)


@pytest.mark.parametrize(("day", "label"), NYSE_2027_HOLIDAYS, ids=lambda v: str(v))
def test_a_2027_holiday_is_closed(calendar, day: date, label: str) -> None:
    assert calendar.status(day) == TradingDayStatus.CLOSED, label
    assert calendar.is_trading_day(day) is False


def test_a_normal_2027_weekday_is_open(calendar) -> None:
    assert calendar.status(date(2027, 3, 25)) == TradingDayStatus.OPEN
    assert calendar.is_trading_day(date(2027, 3, 25)) is True


def test_a_2027_weekend_is_closed(calendar) -> None:
    assert calendar.status(date(2027, 3, 27)) == TradingDayStatus.CLOSED  # Saturday
    assert calendar.status(date(2027, 3, 28)) == TradingDayStatus.CLOSED  # Sunday


def test_the_2027_day_after_thanksgiving_closes_early(calendar) -> None:
    session = calendar.session(date(2027, 11, 26))

    assert session is not None
    assert session.early_close is True
    assert session.close_at.astimezone(ZoneInfo("America/New_York")).hour == 13


def test_2027_has_no_christmas_eve_early_close(calendar) -> None:
    """24 December 2027 is the observed Christmas holiday: fully closed, not short.

    Carrying the 2026 pattern forward would have produced a session on a day the
    exchange is shut — which is why the calendar is transcribed, not derived.
    """
    assert calendar.is_early_close(date(2027, 12, 24)) is False
    assert calendar.status(date(2027, 12, 24)) == TradingDayStatus.CLOSED


def test_2027_has_no_july_3_early_close(calendar) -> None:
    """3 July 2027 falls on a Saturday, so there is no early close to observe."""
    assert calendar.is_early_close(date(2027, 7, 3)) is False
    assert calendar.status(date(2027, 7, 2)) == TradingDayStatus.OPEN


def test_a_2028_weekday_is_unknown_not_assumed_open(calendar) -> None:
    """Beyond verified coverage the calendar says so rather than guessing."""
    assert calendar.status(date(2028, 3, 15)) == TradingDayStatus.UNKNOWN

    with pytest.raises(MarketCalendarError, match="outside the verified calendar years"):
        calendar.is_trading_day(date(2028, 3, 15))


def test_a_2028_weekend_is_still_closed(calendar) -> None:
    """Weekends need no verification — that much is true of every year."""
    assert calendar.status(date(2028, 3, 18)) == TradingDayStatus.CLOSED
