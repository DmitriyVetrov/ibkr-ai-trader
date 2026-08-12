# Execution (Milestone 8)

Development guidance for the only layer that can send an order.

Everything upstream of this proposes, validates, authorises or sizes. This is
where those become an instruction that reaches a broker and cannot be taken
back. Read this before changing anything under `execution/`, `broker/base.py`,
`broker/factory.py` or `broker/ibkr/order_translation.py`.

## The rules, without exception

> **NEVER submit without both switches** — `execution.enabled` *and* an explicit
> authorisation.
> **NEVER retry an ambiguous submission.**
> **NEVER infer a fill.**
> **NEVER recompute a quantity, a price or a limit that Milestone 7 decided.**
> **NEVER re-derive a broker contract id.**
> **NEVER submit a multi-leg structure as independent orders.**
> **NEVER let a dry run construct a broker.**
> **NEVER use floating point for money.**

Each is enforced structurally, not by convention, and each has tests that fail
loudly. If a change makes one of them merely *unlikely* rather than
*impossible*, the change is wrong.

| Rule | How it is enforced | Test |
|---|---|---|
| Two switches to submit | `ExecutionRequest` cannot be built with `execution_authorized=False`; `require_explicit_authorization: false` fails to load | `tests/execution/test_service.py` |
| No retry | No code path from `UNKNOWN` to a second submission; `auto_retry_on_timeout: true` fails to load | `tests/execution/test_timeout.py` |
| No inferred fill | `filled_quantity` moves only from a broker report; `FILLED` with a short fill fails to construct | `tests/execution/test_partial_fill.py` |
| Nothing recomputed | Every figure is copied from the authorisation; the service names no engine | `tests/execution/test_service.py::test_the_service_never_recalculates_a_quantity` |
| Contract ids are copied | A leg without one is `CONTRACT_ID_MISSING`; the schema requires it | `tests/execution/test_purchase_card.py` |
| One structure, one order | `allow_independent_leg_orders: true` fails to load; combos go as BAG | `tests/execution/test_multi_leg.py` |
| A dry run builds no broker | The factory is never reached; a monkeypatched exploding factory proves it | `tests/execution/test_dry_run.py` |
| Only execution can submit | `build_broker` always returns read-only; upstream cannot import `broker/` | `tests/execution/test_boundaries.py` |

## The three answers that matter

After any submission attempt, exactly one of these is true, and conflating any
two of them is how a system places a duplicate order:

```
FAILED     the attempt provably did not leave this process.
           Read-only broker, disconnected broker, or our own translation
           refused. Nothing was sent. A later attempt is safe.

SUBMITTED  the broker answered. Whatever it said is recorded verbatim.
           (and the fill states)

UNKNOWN    we sent something and never learned the outcome.
           NOT a failure, and NOT a reason to retry. Resolved by asking the
           broker what it has.
```

`ExecutionEngine._preflight` is the only place that may produce `FAILED`,
because it checks conditions that are decidable *without touching the wire*.
Anything that happens after the attempt begins is `UNKNOWN`. If you add a new
failure mode, ask one question: *could the order have reached the broker?* If
the answer is "possibly", it is `UNKNOWN`.

## Units: the mistake worth guarding against

Milestone 7 records the cost of a structure as **money**:

```
unit_cost = sum over legs of (ask x multiplier x ratio)     # e.g. 605.00
```

A broker limit price is a **quote**, per multiplier unit:

```
limit_price = unit_cost / multiplier                        # e.g. 6.05
```

Sending `605` where `6.05` was meant is a hundredfold overpayment, and every
downstream number would reproduce it faithfully. The conversion happens
**exactly once**, in `order_builder.limit_price_from_reference`, against a
multiplier the validator has already proved every leg shares. The record keeps
the two apart by name — `reference_price` (structure money) versus
`reference_quote` / `submitted_price` (quoted terms) — rather than by comment.

Rounding is always **down**, so it can only ever bid below what was authorised.
An unfilled order is recoverable; an overspend is not.

## Adding a failure mode

1. add a member to `ExecutionReasonCode` in `domain/enums.py` — extend, never
   fork, and never reuse a `RiskReasonCode`: risk answers *may we trade this?*
   and execution answers *what happened when we tried to send it?*;
2. add it to the `execution_reason_code` enum in **both**
   `schemas/execution_record.json` and `schemas/execution_event.json`, and to
   the copy embedded in `schemas/execution_run.json`
   (`tests/execution/test_schemas.py` asserts they stay in step);
3. raise or record it from the layer that can actually detect it — a check that
   needs a broker belongs in the engine, everything else in `validation.py`;
4. add a test that asserts the **named code**, not merely that something was
   refused. "It said no" is not a diagnosis.

## Adding an order type

Think twice. The vocabulary is `LIMIT` and nothing else, and that is a
deliberate constraint rather than an unfinished list: a market order on an
option is an unbounded price, and Milestone 7 authorised a specific amount of
capital against a specific quoted cost. `permitted_order_types` refuses
`MARKET` at load time.

If a later milestone genuinely needs another type:

1. add it to `permitted_order_types` in `config/execution.yaml` **and** relax
   the validator that refuses it, so the widening is visible in a diff;
2. add its code to `_ORDER_TYPE_CODES` in `broker/ibkr/order_translation.py`;
3. state what bounds the price. If nothing does, that is the answer to whether
   it belongs here.

## Adding a strategy structure

`_COMBO_STRATEGIES` in `order_translation.py` lists the multi-leg structures the
combo builder is written and tested for. A structure absent from it is
`MULTI_LEG_UNSUPPORTED` — a refusal, never an approximation assembled from
unrelated single-leg orders.

A combo with mixed leg directions is refused outright. The net-price sign
convention for one is not exercised by any shipped strategy, and an untested
sign is a credit order where a debit was meant.

## What Milestone 8 deliberately does not do

- **No exits.** No trailing stops, no take profits, no thesis exits.
  Cancellation exists only to close a submitted order's lifecycle. Exit policy
  is Milestone 9.
- **No repricing.** Execution does not chase the market. A trade that no longer
  fits needs a *new* Milestone 7 authorisation, sized against prices that
  actually exist.
- **No price fetching.** It never reads a quote before submitting: a second
  uncached round trip on the submission's connection is exactly what Milestone 2
  found to be unreliable. Drift is checked only against a price a caller
  supplies explicitly.
- **No reservation release.** An authorisation that never executed still
  consumes campaign budget. Milestone 8 records submission, not settlement, so
  releasing stale reservations belongs to the milestone that can tell.
- **No model.** There is no LLM client, prompt or agent in the import graph, and
  a test asserts it transitively. Execution translates an already-approved
  deterministic artifact; there is nothing in that for a model to decide — which
  is why there is no `.claude/agents/` entry for this milestone.

## Before touching the paper validation

`tests/integration/test_paper_execution.py` submits a **real order**. It is
gated on `ALLOW_LIVE_TESTS=true` *and* `RUN_PAPER_EXECUTION_TESTS=true`, and
refuses any mode but PAPER. Keep both gates: unlocking the gateway for a
read-only diagnostic must not also authorise an order.

It opens one short-lived connection per operation — submit, query, cancel,
confirm — because only the first uncached round trip on a TWS connection is
reliably answered. Do not "optimise" those into one session.

Its order is priced far from anything that could trade, and it still handles a
fill. "Extremely unlikely to fill" is not "guaranteed not to fill", and a test
that assumed otherwise would leave a real paper position nobody knew about.
