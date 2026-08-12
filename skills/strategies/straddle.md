# LONG_STRADDLE

Configuration: [`config/strategies/long_straddle.yaml`](../../config/strategies/long_straddle.yaml).
Structure: [`src/trading_system/strategies/long_straddle.py`](../../src/trading_system/strategies/long_straddle.py).
This document is a specification. It states no numbers; the files above do.

## Purpose

One bought call and one bought put, on the same underlying, the same expiration
and the **same strike**. It profits from a large move in either direction and
loses when the move is too small to cover two premiums. It expresses a view
about *magnitude*, not about direction.

## Applicable hypotheses

`A` — a large move is likely and no specific catalyst is required — and `D` —
a sharp move around a specific identified event. Both are direction-uncertain
claims, which is what a straddle is for.

The difference between them decides the *expiration*, not the structure: `D`
has a dated event to align to and `A` does not. That is why the shipped
expiration policy is event-aligned with a target-DTE fallback rather than two
separate strategies.

## Required data

- an option chain visible at the research instant;
- per-contract quotes for both legs: bid and ask at minimum;
- a broker contract id and trading class for each leg;
- option-level volume and open interest for each leg;
- a reference price for the underlying, from the configured fields.

Without a reference price there is no at-the-money strike to identify, and the
selection fails rather than assuming one.

## Legs

Two legs: **BUY CALL x1** and **BUY PUT x1**, asserted in code, with the
relationship `SAME` — one shared strike.

The strike is chosen **jointly**, not per leg: the selector takes the listed
strike nearest the target at which *both* legs have a usable contract.
Selecting each leg independently and hoping the strikes matched would fail
whenever one leg's best strike happened to be illiquid, even though a perfectly
good shared strike sat one step away. A straddle is one position, so its strike
is one decision.

## Expiration policy

`EVENT_ALIGNED` with a target-DTE fallback, per the configuration:

- when the research report names an event inside the horizon, the expiration is
  the first listed one on or after that event, within the configured window
  after it;
- when it names none — the ordinary `A` case — the target DTE applies, and the
  record says the fallback was used.

**The event date comes from the structured research report and nowhere else.**
It is never parsed out of prose, never recalled and never estimated. An
event-aligned rule with no event is a fallback, not an invention.

## Strike policy

`ATM` for both legs: the listed strike nearest the reference price, shared. A
chain too coarse to place the target within the configured distance is a
rejection, not a rounding.

## Liquidity

Option-level, per leg, at the strategy's floors — which are tighter than the
directional strategies' by design, because a two-legged position pays two
spreads on the way in and two on the way out.

Underlying volume is not evidence about either leg.

## Invalidation

- the expected move happens but too slowly, so time decay outruns it;
- implied volatility collapses after entry, which loses money even when the
  move eventually arrives;
- for the `D` case, the event is postponed beyond the expiration — the catalyst
  the position was timed around no longer falls inside its life.

## Prohibited behaviour

- Selling either leg.
- Legging in or out. This is one position; `allow_independent_leg_exit` is
  false, and the trailing stop applies to the combined structure.
- Accepting two different strikes. Legs that resolve differently are rejected
  as `INCOMPATIBLE_LEG`; a "nearly straddle" is a different position with
  different breakevens.
- Accepting two different expirations, multipliers or trading classes.
- Inventing an event date, or aligning an expiration to an event the research
  report did not carry.
- Deciding a quantity or an amount of money.

## Failure conditions

| Condition | Result |
| --- | --- |
| no chain visible at the instant | `OPTION_CHAIN_UNAVAILABLE` |
| no expiration inside the effective DTE window | `NO_VALID_EXPIRATION` |
| no reference price for the at-the-money target | `REQUIRED_DATA_UNAVAILABLE` |
| no single strike usable by both legs | `NO_VALID_STRIKE` |
| legs that cannot be combined | `NO_VALID_CONTRACT` with `INCOMPATIBLE_LEG` rejections |
| a stored record that was not knowable at the instant | `POINT_IN_TIME_ERROR` |

A multi-leg strategy is accepted whole or not at all. One leg that does not fit
invalidates the structure, and a partially filled straddle is a directional
position nobody chose.
