# The expiration policy

Implemented in `src/trading_system/exit/expiration.py`. A pure function of its
arguments: the instant is supplied and the calendar is injected, which is what
makes a stored expiration verdict reproducible.

This is an **options** system, and expiration is a safety policy rather than an
observation. A stock position has no deadline; a long option loses the whole
premium on a date, and the last week of one is where time decay takes back what
the thesis earned. So expiration sits at position 5 in the precedence — above
maximum loss, take profit and the trailing stop, and below only the four
policies that establish whether the position can be judged at all.

## The date is the exchange's, not UTC's

DTE counts calendar days from the **exchange-local** date of the evaluation
instant to the expiration, through `config/data.yaml`'s market calendar.

Counting from a UTC date is wrong by one for most of the evening — which is
exactly when a force-exit threshold matters:

```
2026-08-11 00:30 UTC  =  2026-08-10 20:30 in New York
expiration 2026-08-18

correct (exchange-local):  8 days
naive (UTC date):          7 days
```

`tests/exit/test_expiration.py::test_dte_counted_from_utc_would_be_wrong_by_one`
asserts both numbers and that they differ. Milestone 6 recorded and fixed the
same bug for contract *selection*; this is the same rule applied to the exit
side, through the same calendar and the same `zoneinfo` conversion, so daylight
saving is handled rather than assumed.

## DTE 0 is not "a day of trading left"

It means the contract expires **today**. Whether the session is open, short, or
already over is a calendar question, and `session_state` answers it rather than
assuming:

| answer | means |
|---|---|
| `OPEN` | regular trading is in progress at the evaluation instant |
| `CLOSED` | a session exists today but the instant is outside it |
| `NOT_A_TRADING_DAY` | the exchange does not trade on this day at all |
| `UNKNOWN` | the year is outside the verified calendar; no claim is made |

The session state is recorded on the outcome's detail, so an operator reading a
force-exit knows whether there was still a market to exit into.

## An unverified year is unknown, never open

The shipped calendar covers 2026 and 2027 — the years whose NYSE holidays were
actually transcribed. Outside them the answer is `UNKNOWN` and, by default, a
block: a deadline nobody checked must not pass as ordinary.

`exit.expiration.block_on_unknown_calendar: false` relaxes it, and the honest
alternative is to add the year's holidays to `config/data.yaml` rather than
assume the session exists.

## An expired contract blocks; it does not force an exit

An expired option cannot be sold, so an order for one is not an exit. The
outcome is a **block**, and the detail says what actually happened to it is a
broker action — expiry worthless, automatic exercise, an assignment — which
Milestone 9 observes as a position change.

Nothing here models assignment or exercise. This system holds long options,
which are never assigned; early exercise is a right it does not use. Inventing a
model would be inventing broker behaviour.

## The thresholds

```yaml
expiration:
  warning_dte: 10        # near the deadline. REPORTED, never exited on its own
  force_exit_dte: 5      # at or below this, an exit is REQUIRED
```

Both are inclusive: "5 or fewer days" means 5 exits. The warning is a `WAIT`
carrying `EXPIRATION_WARNING`, and every other policy still applies — a warning
that exited would be a force-exit with a friendlier name.

Per-strategy narrowing in `config/strategies/*.yaml`:

```yaml
exit_policy:
  close_at_dte: 10       # must be >= the global force_exit_dte
```

**A larger `close_at_dte` is narrower**: it closes the position *earlier*. A
smaller one holds it closer to expiry than the global floor permits and fails to
load. The shipped values reflect the payoff — the directional strategies close
at 7 days, the straddle and strangle at 10, because long premium decays fastest
near expiry and a two-legged structure has twice as much of it to lose.

## The nearest expiration binds

`expiration_view` takes every leg's expiration and uses the **minimum**. Every
strategy shipped today has one expiration across its legs (`same_expiration` on
the structure), but a calendar spread would not, and whichever leg expires first
is when the structure stops being the structure that was authorised.

A leg reporting *no* expiration is recorded as `missing_expiration` and blocks
with `EXPIRATION_DATA_UNAVAILABLE`, rather than the DTE being computed quietly
from the legs that did report one.
