# Risk and allocation (Milestone 7)

Development guidance for the deterministic layer that decides **how much**.

This is the milestone where the system stops proposing and starts committing
capital. Everything upstream of it is a proposal; nothing downstream of it may
exceed what it authorised. Read this before changing anything under `risk/` or
`allocation/`.

## The rules, without exception

> **NEVER let AI determine quantity.**
> **NEVER let AI override risk.**
> **NEVER submit orders from Milestone 7.**
> **NEVER bypass deterministic risk checks.**
> **NEVER use broker calls inside `RiskEngine` or `AllocationEngine`.**
> **NEVER use floating point for money.**
> **NEVER invent missing prices.**
> **NEVER convert `UNKNOWN` into `TRUE`/`FALSE`.**

Each of these is enforced structurally, not by convention, and each has tests
that fail loudly. If a change makes one of them merely *unlikely* rather than
*impossible*, the change is wrong.

| Rule | How it is enforced | Test |
|---|---|---|
| No AI decides quantity or money | Neither engine has a parameter, field or import through which a model could speak | `tests/risk/test_engine.py::test_no_agent_can_override_a_rejection` |
| No AI overrides risk | `CampaignAllocation` refuses to be `APPROVED` over a `REJECTED` risk verdict — model *and* schema | `tests/contract/test_workflow_contracts.py` |
| No orders | Nothing under `risk/` or `allocation/` imports a broker or names an order API | `tests/risk/test_boundaries.py` |
| No broker in the engines | Account state arrives as a stored `AccountSnapshot`; only the CLI holds a broker | `tests/risk/test_boundaries.py::test_only_the_cli_turns_broker_state_into_an_account_snapshot` |
| Decimal money | `Money` rejects `float` outright; every calculation is `Decimal` | `tests/allocation/test_quantity.py` |
| No invented prices | `CandidatePrice` cannot be `available` without a figure, and refuses a figure when unavailable | `tests/risk/test_engine.py` |
| Unknown is not a pass | `RiskCheckOutcome.NOT_EVALUATED` is distinct from `PASS` | `tests/risk/test_engine.py::test_an_untracked_daily_loss_is_unevaluated_not_passed` |

## The two engines, and why they are two

```
RiskEngine.evaluate(candidate, campaign)  ->  "is this permitted?"
        |
AllocationEngine.allocate(candidates, campaign)  ->  "how many units?"
```

`RiskEngine` answers **permission**. It tests *one whole unit* against every
limit and can reject before a quantity is ever calculated. That is deliberate:
a refusal reported as "we computed a quantity of zero" tells nobody which limit
to look at, whereas `MAX_ALLOCATION_PER_TRADE_EXCEEDED` with an actual and a
limit value tells them exactly.

`AllocationEngine` answers **size**, and only for candidates the risk engine
already approved. It never overrules a rejection; a rejected candidate is
recorded as rejected and never sized.

Both are **pure functions of their arguments**. No clock, no network, no
broker, no repository, no random source. The decision instant is passed in.
This is what makes a stored verdict reproducible years later, and it is checked
by `tests/risk/test_boundaries.py`.

## Campaign is not account

The single most important distinction in this milestone:

```
IBKR paper account:   EUR 1,000,000    <- irrelevant to what may be spent
configured campaign:  EUR 5,000        <- the envelope
less a 20% reserve:   EUR 4,000        <- the most that may ever be committed
```

A large account balance can never widen the campaign. A small one *can* narrow
it — where the broker reports less available capital than the campaign
permits, the account wins. **The most restrictive relevant limit always wins.**

And the account's *currency* is not the campaign's either. The comparison above
only means anything once both sides are in one currency:

```
account balance     EUR 1,000,000   base currency, from the broker
campaign envelope   EUR 5,000       declared, budget_currency
      |
      |  x EUR/USD, captured with the balance
      v
what may be spent   USD 5,500       target_currency, and what a price is in
```

Both conversions use the same captured rate and both are recorded. Neither
happens without one: `FX_RATE_UNAVAILABLE` is the answer, and it is a rejection
rather than a smaller number.

## The limit hierarchy

```
config/risk.yaml           GLOBAL    the outer boundary of the whole system
      |
config/campaign.yaml       CAMPAIGN  what this campaign permits within it
      |
config/strategies/*.yaml   STRATEGY  what this strategy permits within that
      |
the candidate itself       POSITION  what one position may commit
```

A child may **narrow** a parent and may never **widen** one. Widening is a
*configuration load failure*, caught in `infrastructure/settings.py` when the
files are read. It is never clamped: a clamped limit runs correctly and is
invisible in the diff that introduced it, so someone reviewing `campaign.yaml`
would see a number that is not the number in force.

Every effective limit records which layer supplied it, in `RiskLimits.scopes`,
so a stored decision stays explainable after the configuration moves on.

## Quantity

```
quantity = floor(min(
    campaign budget remaining / unit cost,
    risk budget remaining     / unit max loss,
    per-trade allocation cap  / unit cost,
    concentration room        / unit cost,
    contract count cap,
    broker available funds    / unit cost,
))
```

`max_units()` computes an exact floor and then **verifies it by
multiplication** in both directions. That is fussier than it looks: a division
that rounded the wrong way in the last digit would produce a position one
contract larger than any limit authorised, every time, silently.

A quantity is always a whole number. There is no such thing as a third of an
option contract.

## Maximum loss

Read from the strategy's own `MaxLossBasis`, declared on its
`StrategyStructure` in code — **never** from a generic formula chosen by the
risk engine. "Max loss is the premium" is true of the four long-debit
strategies shipped today and false of the first credit spread anyone adds, and
an engine that assumed it would size that spread as though it could only lose
what it paid, which is exactly backwards.

A basis the engine cannot compute is `MAX_LOSS_UNDEFINED` — a rejection, not an
estimate. An unquantified loss is not a small one.

## When adding a new strategy

1. Set `max_loss_basis` on its `StrategyStructure`. If the loss is not bounded
   by the debit paid, use `NOT_DEFINED` and let the risk engine refuse it until
   a model exists for it. **Do not** default it to `NET_DEBIT_PAID` to make a
   test pass.
2. Add its suite under `tests/strategies/`.
3. Check that `directional_view` is right — it feeds the directional exposure
   limit, and a mislabelled structure quietly concentrates the book.

## When adding a new limit

1. Decide which layer *owns* it, and put it in exactly one file. Every safety
   limit has exactly one authoritative source.
2. If a lower layer may also declare it, add the narrowing check to
   `SystemConfig`, so widening fails to load.
3. Add it to `RiskLimits`, record its scope, and give it a check in
   `RiskEngine` with an `actual` and a `limit` value — the explanation is
   generated from those and is never written by hand.
4. If it constrains size as well as permission, add its ceiling to
   `QuantityCalculation` so "why this many" stays answerable from the record.
5. **Decide which currency it is in, and say so.** A *capital* limit is
   declared in `budget_currency` and belongs in `resolve_limits`' `declared`
   dict so it is converted with the others; an *instrument* limit (a price
   band, a spread) is already in the target currency and must never be
   converted. Getting this wrong is invisible: the limit still binds, just at
   the wrong number, and only against a real account at a real rate.

## What Milestone 7 hands to Milestone 8

An **authorisation**, which is not an order:

> `APPROVED`, campaign, underlying, strategy, legs, quantity, capital
> commitment, maximum loss, allocation id, decision timestamp, price reference,
> provenance.

There is no order type, no side, no limit price, no time-in-force and no broker
order id anywhere in it, and a test asserts their absence. Milestone 8 decides
*how to execute* that authorisation and stays bound by every figure in it.

## Known limitations to keep in mind

* **Realised daily profit and loss is not tracked yet.** The daily-loss limit
  is recorded as `NOT_EVALUATED`, never as passed. Milestone 9 supplies the
  figure; `campaign.account.require_daily_loss_tracking` then flips to `true`.
* **An authorisation that was never executed still consumes campaign budget.**
  Milestone 7 cannot know whether an order filled. Double-authorising the same
  capital is the failure worth preventing; releasing stale reservations belongs
  to the milestone that learns what happened to them.
* **Three currencies, and never fewer.** The campaign's capital is *declared*
  in `campaign.budget_currency` (EUR — what the operator holds) and *spent* in
  `campaign.currency_policy.target_currency` (USD — what a US-listed option is
  quoted in). Both engines work entirely in the target currency, because a
  single-currency comparison is the only kind they can get right.

  The conversion happens **once**, in `resolve_limits`, against every money
  limit at the same rate. Do not convert per comparison: two limits derived
  from one rate would disagree in the last digit depending on multiplication
  order. `RiskLimits.declared` keeps the source figures in their own currency,
  and `RiskLimits.limit_currency` says which currency the object's money fields
  are actually in — check `usable_against()` before comparing any of them with
  a price rather than assuming.

  The rate comes from `AccountSnapshot.fx_rates`, captured in the same broker
  read as the balance it converts. **Never fetch one here** — these packages
  hold no broker, and a rate from a different instant would let a replay reach
  a different verdict. Without a valid rate the answer is `FX_RATE_UNAVAILABLE`
  and nothing is authorised or sized; `fx/convert.py` has no input that makes
  two different currencies convert at 1.0, and adding one would undo the
  milestone.

  **An instrument price is never converted, in either direction.** A limit is
  compared and discarded; a price becomes the limit price on an order, and the
  exchange expects the contract's own currency. A contract quoted in something
  else is `CURRENCY_MISMATCH` with a fix an operator can act on. This is why
  `risk.yaml`'s `min_option_price`/`max_option_price` sit apart from everything
  under `capital_currency`: they are in the *target* currency and are not
  capital limits.
* **Correlation is not modelled.** Concentration rules are explicit and
  countable: per underlying, per strategy, per direction, plus a position
  count. Do not add a correlation matrix here; it would claim a precision
  nobody has measured.
