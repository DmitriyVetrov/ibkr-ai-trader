# Working on orphan-position cleanup

Development guidance for the controlled closure of pre-existing broker
holdings. Read [CLAUDE.md](../../CLAUDE.md) first; this is the detail behind
the summary there.

This introduces **no agent**, so there is deliberately no `.claude/agents/`
entry for it — exactly as in Milestones 7 through 12. One would imply a model
is involved somewhere, and none is. Whether to sell a holding an operator
explicitly named is not a judgement; it is a sequence of checks.

## The problem it solves

Reconciliation reports an `ORPHAN_BROKER_POSITION` when the broker holds
something no execution of ours accounts for. It never adopts it, never sells
it and never assigns it to a campaign — and it is right not to. That leaves a
real account in a state only a person can resolve, and leaves the M12 readiness
gate at `NOT_READY` because reconciliation is `MISMATCH`.

The resolution is not "make reconciliation stop reporting it". It is: close the
holding deliberately, and keep the whole thing auditable.

```
external / pre-existing broker position
      |
observed as ORPHAN_BROKER_POSITION       reconciliation, unmodified
      |
explicitly authorised cleanup            an operator, a specific report,
      |                                  specific broker contract ids
cleanup execution                        ExecutionService, the ONE order path
      |
broker confirmation                      a POSITION read, not a fill report
      |
reconciliation history records it
```

## The shape of it

| module | answers | pure? |
|---|---|---|
| `models.py` | what a target, an authorisation and a result *are* | yes |
| `targets.py` | which holdings may be touched | yes |
| `gates.py` | may this run submit anything at all | yes |
| `store.py` | the immutable record | no (disk) |
| `service.py` | the composition root | no (services) |
| `report.py` | what an operator reads | yes |

`service.py` is the only module here that transitively reaches the broker
library, and only because it calls
`ExecutionService.submit_cleanup` — which owns the system's one writable broker
constructor. `tests/cleanup/test_boundaries.py` pins that.

## Adding something

### A new refusal

Two places, and picking the right one matters:

* **`targets.py`** if it is about *whether this holding is a legitimate
  target* — its identity, its quantity, whether the report still describes it.
  Add a `CleanupCandidate` with `accepted=False` and a reason a person can act
  on. Never drop the candidate: a shorter list must not read as a tidier
  account.
* **`gates.py`** if it is about *whether this run may submit* — mode, guards,
  configuration, freshness, the connected account. Add a `_gate(...)` and
  return it from `evaluate_run_gates` or `evaluate_target_gates`. Every gate is
  evaluated even after one fails, so an operator sees the whole picture.

If it needs a new machine-readable code, add it to `ExecutionReasonCode` — that
vocabulary answers *what happened when we tried to send it?*, which is exactly
this question — and to the three execution schemas that enumerate it.

### A new configuration value

`config/cleanup.yaml` plus a field on `CleanupConfig`. Config models are
`extra="forbid"`, so a typo fails loudly.

If the value could make the operation *less* safe when set, it must **fail to
load**, not be clamped. Five already do:
`allow_live`, `paper_only: false`, `require_explicit_authorization: false`,
`require_orphan_finding: false`, `allow_short_positions`,
`allow_partial_continuation`. A clamped safety value is a safety value nobody
can see.

### A new outcome status

`CleanupOutcomeStatus`. Before adding one, check it is genuinely a different
*fact* rather than a different shade of an existing one. The set is already
shaped around distinctions that cost money to collapse:

* `REFUSED` — nothing left this process;
* `REJECTED` — the broker received it and turned it down (an attempt counted);
* `UNCERTAIN` — something was sent and the outcome was never learned;
* `WORKING` — an order exists and the account still holds the position;
* `PARTIALLY_CLOSED` — the *position read* shows less than before;
* `CLOSED` — the position read shows none.

`CLOSED` requires an `observed_quantity_after`, enforced by a validator. A fill
report is a claim about an order; only a position read is a claim about the
account.

## What this deliberately does not do

* **Adopt.** No allocation, purchase card, risk decision, opportunity,
  strategy, research report, expected position or strategy position is created.
  `ExecutionIntent.CLEANUP` makes it structural: `ExecutionRecord` *refuses* to
  be constructed carrying any of them.
* **Move campaign money.** No reservation, no budget, no realised profit or
  loss. Each of the three ledgers excludes `CLEANUP` itself, so this package
  does not have to remember to.
* **Invent a structure.** Two holdings that look like a straddle may or may not
  have been bought as one. There is no combo path here at all — one holding,
  one order. Every orphan being *long* is what makes that safe: closing one leg
  of an invented pair cannot leave a short.
* **Retry, reprice or continue.** A rejection is reported. An `UNKNOWN` blocks
  everything about that holding permanently and is resolved by observing the
  broker. A partial fill is reported and the remainder left.
* **Repair.** Nothing edits a reconciliation, a finding, an execution or a
  position into agreement. The original `MISMATCH` stays exactly as it was.
* **Touch a short holding.** Closing one is a purchase whose cost is unbounded
  above. Reported, and left where it is.

## The four switches

No two are the same decision, and all four are required:

1. `cleanup.enabled` in `config/cleanup.yaml` — ships `false`;
2. `execution.enabled` in `config/execution.yaml` — ships `false`;
3. `--confirm` on the command;
4. `TRADING_MODE=PAPER`, both live guards off, and the **connected account**
   proving it is a paper account — the account the broker reported, never the
   one in the configuration. Comparing the configuration against itself proves
   nothing.

Without `--confirm` the code path never reaches the method that can construct a
writable broker, so "a review cannot place an order" is a property of the graph
rather than a check someone has to get right. `tests/cleanup/test_service.py::
test_a_review_constructs_no_writable_broker` asserts the factory was **never
called**, which is a much stronger claim than "the broker refused".

## Idempotency, in three layers

1. **Broker observation.** The holding is gone, so nothing is targetable and
   the run is `NO_TARGETS`. The ordinary second-run answer.
2. **The execution ledger.** `cleanup_execution_request_identifier` excludes
   the clock *and the quantity*, so a re-run lands on the same identity and any
   earlier attempt that reached the broker — **including a filled one** —
   refuses a second.
3. **The working-order gate.** An order at the broker for this contract blocks
   it, whoever sent it.

Each alone would be enough for the common case; together they cover the ones
where the others are blind.

## Running it

```bash
# Review. Reads the broker, evaluates every gate, builds every order, sends
# nothing, stores nothing, and constructs no writable broker.
python -m trading_system.cli reconciliation cleanup-orphans

# One holding only.
python -m trading_system.cli reconciliation cleanup-orphans --contract-id 848575117

# SUBMITS. Needs cleanup.enabled and execution.enabled as well.
python -m trading_system.cli reconciliation cleanup-orphans --confirm
```

Against a real gateway the CLI must run inside the gateway's network namespace
— the image trusts only `127.0.0.1`:

```bash
docker compose run --rm trading-runtime reconciliation cleanup-orphans
```

## Tests

```bash
pytest tests/cleanup                            # targets, gates, order, records, service, CLI
pytest tests/cleanup/test_boundaries.py         # one order path, and it is not here
pytest tests/integration/test_orphan_cleanup.py # the whole loop, simulated

# Read-only against a live gateway. Submits nothing.
ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true \
  pytest tests/integration/test_paper_orphan_cleanup.py -m paper_execution -s

# SELLS REAL HOLDINGS. Three variables, deliberately: unlocking the suite to
# buy one contract does not authorise liquidating what the account already had.
ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true \
  RUN_ORPHAN_CLEANUP_PAPER_TEST=true IBKR_READ_ONLY=false \
  pytest tests/integration/test_paper_orphan_cleanup.py -m paper_execution -s
```
