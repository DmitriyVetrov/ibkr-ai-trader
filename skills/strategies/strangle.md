# LONG_STRANGLE

Configuration: [`config/strategies/long_strangle.yaml`](../../config/strategies/long_strangle.yaml).
Structure: [`src/trading_system/strategies/long_strangle.py`](../../src/trading_system/strategies/long_strangle.py).
This document is a specification. It states no numbers; the files above do.

## Purpose

One bought out-of-the-money call and one bought out-of-the-money put, on the
same underlying and the same expiration, with **different strikes** either side
of the reference price. The same claim as a straddle — a large move, direction
unknown — expressed more cheaply, and needing a larger move to pay.

## Applicable hypotheses

`A` and `D`, exactly as the straddle. Which of the two structures better
expresses a given outlook is the strategy agent's judgement: a strangle is the
cheaper expression and the more demanding one, so it suits a conviction that
the move will be *large*, not merely that it will happen.

## Required data

- an option chain visible at the research instant;
- per-contract quotes for both legs: bid and ask at minimum;
- a broker contract id and trading class for each leg;
- option-level volume and open interest for each leg;
- a reference price for the underlying — without it there is no "out of the
  money" to be on the correct side of.

## Legs

Two legs: **BUY CALL x1** and **BUY PUT x1**, asserted in code, with the
relationship `CALL_ABOVE_PUT`.

Each leg is chosen independently against its own target, and the relationship
is then enforced. A chain coarse enough that both offsets round to the same
strike does not produce a cheap strangle — it produces a straddle, and
returning that under this name would misdescribe the position, its cost and its
breakevens to every later stage. The selection is rejected instead.

## Expiration policy

`EVENT_ALIGNED` with a target-DTE fallback, per the configuration, and
identical in behaviour to the straddle's: the event comes from the structured
research report or the fallback applies and says so.

## Strike policy

`OTM_PERCENT` for both legs. One configured offset serves both, because the
direction comes from the right:

- the call targets the reference price raised by the offset, and must sit at or
  above the reference;
- the put targets it lowered by the offset, and must sit at or below.

A candidate on the wrong side of the reference is rejected as
`STRIKE_POLICY_NOT_SATISFIED` — an "out-of-the-money call" below spot is an
in-the-money call, whatever the arithmetic rounded to.

## Liquidity

Option-level, per leg, at the strategy's floors. Out-of-the-money contracts are
where thin option markets show up first, which is why the floors here are
tighter than the directional strategies' and why unknown liquidity is rejected
by default rather than assumed adequate.

## Invalidation

- the move arrives but does not reach either strike, so both legs expire
  worthless despite the thesis being directionally right about volatility;
- implied volatility collapses after entry;
- for the `D` case, the event is postponed beyond the expiration.

## Prohibited behaviour

- Selling either leg.
- Legging in or out: one position, one exit, `allow_independent_leg_exit`
  false.
- Accepting a call strike at or below the put strike.
- Accepting a leg on the wrong side of the reference price.
- Accepting two different expirations, multipliers or trading classes.
- Inventing an event date.
- Deciding a quantity or an amount of money.

## Failure conditions

| Condition | Result |
| --- | --- |
| no chain visible at the instant | `OPTION_CHAIN_UNAVAILABLE` |
| no expiration inside the effective DTE window | `NO_VALID_EXPIRATION` |
| no reference price for the offset targets | `REQUIRED_DATA_UNAVAILABLE` |
| no strike within the configured distance of either target | `NO_VALID_STRIKE` |
| the two legs resolve to the same strike, or the call below the put | `NO_VALID_CONTRACT` with `INCOMPATIBLE_LEG` rejections |
| a stored record that was not knowable at the instant | `POINT_IN_TIME_ERROR` |
