# Working on positions, reservations and reconciliation (Milestone 9)

Development guidance for the milestone that closes the loop. Read
[CLAUDE.md](../../CLAUDE.md) first; this is the detail behind the summary there.

Milestone 9 introduces **no agent**, so there is deliberately no
`.claude/agents/` entry for it — exactly as in Milestones 7 and 8. One would
imply a model is involved somewhere, and none is. Comparing two ledgers is
arithmetic.

## The shape of it

```
Broker (read-only, ONE short-lived connection)
      |
account + positions + open orders + fills     all startup-cache backed
      |
BrokerPositionSnapshot    what the broker says it holds
recorded fills            deduplicated on the broker's own execution ids
      |
ExpectedPosition          what CONFIRMED FILLS say should exist
StrategyPosition          the logical structure, leg by leg
      |
resolve UNKNOWN executions from broker evidence
      |
reservation lifecycle     consume / release / hold, on proof only
      |
ReconciliationEngine      deterministic, pure
      |
immutable ReconciliationResult + events
```

Three packages, and the split matters:

| package | answers | holds a broker? |
|---|---|---|
| `positions/` | what is held, and what we believe is held | yes, read-only, one per capture |
| `reservations/` | what our capital is doing | **no** — a test asserts it |
| `reconciliation/` | where the two disagree | only through `positions/` |

`portfolio/` in specification §3 is this, under a different name: it ships as
`positions/` so the package, the CLI group and the test suite share one name —
the same choice `strategy_selector.md` makes against the specification's
`options_strategist.md`. `portfolio/pnl.py` remains unbuilt; profit and loss
attribution is Milestone 11.

## The distinctions everything rests on

Four pairs. Each looks like one thing and is two, and every one of them has a
test that fails loudly:

**BROKER OBSERVED vs INTERNAL EXPECTED.** One is a fact about the account, the
other is a belief derived from confirmed fills. They are separate models,
separate stores and separately labelled in every CLI rendering. Merging them
would destroy the only evidence they ever disagreed, which is the entire value
of this stage.

**"We could not look" vs "there is nothing there".** A failed broker read is
`BROKER_DATA_UNAVAILABLE` and produces *no comparison at all*. An empty
portfolio the broker actually reported is `BROKER_RETURNED_EMPTY` and is a
valid answer. `BrokerReadStatus` has separate members, `build_position_snapshot`
and `unavailable_snapshot` are separate constructors, and the model refuses to
let either wear the other's shape. Reconciling against an unreadable broker
would report every real holding as gone, with total confidence.

**RESERVED vs INVESTED.** Capital is committed by an authorisation and spent by
a fill. Only the second creates a position.

**UNKNOWN vs FAILED.** Milestone 8's invariant, carried forward everywhere.
`FAILED` means the attempt provably never left the process, so an order at the
broker for one is `FAILED_EXECUTION_HAS_BROKER_ORDER` — a critical consistency
violation, never a reason to relabel the execution. `UNKNOWN` means we may have
sent something; its capital stays locked until the broker settles it.

## What to do when...

### ...you add a finding type

1. Add the member to `ReconciliationFindingType` in `domain/enums.py`.
2. Add its severity to `config/reconciliation.yaml`. **The configuration fails
   to load without it** — every finding type must have a stated severity,
   because defaulting an unlisted one would make the most dangerous finding the
   easiest to miss.
3. Add it to `_M1_DISCREPANCY` in `reconciliation/models.py` if it describes a
   disagreement, or to `AGREEMENT_FINDINGS` in `domain/enums.py` if it does not.
   An agreement has no Milestone 1 shape and `to_discrepancy()` raises for one.
4. Add it to `schemas/reconciliation_result.json`'s `finding_type` enum.
5. Build it through `make_finding` so its severity comes from configuration
   rather than from your comparison function.

### ...you add a reservation outcome

The rule is one question: **is there proof the capital was not spent?** If the
answer is anything short of yes, the capital stays committed.

1. Add the reason to `ReservationReasonCode` and the schema's enum.
2. Handle it in `_released_target` in `reservations/lifecycle.py`, gated on a
   named policy switch in `config/reconciliation.yaml`.
3. Return **deltas**, never totals. Applying an outcome twice must move
   nothing; that is what makes reconciliation economically idempotent.
4. Add a test to `tests/reservations/test_invariants.py` — the accounting
   identity has to survive your new path.

Do not add a switch that releases an `UNKNOWN`. `release_on_unknown: true`
fails to load, `reservations release` refuses it, and the lifecycle refuses it
where *any* attempt against the authorisation is unresolved. Three refusals is
the right number for the one thing here that can fund the same trade twice.

### ...you change how a position is identified

`contract_key` prefers the broker's contract id and falls back to
symbol/expiry/strike/right only when there is none — recording which happened
in `identified_by_contract_id`. Adjusted option contracts share their
human-readable fields, so keying on those alone eventually merges two different
instruments. `positions.prefer_broker_contract_id: false` fails to load.

An option *fill* with neither a contract id nor supplied contract terms is
refused outright (`FillTranslationError`): it would key on `NVDA|OPTION` and
merge every strike into one position, and a wrong position is worse than a
missing one because nothing looks broken.

### ...you touch the broker read

One connection, four reads, no health probe. Account summary, positions, open
orders and fills are all served from `ib_async`'s startup handshake cache, so
one connection answers all four without a second uncached round trip — the
constraint Milestone 2 measured. Anything that needs a *fresh* request needs
its own short-lived connection.

`tests/positions/test_snapshot.py::test_only_the_four_cache_backed_reads_are_issued`
asserts the exact call list. If you add a fifth read, you are adding a round
trip that may never be answered.

### ...you are tempted to fix a discrepancy

Don't. Reconciliation reports. `corrective_orders_permitted: true` and
`auto_adopt_orphan_positions: true` both fail to load, `ReconciliationResult`
refuses to be constructed with a non-zero `orders_submitted` or
`corrective_orders`, and `tests/positions/test_boundaries.py` proves the
writable broker constructor is unreachable from all three packages.

An orphan broker position is *real* and stays exactly where it is, with
acquisition provenance `UNKNOWN`. The first reconciliation of an account that
traded before this system existed is expected to report several; that is the
correct outcome, not a bug to tune away. Adopting one would mean inventing an
allocation, an execution, a strategy and a research thesis for a holding this
system knows nothing about.

## Units, again

The factor-of-100 error Milestone 8 warned about lives here too, in a new
place:

| figure | units | example |
|---|---|---|
| `ObservedFill.price`, `ExpectedPosition.average_price` | broker quote | `6.05` |
| `ExpectedPosition.average_cost`, `ObservedPosition.average_cost` | money per contract | `605.00` |
| `market_value`, `total_cost`, reservation amounts | money | `1210.00` |

`ExecutionRecord.executed_capital` is in **quoted** terms. Reservations must
never use it as money — `reservations/lifecycle.py::executed_capital` does the
multiplication once, explicitly, against a multiplier the Milestone 8 validator
already proved every leg shares. A reservation consuming 12.10 where 1,210.00
was meant leaves the campaign believing it has almost its whole budget left.

A conversion that needs a multiplier nobody reported yields `None`. Never 100.

## What this milestone deliberately does not do

* **No exits.** No trailing stop, no take profit, no time-to-expiration policy,
  no exit engine. A position is observed, not managed.
* **No thesis monitoring.** `ThesisStatus` stays `UNKNOWN` on every projected
  position snapshot; the thesis monitor is a separate agent and a separate
  question.
* **No corrective trading, ever.** Not behind a flag, not behind a confirm.
* **No adoption of orphan holdings.** A controlled onboarding workflow is a
  later milestone's job.
* **No profit and loss methodology.** Broker-reported realised and unrealised
  figures are preserved; nothing is derived from incomplete internal history.
* **No FX.** A fill in a currency the campaign does not hold is
  `CURRENCY_MISMATCH`, reported and left alone. Milestone 7's refusal,
  preserved rather than undone.

## Account numbers

Every Milestone 9 artifact stores a **masked** account reference
(`mask_account`, `positions.account_mask_visible_characters`), and
`tests/positions/test_models.py` asserts the full number never reaches a stored
payload. The one exception is the Milestone 7 `AccountSnapshot`, which stores
the broker's own account id because that is a completed milestone's stored
contract; the CLI masks it at every presentation boundary. If you extend that
artifact, mask the new field.

## Running it

```bash
make test-positions test-reservations test-reconciliation
make test-position-integration

python -m trading_system.cli positions validate
python -m trading_system.cli positions snapshot --simulated
python -m trading_system.cli reservations validate
python -m trading_system.cli reconciliation run --simulated --dry-run
```

Every one of those reports `orders submitted: 0` and `corrective orders: 0`,
read off the broker rather than asserted.
