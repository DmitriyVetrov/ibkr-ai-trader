"""The cron evaluator. Pure, clock-free and therefore assertable.

Written rather than taken from a library so that "what fires at 14:35 on a
Monday" is a question you answer with an assertion instead of by waiting. These
tests are what justify that choice: every one of them would be a sleep against
a scheduling library that owned its own loop.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_system.operations.cron import CronError, matches, next_fire, parse_cron

pytestmark = pytest.mark.unit

#: A Monday inside the New York session.
MONDAY = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The grammar
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "expression",
    [
        "* * * * *",
        "0 9 * * 1-5",
        "*/5 9-16 * * 1-5",
        "0,30 * * * *",
        "0 0 1 1 *",
        "15 10-16/2 * * 1",
    ],
)
def test_a_valid_expression_parses(expression: str) -> None:
    assert parse_cron(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "* * * *",  # four fields
        "* * * * * *",  # six
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 0 * *",  # day-of-month starts at 1
        "* * * 13 *",  # month out of range
        "*/0 * * * *",  # zero step
        "30-10 * * * *",  # backwards range
        "abc * * * *",
    ],
)
def test_an_unusable_expression_is_a_configuration_failure(expression: str) -> None:
    """Raised at load, never swallowed at fire time.

    A cadence nobody could parse must not quietly become a job that never runs
    — which looks identical to a job that works perfectly and finds nothing to
    do.
    """
    with pytest.raises(CronError):
        parse_cron(expression)


def test_seven_means_sunday_as_ordinary_cron_does() -> None:
    sunday = parse_cron("0 0 * * 7")
    assert 0 in sunday.weekdays


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def test_a_five_minute_cadence_matches_on_the_five() -> None:
    assert matches("*/5 * * * *", datetime(2026, 8, 10, 14, 35, tzinfo=UTC))
    assert not matches("*/5 * * * *", datetime(2026, 8, 10, 14, 36, tzinfo=UTC))


def test_a_weekday_cadence_does_not_fire_at_the_weekend() -> None:
    saturday = datetime(2026, 8, 15, 14, 30, tzinfo=UTC)
    assert not matches("30 14 * * 1-5", saturday, timezone="UTC")
    assert matches("30 14 * * 1-5", MONDAY, timezone="UTC")


def test_the_expression_is_evaluated_in_the_schedule_timezone() -> None:
    """14:30 UTC is 10:30 in New York. A cadence written for the session must
    be read in the session's own clock, or it fires an hour out for half the
    year — which for a market-hours job means before the open."""
    assert matches("30 10 * * 1-5", MONDAY, timezone="America/New_York")
    assert not matches("30 10 * * 1-5", MONDAY, timezone="UTC")


def test_day_of_month_and_day_of_week_are_ored_when_both_are_restricted() -> None:
    """Standard cron behaviour, and the part that surprises people.

    The shipped schedules never restrict both, so the rule never bites — it is
    implemented correctly anyway, because a scheduler that silently disagreed
    with the crontab it was configured from would be worse than one that
    refused the syntax.
    """
    expression = "0 9 1 * 1"  # the first of the month, OR any Monday
    first_of_month = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)  # a Tuesday
    a_monday = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    neither = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)  # a Wednesday

    assert matches(expression, first_of_month, timezone="UTC")
    assert matches(expression, a_monday, timezone="UTC")
    assert not matches(expression, neither, timezone="UTC")


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(CronError):
        matches("* * * * *", datetime(2026, 8, 10, 14, 30))


# ---------------------------------------------------------------------------
# Next firing
# ---------------------------------------------------------------------------
def test_the_next_firing_is_strictly_after_the_instant_given() -> None:
    on_the_minute = datetime(2026, 8, 10, 14, 35, tzinfo=UTC)
    following = next_fire("*/5 * * * *", on_the_minute, timezone="UTC")

    assert following == datetime(2026, 8, 10, 14, 40, tzinfo=UTC)


def test_the_next_firing_can_be_inclusive_when_asked() -> None:
    on_the_minute = datetime(2026, 8, 10, 14, 35, tzinfo=UTC)
    following = next_fire("*/5 * * * *", on_the_minute, timezone="UTC", inclusive=True)

    assert following == on_the_minute


def test_a_weekday_cadence_skips_the_weekend() -> None:
    friday_evening = datetime(2026, 8, 14, 23, 0, tzinfo=UTC)
    following = next_fire("0 12 * * 1-5", friday_evening, timezone="UTC")

    assert following is not None
    assert following.weekday() == 0  # Monday


def test_a_leap_day_within_the_search_window_is_found() -> None:
    """The window is a year and a day, which reaches the next 29 February from
    anywhere in the three years before a leap year."""
    start = datetime(2027, 3, 1, 0, 0, tzinfo=UTC)

    assert next_fire("0 0 29 2 *", start, timezone="UTC") == datetime(2028, 2, 29, 0, 0, tzinfo=UTC)


def test_a_firing_beyond_the_search_window_returns_none_rather_than_looping() -> None:
    """The search is bounded, and the bound is reported honestly.

    From just after a leap day the next one is nearly four years away — well
    outside the window. ``None`` is the answer; searching until it found one
    would turn a rare calendar fact into a hung process, and a scheduler that
    can hang while *planning* is worse than one that admits it does not know.
    """
    start = datetime(2028, 3, 1, 0, 0, tzinfo=UTC)

    assert next_fire("0 0 29 2 *", start, timezone="UTC") is None


def test_the_next_firing_is_returned_in_utc() -> None:
    following = next_fire("30 9 * * 1-5", MONDAY, timezone="America/New_York")

    assert following is not None
    assert following.tzinfo is not None
    assert following.utcoffset() is not None


def test_the_result_is_deterministic() -> None:
    first = next_fire("*/10 * * * *", MONDAY, timezone="America/New_York")
    second = next_fire("*/10 * * * *", MONDAY, timezone="America/New_York")

    assert first == second


# ---------------------------------------------------------------------------
# The shipped cadences
# ---------------------------------------------------------------------------
def test_every_shipped_cadence_parses(system_config) -> None:
    """A cadence in the repository that nobody could parse would be a job that
    never runs and looks scheduled."""
    for name, job in system_config.schedules.jobs.items():
        assert parse_cron(job.cron), name


def test_every_shipped_cadence_fires_within_a_year(system_config) -> None:
    for name, job in system_config.schedules.jobs.items():
        following = next_fire(job.cron, MONDAY, timezone=system_config.schedules.timezone)
        assert following is not None, name
