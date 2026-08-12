# LONG_CALL

Configuration: [`config/strategies/long_call.yaml`](../../config/strategies/long_call.yaml).
Structure: [`src/trading_system/strategies/long_call.py`](../../src/trading_system/strategies/long_call.py).
This document is a specification. It states no numbers; the files above do.

## Purpose

One bought call. It profits when the underlying rises far enough, soon enough,
to outrun the premium paid, and loses that premium when it does not. The trade
is an expression of *direction* plus *timing*, and both have to be right.

## Applicable hypotheses

`B` — predominantly up — and nothing else. This is the only shipped strategy
whose payoff requires a direction, so it is the only one a directional
hypothesis maps to.

The mapping is not written here: it is `applicable_hypotheses` in the
configuration file, and the registry derives the hypothesis-to-strategy table
from it.

## Required data

- an option chain visible at the research instant;
- a per-contract quote for the candidate: bid, ask and delta at minimum;
- a broker contract id and the trading class the broker reported;
- option-level volume and open interest.

Every one of these is listed in `required_option_fields` in the configuration.
A candidate missing any of them is rejected by name — never estimated, never
substituted from the underlying, never carried forward as a zero.

## Legs

One leg: **BUY CALL, ratio 1**. Asserted in code, so a configuration cannot
turn this into a spread by adding a second leg or selling the first.

## Expiration policy

The rule and the target come from `expiration_policy` in the configuration; the
effective window is the strategy's DTE range intersected with
`config/risk.yaml`'s. DTE is counted in calendar days from the exchange-local
date of the decision instant to the expiration date, using the market calendar
in `config/data.yaml`.

An expiration on a day the calendar says the market is closed is rejected. A
year the calendar does not cover answers "unknown" and is accepted, recorded as
unknown rather than assumed open.

## Strike policy

`TARGET_DELTA`: the listed strike whose delta is closest to the configured
target. Ties break on the lower strike, then the lower contract id, so the
choice is reproducible.

**If delta is unavailable, the selection is unavailable.** A delta is never
approximated from moneyness, from a pricing model or from memory. The candidate
is rejected as `MISSING_DELTA` and, if no candidate has one, the selection ends
as `REQUIRED_DATA_UNAVAILABLE`.

## Liquidity

Option-level only. That the underlying trades hundreds of millions of shares is
not evidence that this contract has a market. The floors are
`liquidity.min_open_interest` and `liquidity.min_daily_volume` in the
configuration, and they may only be tighter than `config/risk.yaml`'s.

Unknown liquidity is not zero and not acceptable by default:
`quotes.unknown_liquidity_policy` in `config/contract_selection.yaml` decides,
and it ships as `REJECT`.

## Invalidation

The research report's own invalidation conditions travel with the position and
are what the thesis monitor consumes. At this stage the strategy adds two
structural ones:

- the directional view no longer holds — a bullish thesis that has turned
  bearish invalidates a long call outright, not gradually;
- the time value the premium bought has been spent without the move arriving.

The exit rules that act on these — trailing stop, take profit, maximum loss,
close-at-DTE — are `exit_policy` in the configuration and are executed by a
later milestone.

## Prohibited behaviour

- Selling any leg. This is a long-premium strategy; there is no short version
  of it here.
- Selecting a strike or an expiration by anything other than the configured
  policy, including "the closest thing available" when the policy is not
  satisfiable.
- Reading a missing delta, bid, volume or open interest as zero.
- Deriving the trading class from the ticker. IBKR has been observed reporting
  SPY options under class `2SPY`; the class is copied from the chain.
- Deciding a quantity or an amount of money. Neither exists at this stage.

## Failure conditions

Each is a named, recorded outcome, never a fallback to an approximate contract:

| Condition | Result |
| --- | --- |
| no chain visible at the instant | `OPTION_CHAIN_UNAVAILABLE` |
| no expiration inside the effective DTE window | `NO_VALID_EXPIRATION` |
| no quote, no delta, or a missing required field | `REQUIRED_DATA_UNAVAILABLE` |
| no strike within the configured distance of the target | `NO_VALID_STRIKE` |
| every candidate below the liquidity floor, or unknown | `NO_VALID_STRIKE` with `LOW_OPTION_LIQUIDITY` / `OPTION_LIQUIDITY_UNKNOWN` rejections |
| a stored record that was not knowable at the instant | `POINT_IN_TIME_ERROR` |
