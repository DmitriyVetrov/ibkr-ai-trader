# Working on observability and operations (Milestone 11)

Development guidance for the milestone that makes the system *operable*. Read
[CLAUDE.md](../../CLAUDE.md) first; this is the detail behind the summary there.

Milestone 11 introduces **no agent**, so there is deliberately no
`.claude/agents/` entry for it — exactly as in Milestones 7, 8, 9 and 10. No
operational decision is made by a model: what a trade made is arithmetic over
confirmed fills, when a job runs is a cron expression, and whether capital may
return is a question about evidence.

## The shape of it

Three separable concerns, and keeping them separate is most of the design.

```
DOMAIN                                    OBSERVABILITY
  |                                            |
immutable artifacts  <-- ids only -->  spans / metrics / logs
  |                                            |
realised P&L                            OTLP -> Collector
  |                                            |
settlement                        Tempo / Prometheus / Loki -> Grafana
  |
capital returned                  OPERATIONS
  |                                  |
daily loss state              Scheduler -> existing service methods
                                     |
                              health / alerts / notifications
```

| package | answers |
|---|---|
| `pnl/` | what did this trade actually make, and may its capital come back? |
| `operations/` | what should run, when, and what does an operator need to know? |
| `observability/` | what happened, how long did it take — as a *side channel* |

## The one rule in `observability/`

> **Telemetry cannot change what the trading system does.**

If the collector, Tempo, Prometheus, Loki and Grafana are all down, the system
behaves identically. Every call is wrapped so a provider that raises is
indistinguishable from one that is switched off, and
`tests/observability/test_failure_isolation.py` asserts it in its strongest
available form: the same operation is run with telemetry off, recording, broken
and half-broken, and the **stored artifacts are compared byte for byte**.

Three consequences you have to preserve:

**The SDK lives in exactly one module.** `attributes`, `privacy`, `provider`,
`tracing`, `metrics`, `instrument`, `llm` and `logging` import nothing but the
standard library. Only `otel.py` imports `opentelemetry`, and only `runtime.py`
imports `otel`. That is not tidiness — the research agent, the exit engine, the
risk engine and the strategy selector all have boundary tests forbidding
`socket`, `urllib`, `http` and `requests` in their transitive import graphs, and
the OTLP exporter imports every one of them. Adding an SDK import anywhere else
breaks four suites at once, which is the point.

**Nothing reads telemetry.** There is no path from a span, a metric or a
telemetry status into a decision. `telemetry_enabled()` exists for the health
report and for tests; a trading branch on it would be a trading decision that
depends on an exporter.

**The exporter never blocks.** `block_on_full_queue: true` fails to load. A
queue that applied back-pressure would let a dead collector stall a monitoring
cycle, which is the same failure as an unbounded broker request and is refused
for the same reason.

### When you add a span

1. add the name to `OPERATION_NAMES` in `observability/attributes.py` — a test
   parses every `operation(...)` and `traced(...)` call in `src/` and fails on a
   name outside the list, so a typo cannot silently create a second operation
   nobody is querying;
2. use the `@traced` decorator rather than wrapping a method body. A diff that
   re-indents a risk or exit method to add an observability concern is a diff
   nobody can review for what actually changed;
3. put domain identifiers in the **span**, never in a metric label.

### When you add a metric

1. add the constant and put it in `METRIC_NAMES`;
2. think about the labels. `FORBIDDEN_LABELS` refuses every domain identifier at
   the point of recording, whatever `config/observability.yaml` says — but the
   guard only helps for names it knows. A new high-cardinality label goes in
   the list *and* in the configuration;
3. a dropped label does not drop the measurement. The signal is worth more than
   the label; what must not happen is the label reaching the backend.

### When you add an attribute

`privacy.py` drops anything whose name contains `password`, `secret`, `token`,
`api_key`, `credential`, `prompt`, `completion`, `portfolio`, `balance` or
`account_number`, masks anything shaped like an account number, truncates long
strings, and drops monetary values unless configuration permits them.

If your attribute is a legitimate count or identifier whose *name* collides —
`llm.input_tokens` contains "token", `trading.pnl.id` contains "pnl" — add the
exact name to `ALLOWED_EXACT_NAMES`. An allow-list of exact names rather than a
loosened pattern, so adding one is deliberate and reviewable.

## The one rule in `operations/`

> **The scheduler orchestrates. It contains no trading logic.**

Every job is one call to an already-tested service method. Whether a position
should close is Milestone 10's answer, how an exit order is sent is Milestone
8's, and what actually happened at the broker is Milestone 9's. A scheduler that
re-derived any of those would be a second, untested copy of a safety decision —
and the tested one would stop being the one that runs.

### When you add a job

1. write the function in `operations/jobs.py`. Its body should be a service
   call, an outcome, and nothing else;
2. register it in `JOB_BUILDERS`. A cadence in `config/schedules.yaml` naming a
   job with no implementation is a `KeyError` at scheduler construction, never a
   job that looks scheduled and never runs;
3. add the cadence to `config/schedules.yaml`, with `market_hours_only` and a
   `timeout_seconds` you have actually thought about;
4. **make it idempotent against persisted state.** The scheduler's duplicate
   protection is the stored `JobRun` for the scheduled instant, but a job that
   is re-run after an `UNKNOWN` must still move nothing. No process-local flag
   is protection;
5. if it can reach an order path, set `can_submit_orders=True` — a test asserts
   the set of jobs that can, and it is currently exactly one.

### The five statuses, and why none of them collapses

| status | means |
|---|---|
| `SUCCESS` | it ran and finished |
| `SKIPPED` | it deliberately did not run — disabled, closed market, nothing to do. **Not an error** |
| `FAILED` | it ran and raised. Something is wrong |
| `UNKNOWN` | it started and its completion was never recorded. **A question, not a failure** |
| `BLOCKED` | a safety condition refused to let it run |

`UNKNOWN` is the one to be careful with. It arises from a timeout and from a
process that died mid-run, and in both cases the work may still be in flight.
Python cannot kill a thread, so the timeout stops the *scheduler* waiting, not
the job — calling that `FAILED` would be a claim nobody can support, and would
invite a retry policy built on it.

### Restart

`recover()` runs first on `serve()`. A `RUNNING` record left by a dead process
becomes `UNKNOWN` — never `SUCCESS`, which would assume, and never `FAILED`,
which would also assume. Every job being idempotent is what makes re-running it
the way to establish the answer.

## The one rule in `pnl/`

> **Only a broker-confirmed fill contributes, and capital returns only on
> broker-confirmed closure.**

Not a limit price, not the reference price Milestone 7 authorised, not a
midpoint, not an estimate of what an exit ought to have made.

### `NOT_AVAILABLE` is a first-class result

A missing commission, an absent multiplier, a cross-currency pair, an
unresolved execution: each produces a result with **no figure attached at all**
and a reason code. The model refuses a `NOT_AVAILABLE` record that carries a
number, because the consumer that matters reads the number — and that consumer
is the daily loss limit.

### When you add a reason a result cannot be computed

1. add a member to `PnLReasonCode`;
2. add the branch to `PnLCalculator.compute`, in the refusal block *before* the
   arithmetic, and return through `_unavailable`;
3. add it to `schemas/realized_pnl.json`;
4. decide whether it should also block settlement. It usually should: a result
   nobody could compute means what came back is not a known quantity.

### The three daily states

| state | means |
|---|---|
| `TRACKED` | every closure today produced a usable figure |
| `UNKNOWN` | closures happened and at least one produced nothing. **Not zero loss** |
| `NOT_TRACKED` | no ledger was consulted. Also not zero loss, and a different fact |

`block_on_unknown_daily_loss` ships **true**: once the ledger exists, a figure
it could not produce is evidence that something is wrong.
`require_daily_loss_tracking` ships **false**, because a deployment that has
never closed a position should not be blocked by a limit it has no data for.

### Settlement

```
broker confirms none of the structure   proof the position ended
every execution resolved                proof nothing is still working
realised result computed                proof of what came back
reconciliation agrees                   proof the records are not disputed
    -> SETTLE (fully, or the matched fraction)
```

Three switches fail to load: `release_on_unknown: true`,
`require_broker_confirmed_closure: false`, and `currency.allow_conversion: true`.
Each would let capital move on something weaker than evidence.

**Settlement returns capital, not proceeds.** What comes back is what the
reservation consumed; the realised result is recorded next to it and, by
default, is *not* added to the spendable envelope — a system that grew its own
budget on a winning trade would be compounding without anybody deciding to.

**There is no second capital ledger.** Capital moves as an appended event on the
Milestone 9 reservation, folded on read exactly as every other reservation event
is. `settled_amount` is a *separate dimension* from consumed/released/remaining:
moving capital from `consumed` back to `released` on closure would erase the
only record that it was ever spent, and `released` already means something
different — capital that was never spent at all.

## What this milestone deliberately does not do

- **It does not decide anything.** No order, no sizing, no permission. The
  scheduler runs services that already made those decisions.
- **It does not adopt an orphan position.** `ORPHAN_BROKER_POSITION` is still
  reported and still stays exactly where it is.
- **It does not implement the separate thesis monitor.** Registered, disabled,
  and honest: `VALID / WEAKENING / INVALIDATED / UNKNOWN` needs a judgement no
  milestone has made, and the job records `SKIPPED / NOT_IMPLEMENTED` rather
  than fabricating a verdict.
- **It does not enable LIVE.** The scheduler refuses it, and the exit-submission
  job refuses it separately — the right number of refusals for the one
  irreversible action here.
- **It does not make telemetry required.** The stack is optional, ships
  disabled, and the SDK is an optional extra.

## Where to look

| question | file |
|---|---|
| what does a trade's result rest on? | `pnl/calculator.py` |
| when may capital come back? | `pnl/settlement.py` |
| what does the risk engine read? | `pnl/campaign_state.py` |
| what runs, and when? | `config/schedules.yaml`, `operations/scheduler.py` |
| what does a job actually call? | `operations/jobs.py` |
| what is healthy, and which health? | `operations/health.py` |
| what fires an alert? | `config/alerts.yaml`, `operations/alerts.py` |
| what may a span carry? | `observability/attributes.py`, `observability/privacy.py` |
| what may a metric label be? | `observability/metrics.py` |
| where does telemetry go? | `config/observability.yaml`, `deploy/otel/collector.yaml` |
