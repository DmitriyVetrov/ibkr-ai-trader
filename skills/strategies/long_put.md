# LONG_PUT

Configuration: [`config/strategies/long_put.yaml`](../../config/strategies/long_put.yaml).
Structure: [`src/trading_system/strategies/long_put.py`](../../src/trading_system/strategies/long_put.py).
This document is a specification. It states no numbers; the files above do.

## Purpose

One bought put. It profits when the underlying falls far enough, soon enough,
to outrun the premium paid, and loses that premium when it does not. The mirror
of the long call, and subject to the same requirement: direction *and* timing.

## Applicable hypotheses

`C` — predominantly down — and nothing else. The mapping lives in
`applicable_hypotheses` in the configuration file, not here.

A long put chosen for a bullish outlook is not an unusual view worth
preserving; it is a contradiction, and the deterministic validator rejects the
decision rather than recording it.

## Required data

- an option chain visible at the research instant;
- a per-contract quote for the candidate: bid, ask and delta at minimum;
- a broker contract id and the trading class the broker reported;
- option-level volume and open interest.

`required_option_fields` in the configuration is the authoritative list.

## Legs

One leg: **BUY PUT, ratio 1**. Asserted in code.

## Expiration policy

As for the long call: the configured rule and target, inside the strategy's DTE
window intersected with `config/risk.yaml`'s, counted in calendar days from the
exchange-local date of the decision instant using the market calendar.

## Strike policy

`TARGET_DELTA`. **Put deltas run from 0 to -1**, so the configured target is
negative, and the configuration is rejected at load if a put leg targets a
positive delta or a call leg a negative one. The sign is validated, never
inferred and never silently flipped.

If delta is unavailable the selection is unavailable — the same rule, and for
the same reason, as the long call.

## Liquidity

Option-level only, at the floors in the configuration, never below
`config/risk.yaml`'s. Unknown liquidity is decided by
`quotes.unknown_liquidity_policy`, which ships as `REJECT`.

## Invalidation

- the bearish view no longer holds;
- the premium's time value has been spent without the move arriving.

The research report's own invalidation conditions travel with the position and
are what the thesis monitor consumes.

## Prohibited behaviour

- Selling any leg.
- Treating a put as a hedge for a position this system does not know about.
  Nothing at this stage knows the portfolio, and nothing here may reason as
  though it did.
- Selecting a strike or expiration by anything other than the configured
  policy.
- Reading a missing delta, bid, volume or open interest as zero.
- Deriving the trading class from the ticker.
- Deciding a quantity or an amount of money.

## Failure conditions

The same named outcomes as the long call: `OPTION_CHAIN_UNAVAILABLE`,
`NO_VALID_EXPIRATION`, `REQUIRED_DATA_UNAVAILABLE`, `NO_VALID_STRIKE`,
`POINT_IN_TIME_ERROR`. Each is recorded with the candidates it rejected and
why. No approximate contract is ever returned in place of one that satisfies
the policy.
