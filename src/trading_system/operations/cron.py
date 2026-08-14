"""A deterministic five-field cron evaluator. Pure, and clock-free.

Written rather than taken from a library for one reason: **the scheduler has to
be reproducible under a** :class:`~trading_system.infrastructure.clock.FixedClock`.
Every scheduling library owns its own loop and its own notion of now, which
would make "what fires at 14:35 on a Monday" a question you answer by waiting
rather than by asserting. Here it is a function of two arguments.

Five fields, in the standard order::

    minute   0-59
    hour     0-23
    day      1-31
    month    1-12
    weekday  0-6, Sunday is 0 (7 is accepted and means Sunday too)

Each field accepts ``*``, a number, a ``a-b`` range, a ``*/n`` or ``a-b/n``
step, and a comma-separated list of any of those. That is the whole grammar,
deliberately: ``@reboot``, ``L``, ``W``, ``#`` and named months are all absent
because a cadence nobody can read at a glance is a cadence nobody can review,
and this file is where trading frequency is decided.

**Day-of-month and day-of-week are OR'd** when both are restricted, which is
what standard cron does and what surprises people. ``0 9 1 * 1`` fires on the
first of the month *and* on every Monday. The shipped schedules never restrict
both, so the rule never bites; it is implemented correctly anyway, because a
scheduler that silently disagreed with the crontab it was configured from would
be worse than one that refused the syntax.

Times are evaluated in the *configured timezone*, then compared in UTC. A
schedule that read "09:30" in UTC would fire an hour out for half the year,
which for a market-hours job means half the year of firing before the open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["CronError", "CronExpression", "matches", "next_fire", "parse_cron"]

#: How far ahead :func:`next_fire` will search before giving up. A year plus a
#: day covers every expression this grammar can express — including 29 February
#: in a non-leap year, which correctly has no next firing within the window and
#: is reported as ``None`` rather than searched for forever.
_SEARCH_LIMIT_MINUTES = 366 * 24 * 60


class CronError(ValueError):
    """A cron expression could not be parsed.

    A parse failure is a *configuration* failure and is raised at load, never
    swallowed at fire time. A job whose cadence nobody could parse must not
    quietly become a job that never runs.
    """


@dataclass(frozen=True, slots=True)
class CronExpression:
    """One parsed cadence, as the set of values each field admits."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: Whether the original text restricted the field at all. Needed because
    #: day-of-month and day-of-week are OR'd only when *both* are restricted.
    day_restricted: bool
    weekday_restricted: bool
    source: str

    def matches(self, moment: datetime) -> bool:
        """Whether this expression fires at ``moment``, to the minute.

        ``moment`` must already be in the schedule's timezone; converting here
        would hide the conversion from the caller, and the conversion is the
        part most likely to be wrong.
        """
        if moment.month not in self.months:
            return False
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False

        # Python's Monday-is-0 to cron's Sunday-is-0.
        weekday = (moment.weekday() + 1) % 7
        day_ok = moment.day in self.days
        weekday_ok = weekday in self.weekdays

        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok


def parse_cron(expression: str) -> CronExpression:
    """Parse a five-field cron expression, or raise :class:`CronError`."""
    fields = expression.split()
    if len(fields) != 5:
        raise CronError(
            f"cron expression {expression!r} has {len(fields)} field(s); five are required "
            f"(minute hour day-of-month month day-of-week)"
        )
    minute, hour, day, month, weekday = fields
    return CronExpression(
        minutes=_field(minute, low=0, high=59, name="minute", expression=expression),
        hours=_field(hour, low=0, high=23, name="hour", expression=expression),
        days=_field(day, low=1, high=31, name="day-of-month", expression=expression),
        months=_field(month, low=1, high=12, name="month", expression=expression),
        weekdays=_weekdays(weekday, expression=expression),
        day_restricted=day.strip() != "*",
        weekday_restricted=weekday.strip() != "*",
        source=expression,
    )


def matches(expression: str, moment: datetime, *, timezone: str = "UTC") -> bool:
    """Whether ``expression`` fires at ``moment``, evaluated in ``timezone``."""
    return parse_cron(expression).matches(_local(moment, timezone))


def next_fire(
    expression: str,
    after: datetime,
    *,
    timezone: str = "UTC",
    inclusive: bool = False,
) -> datetime | None:
    """The next instant this expression fires, strictly after ``after``.

    Returns an aware UTC datetime, or ``None`` when nothing fires within a
    year — which a valid expression such as ``0 0 29 2 *`` genuinely can do in
    a non-leap year. ``None`` is the honest answer there; searching forever
    would turn a rare calendar fact into a hung process.

    The search steps one minute at a time in the schedule's own timezone, which
    is what makes daylight-saving transitions behave: a job scheduled for 09:30
    local fires at 09:30 local on both sides of the change.
    """
    parsed = parse_cron(expression)
    zone = ZoneInfo(timezone)
    local = _local(after, timezone).replace(second=0, microsecond=0)
    if not inclusive:
        local += timedelta(minutes=1)

    for _ in range(_SEARCH_LIMIT_MINUTES):
        if parsed.matches(local):
            from datetime import UTC

            # Re-anchor to the zone so a naive arithmetic result across a DST
            # boundary carries the offset that actually applies to it.
            return local.replace(tzinfo=zone).astimezone(UTC)
        local += timedelta(minutes=1)
    return None


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------
def _local(moment: datetime, timezone: str) -> datetime:
    """An aware instant in the schedule's timezone, as naive local wall time.

    Naive on purpose: cron reasons about wall-clock fields, and keeping the
    offset attached through minute-by-minute arithmetic is how an hour goes
    missing twice a year.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise CronError("cron evaluation requires a timezone-aware instant")
    try:
        zone = ZoneInfo(timezone)
    except Exception as exc:
        raise CronError(f"unknown schedule timezone {timezone!r}") from exc
    return moment.astimezone(zone).replace(tzinfo=None)


def _field(text: str, *, low: int, high: int, name: str, expression: str) -> frozenset[int]:
    """One field's admitted values, as a set."""
    values: set[int] = set()
    for part in text.split(","):
        values.update(_part(part.strip(), low=low, high=high, name=name, expression=expression))
    if not values:
        raise CronError(f"cron {name} field {text!r} in {expression!r} admits no value")
    return frozenset(values)


def _part(part: str, *, low: int, high: int, name: str, expression: str) -> set[int]:
    if not part:
        raise CronError(f"cron {name} field in {expression!r} has an empty element")

    step = 1
    if "/" in part:
        part, _, step_text = part.partition("/")
        try:
            step = int(step_text)
        except ValueError as exc:
            raise CronError(
                f"cron {name} step {step_text!r} in {expression!r} is not a whole number"
            ) from exc
        if step <= 0:
            raise CronError(f"cron {name} step must be positive in {expression!r}")

    if part in ("*", ""):
        start, end = low, high
    elif "-" in part:
        start_text, _, end_text = part.partition("-")
        start, end = _number(start_text, name, expression), _number(end_text, name, expression)
    else:
        start = end = _number(part, name, expression)

    if not (low <= start <= high and low <= end <= high):
        raise CronError(f"cron {name} value {part!r} in {expression!r} is outside [{low}, {high}]")
    if start > end:
        raise CronError(
            f"cron {name} range {part!r} in {expression!r} runs backwards; write two "
            f"comma-separated ranges instead of relying on a wrap"
        )
    return set(range(start, end + 1, step))


def _weekdays(text: str, *, expression: str) -> frozenset[int]:
    """Weekdays, accepting 7 for Sunday as ordinary cron does."""
    values = _field(text, low=0, high=7, name="day-of-week", expression=expression)
    return frozenset(0 if value == 7 else value for value in values)


def _number(text: str, name: str, expression: str) -> int:
    try:
        return int(text.strip())
    except ValueError as exc:
        raise CronError(
            f"cron {name} value {text!r} in {expression!r} is not a whole number"
        ) from exc
