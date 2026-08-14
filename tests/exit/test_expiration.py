"""Days to expiration, and why the date has to be the exchange's.

This is an options system, and the expiration policy is the one that stops a
long-premium position being held into the week where time decay takes back what
the thesis earned. Two properties matter more than the thresholds themselves:
the date is exchange-local, and an unverified year is unknown rather than open.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from trading_system.data.market_calendar import MarketCalendar
from trading_system.domain.enums import ExitDecisionType, ExitReasonCode
from trading_system.exit.expiration import (
    SessionState,
    days_to_expiration,
    evaluate_expiration,
    exchange_local_date,
    expiration_view,
    session_state,
)
from trading_system.infrastructure.settings import SystemConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def calendar(system_config: SystemConfig) -> MarketCalendar:
    return MarketCalendar(system_config.data.market_calendar)


# ---------------------------------------------------------------------------
# The date is the exchange's, not UTC's
# ---------------------------------------------------------------------------
def test_the_reference_date_is_exchange_local(calendar: MarketCalendar) -> None:
    """23:30 UTC on the 10th is 19:30 in New York on the *same* day.

    Counting from the UTC date is wrong by one for most of the evening, which
    is exactly when a force-exit threshold matters.
    """
    evening = datetime(2026, 8, 10, 23, 30, tzinfo=UTC)

    assert exchange_local_date(evening, calendar) == date(2026, 8, 10)


def test_after_utc_midnight_the_exchange_is_still_on_the_previous_day(
    calendar: MarketCalendar,
) -> None:
    """00:30 UTC on the 11th is 20:30 in New York on the 10th."""
    after_midnight = datetime(2026, 8, 11, 0, 30, tzinfo=UTC)

    assert exchange_local_date(after_midnight, calendar) == date(2026, 8, 10)


def test_dte_counted_from_utc_would_be_wrong_by_one(calendar: MarketCalendar) -> None:
    """The bug this function exists to prevent, stated as a comparison."""
    after_midnight = datetime(2026, 8, 11, 0, 30, tzinfo=UTC)
    expiration = date(2026, 8, 18)

    correct = days_to_expiration(expiration, as_of=after_midnight, calendar=calendar)
    naive = (expiration - after_midnight.date()).days

    assert correct == 8
    assert naive == 7
    assert correct != naive


def test_an_expired_contract_gives_a_negative_dte(calendar: MarketCalendar) -> None:
    """A real answer, and what lets the policy refuse rather than treat it as urgent."""
    assert (
        days_to_expiration(
            date(2026, 8, 1), as_of=datetime(2026, 8, 10, 14, 30, tzinfo=UTC), calendar=calendar
        )
        == -9
    )


# ---------------------------------------------------------------------------
# DTE 0 is not "a day of trading left"
# ---------------------------------------------------------------------------
def test_the_session_on_the_expiration_day_is_reported_not_assumed(
    calendar: MarketCalendar,
) -> None:
    during = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
    after = datetime(2026, 8, 10, 21, 30, tzinfo=UTC)

    assert session_state(date(2026, 8, 10), as_of=during, calendar=calendar) is SessionState.OPEN
    assert session_state(date(2026, 8, 10), as_of=after, calendar=calendar) is SessionState.CLOSED


def test_a_weekend_expiration_is_not_a_trading_day(calendar: MarketCalendar) -> None:
    saturday = date(2026, 8, 15)

    assert (
        session_state(saturday, as_of=datetime(2026, 8, 14, 14, 30, tzinfo=UTC), calendar=calendar)
        is SessionState.NOT_A_TRADING_DAY
    )


# ---------------------------------------------------------------------------
# The view over several legs
# ---------------------------------------------------------------------------
def test_the_nearest_expiration_binds(calendar: MarketCalendar) -> None:
    """Whichever leg expires first is when the structure stops being the
    structure that was authorised."""
    view = expiration_view(
        [date(2026, 9, 18), date(2026, 8, 21)],
        as_of=datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
        calendar=calendar,
    )

    assert view.expiration == date(2026, 8, 21)
    assert view.dte == 11


def test_a_leg_with_no_expiration_is_recorded_rather_than_skipped(
    calendar: MarketCalendar,
) -> None:
    view = expiration_view(
        [date(2026, 9, 18), None],
        as_of=datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
        calendar=calendar,
    )

    assert view.missing_expiration is True


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
def _view(expiration: date | None, *, calendar: MarketCalendar, missing: bool = False):
    return expiration_view(
        [expiration] if not missing else [None],
        as_of=datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
        calendar=calendar,
    )


def test_a_far_expiration_waits(system_config: SystemConfig, calendar: MarketCalendar) -> None:
    outcome = evaluate_expiration(
        _view(date(2026, 9, 18), calendar=calendar),
        config=system_config.exit.expiration,
        force_exit_dte=7,
    )

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.EXPIRATION_NOT_REACHED


def test_the_warning_window_reports_without_exiting(
    system_config: SystemConfig, calendar: MarketCalendar
) -> None:
    """A warning that exited would be a force-exit with a friendlier name."""
    outcome = evaluate_expiration(
        _view(date(2026, 8, 18), calendar=calendar),
        config=system_config.exit.expiration,
        force_exit_dte=5,
    )

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.EXPIRATION_WARNING
    assert outcome.measured == "8"


def test_the_force_exit_threshold_exits(
    system_config: SystemConfig, calendar: MarketCalendar
) -> None:
    outcome = evaluate_expiration(
        _view(date(2026, 8, 13), calendar=calendar),
        config=system_config.exit.expiration,
        force_exit_dte=5,
    )

    assert outcome.decision is ExitDecisionType.EXIT
    assert outcome.reason_code is ExitReasonCode.EXPIRATION_FORCE_EXIT
    assert outcome.measured == "3"
    assert outcome.threshold == "5"


def test_the_threshold_is_inclusive(system_config: SystemConfig, calendar: MarketCalendar) -> None:
    """At the threshold, not merely below it: "5 or fewer days" means 5 exits."""
    outcome = evaluate_expiration(
        _view(date(2026, 8, 15), calendar=calendar),
        config=system_config.exit.expiration,
        force_exit_dte=5,
    )

    assert outcome.measured == "5"
    assert outcome.decision is ExitDecisionType.EXIT


def test_a_strategy_may_force_an_exit_earlier(
    system_config: SystemConfig, calendar: MarketCalendar
) -> None:
    """The effective threshold is passed in, so the layering lives in one place."""
    view = _view(date(2026, 8, 18), calendar=calendar)

    at_global = evaluate_expiration(view, config=system_config.exit.expiration, force_exit_dte=5)
    at_strategy = evaluate_expiration(view, config=system_config.exit.expiration, force_exit_dte=10)

    assert at_global.decision is ExitDecisionType.WAIT
    assert at_strategy.decision is ExitDecisionType.EXIT


def test_a_missing_expiration_blocks(system_config: SystemConfig, calendar: MarketCalendar) -> None:
    outcome = evaluate_expiration(
        _view(None, calendar=calendar, missing=True),
        config=system_config.exit.expiration,
        force_exit_dte=5,
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.EXPIRATION_DATA_UNAVAILABLE
    assert outcome.evaluated is False


def test_an_uncovered_calendar_year_blocks_rather_than_assuming_a_session(
    system_config: SystemConfig, calendar: MarketCalendar
) -> None:
    """A deadline nobody verified must not pass as ordinary."""
    outcome = evaluate_expiration(
        _view(date(2030, 1, 18), calendar=calendar),
        config=system_config.exit.expiration,
        force_exit_dte=5,
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.EXPIRATION_CALENDAR_UNKNOWN


def test_an_already_expired_contract_blocks_rather_than_ordering_an_exit(
    system_config: SystemConfig, calendar: MarketCalendar
) -> None:
    """An expired option cannot be sold, and an order for one is not an exit."""
    outcome = evaluate_expiration(
        _view(date(2026, 8, 3), calendar=calendar),
        config=system_config.exit.expiration,
        force_exit_dte=5,
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert "in the past" in outcome.summary


def test_nothing_here_models_assignment_or_exercise(
    system_config: SystemConfig, calendar: MarketCalendar
) -> None:
    """This system holds long options; inventing a broker action would be
    inventing broker behaviour."""
    outcome = evaluate_expiration(
        _view(date(2026, 8, 3), calendar=calendar),
        config=system_config.exit.expiration,
        force_exit_dte=5,
    )

    assert "broker action" in (outcome.detail or "")
    assert "Milestone 9" in (outcome.detail or "")
