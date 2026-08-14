"""Days to expiration, and what the number means.

A pure function of its arguments — no clock, no broker, no repository. The
instant is supplied and the calendar is injected, which is what makes a stored
expiration verdict reproducible after the fact.

**This is an options system, and expiration is a safety policy rather than an
observation.** A stock position has no deadline; a long option loses the whole
premium on a date, and the last week of one is where time decay takes back what
the thesis earned. So the expiration policy sits above take-profit and trailing
in :data:`~trading_system.domain.enums.EXIT_POLICY_PRECEDENCE`: a position that
is simultaneously at its profit target and one day from expiry exits because of
the deadline, and the record says so.

Three rules, each with tests that fail loudly:

* **The date is the exchange's, not UTC's.** DTE counts calendar days from the
  *exchange-local* date of the evaluation instant to the expiration, through
  ``config/data.yaml``'s market calendar. Counting from a UTC date is wrong by
  one for most of the evening — which is exactly when a force-exit threshold
  matters, and exactly the bug Milestone 6 recorded and fixed for selection.
* **DTE 0 is not "a day of trading left".** It means the contract expires
  today. Whether the session is open, short, or already over is a calendar
  question, and :func:`session_state` answers it rather than assuming.
* **An unverified year is unknown, never open.** The calendar covers the years
  whose holidays were actually transcribed. Outside them the answer is
  ``UNKNOWN`` and, by default, a block — a deadline nobody checked must not
  pass as ordinary.

Assignment and exercise are deliberately **not** modelled. This system holds
long options, which are never assigned; early exercise is a right it does not
use, and expiring in the money produces a broker action Milestone 9 would
observe as a position change. Inventing an assignment model here would be
inventing broker behaviour, so a contract whose expiration has already passed
is ``EXPIRATION_DATA_UNAVAILABLE``-adjacent: it blocks, because an expired
option cannot be sold and an order for one is not an exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum, unique

from trading_system.data.market_calendar import MarketCalendar, TradingDayStatus
from trading_system.domain.enums import ExitDecisionType, ExitPolicyKind, ExitReasonCode
from trading_system.exit.models import ExitPolicyOutcome
from trading_system.infrastructure.settings import ExitExpirationConfig

__all__ = [
    "ExpirationView",
    "SessionState",
    "days_to_expiration",
    "evaluate_expiration",
    "exchange_local_date",
    "expiration_view",
    "session_state",
]


@unique
class SessionState(StrEnum):
    """What the exchange is doing on the expiration date itself.

    Recorded rather than inferred from the DTE, because "expires today" and
    "expires today and the market is already closed" are different facts and
    only the second one means the position can no longer be sold.
    """

    #: Regular trading is in progress at the evaluation instant.
    OPEN = "OPEN"
    #: A session exists today but the evaluation instant is outside it.
    CLOSED = "CLOSED"
    #: The calendar says the exchange does not trade on this day at all.
    NOT_A_TRADING_DAY = "NOT_A_TRADING_DAY"
    #: The year is outside the verified calendar. No claim is made.
    UNKNOWN = "UNKNOWN"


def exchange_local_date(instant: datetime, calendar: MarketCalendar) -> date:
    """The exchange-local date of an instant.

    DTE is a count of calendar days to an expiration, and an expiration is a
    date *at the exchange*. Counting from a UTC date would be wrong by one for
    every instant after the exchange's local midnight, which in this system is
    most of the evening. The same function exists in the contract selector for
    the same reason; both go through the calendar's own timezone rather than a
    fixed offset, so daylight saving is handled by ``zoneinfo``.
    """
    return instant.astimezone(calendar.timezone).date()


def days_to_expiration(expiration: date, *, as_of: datetime, calendar: MarketCalendar) -> int:
    """Calendar days from the exchange-local date of ``as_of`` to ``expiration``.

    Negative for a contract that has already expired, which is a real answer
    and is what lets the policy refuse rather than treat it as urgent.
    """
    return (expiration - exchange_local_date(as_of, calendar)).days


def session_state(day: date, *, as_of: datetime, calendar: MarketCalendar) -> SessionState:
    """What the exchange is doing on ``day``, at ``as_of``."""
    status = calendar.status(day)
    if status == TradingDayStatus.UNKNOWN:
        return SessionState.UNKNOWN
    if status == TradingDayStatus.CLOSED:
        return SessionState.NOT_A_TRADING_DAY
    session = calendar.session(day)
    if session is None:  # pragma: no cover - status OPEN implies a session
        return SessionState.UNKNOWN
    return SessionState.OPEN if session.contains(as_of) else SessionState.CLOSED


@dataclass(frozen=True, slots=True)
class ExpirationView:
    """Everything the expiration policy needs, computed once.

    ``dte`` is the *nearest* expiration across the structure's legs. Every leg
    of every strategy shipped today shares one expiration (``same_expiration``
    on the structure), but a calendar spread would not, and the nearest one is
    the one that binds: whichever leg expires first is when the structure stops
    being the structure that was authorised.
    """

    dte: int | None
    expiration: date | None
    reference_date: date
    session: SessionState
    calendar_covered: bool
    #: True when at least one leg reported no expiration at all.
    missing_expiration: bool = False

    @property
    def expired(self) -> bool:
        return self.dte is not None and self.dte < 0


def expiration_view(
    expirations: list[date | None],
    *,
    as_of: datetime,
    calendar: MarketCalendar,
) -> ExpirationView:
    """Build the view from every leg's expiration.

    A leg with no expiration is recorded as such rather than skipped: an option
    position whose contract terms we cannot read is one we cannot judge, and
    quietly computing the DTE from the legs that did report one would hide it.
    """
    reference = exchange_local_date(as_of, calendar)
    known = [expiration for expiration in expirations if expiration is not None]
    missing = len(known) != len(expirations)
    if not known:
        return ExpirationView(
            dte=None,
            expiration=None,
            reference_date=reference,
            session=SessionState.UNKNOWN,
            calendar_covered=False,
            missing_expiration=True,
        )
    nearest = min(known)
    return ExpirationView(
        dte=(nearest - reference).days,
        expiration=nearest,
        reference_date=reference,
        session=session_state(nearest, as_of=as_of, calendar=calendar),
        calendar_covered=calendar.covers(nearest),
        missing_expiration=missing,
    )


def evaluate_expiration(
    view: ExpirationView, *, config: ExitExpirationConfig, force_exit_dte: int
) -> ExitPolicyOutcome:
    """Decide what the remaining time means. Pure, and never fabricates a date.

    ``force_exit_dte`` is the *effective* threshold — the strategy's, where it
    narrows the global one — and is passed in rather than read from ``config``
    so this function has no opinion about the layering. Configuration loading
    already refused any strategy that widened it.
    """
    if view.missing_expiration or view.dte is None or view.expiration is None:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.EXPIRATION,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.EXPIRATION_DATA_UNAVAILABLE,
            summary="a leg of this structure reports no expiration",
            detail=(
                "days to expiration cannot be computed, so the expiration safety policy "
                "cannot be applied. An option position whose contract terms cannot be read "
                "is not one to make a timing decision about"
            ),
            evaluated=False,
        )

    if view.expired:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.EXPIRATION,
            decision=(
                ExitDecisionType.BLOCK
                if config.block_on_expired_contract
                else ExitDecisionType.EXIT
            ),
            reason_code=(
                ExitReasonCode.EXPIRATION_DATA_UNAVAILABLE
                if config.block_on_expired_contract
                else ExitReasonCode.EXPIRATION_FORCE_EXIT
            ),
            measured=str(view.dte),
            threshold=str(force_exit_dte),
            summary=(
                f"the nearest expiration {view.expiration.isoformat()} is "
                f"{abs(view.dte)} day(s) in the past"
            ),
            detail=(
                "an expired option cannot be sold, so an exit order for one is not an exit. "
                "What actually happened to it — expiry worthless, automatic exercise, an "
                "assignment — is a broker action, and Milestone 9 observes broker actions. "
                "Nothing here models one"
            ),
        )

    if not view.calendar_covered and config.block_on_unknown_calendar:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.EXPIRATION,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.EXPIRATION_CALENDAR_UNKNOWN,
            measured=str(view.dte),
            threshold=str(force_exit_dte),
            summary=(
                f"expiration {view.expiration.isoformat()} falls in a year the market "
                f"calendar has not verified"
            ),
            detail=(
                "the calendar answers UNKNOWN outside the years whose holidays were actually "
                "transcribed. A deadline nobody checked must not pass as ordinary; add the "
                "year's holidays to config/data.yaml rather than assuming the session exists"
            ),
            evaluated=False,
        )

    if view.dte <= force_exit_dte:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.EXPIRATION,
            decision=ExitDecisionType.EXIT,
            reason_code=ExitReasonCode.EXPIRATION_FORCE_EXIT,
            measured=str(view.dte),
            threshold=str(force_exit_dte),
            summary=(
                f"{view.dte} day(s) to expiration {view.expiration.isoformat()}, at or below "
                f"the force-exit threshold of {force_exit_dte}"
            ),
            detail=(
                f"the exchange session on the expiration date is {view.session.value}. Long "
                f"premium decays fastest here, and a structure held to expiry is one whose "
                f"outcome nobody chose"
            ),
        )

    if view.dte <= config.warning_dte:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.EXPIRATION,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.EXPIRATION_WARNING,
            measured=str(view.dte),
            threshold=str(config.warning_dte),
            summary=(
                f"{view.dte} day(s) to expiration {view.expiration.isoformat()}, inside the "
                f"warning window of {config.warning_dte}"
            ),
            detail=(
                f"reported rather than acted on: this position is near its deadline and the "
                f"remaining policies still apply. It exits on its own at "
                f"{force_exit_dte} day(s)"
            ),
        )

    return ExitPolicyOutcome(
        policy=ExitPolicyKind.EXPIRATION,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.EXPIRATION_NOT_REACHED,
        measured=str(view.dte),
        threshold=str(config.warning_dte),
        summary=(
            f"{view.dte} day(s) to expiration {view.expiration.isoformat()}, above the "
            f"warning window of {config.warning_dte}"
        ),
    )
