# Operator handbook

How to run this system day to day, what every switch does, how to read what it
tells you, and what to do when it tells you something bad.

[CLAUDE.md](CLAUDE.md) explains *why* the system is shaped this way and
[CLOUD_CODE_IMPLEMENTATION_SPEC.md](CLOUD_CODE_IMPLEMENTATION_SPEC.md) is the
source of truth for the design. This document assumes neither: it is for the
person at the keyboard.

Every command below is written as `python -m trading_system.cli …`. Prefix with
`.venv/bin/` or activate the venv first. `make help` lists the shortcuts.

---

## 0. The state of this checkout, right now

Read this before you type anything. Three things about the working tree differ
from what the repository ships, and all three are deliberate local operator
changes:

| What | Shipped | Here | Consequence |
|---|---|---|---|
| `config/execution.yaml` → `enabled` | `false` | **`true`** | The system-level order permission is ON. Only `--confirm` stands between a stored allocation and a real paper order. |
| `.env` → `IBKR_READ_ONLY` | `true` | **`false`** | The IBKR API connection is no longer opened read-only, so IBKR itself will no longer refuse an order on our behalf. |
| Working tree | clean | **dirty** | `readiness check` reports `NOT_READY`; a clean tree is required for live review. |

The fourth entry that used to sit here — `treat_as_campaign_currency: [USD]`,
which accounted one dollar as one euro — **is gone**. The setting no longer
exists and a configuration carrying it fails to load. The campaign now declares
its capital in EUR and trades in USD, with an explicit rate between them; see
§9.4.

`TRADING_MODE` is `PAPER` and both live guards are off, so LIVE remains
refused in three independent places. But treat this checkout as **armed for
paper trading**, not as a fresh clone.

`make test` will show **three expected failures** — every one of them a
tripwire reporting the execution flip above, and none of them a defect. They
are enumerated in [§9.9](#99-make-test-fails). Anything else failing is real.

To disarm, in order of speed — see [§10](#10-stopping-it).

```bash
git diff --stat config/          # what is locally changed
python -m trading_system.cli health   # mode, read-only state, broker endpoint
```

---

## 1. What you are operating

```
AI AGENTS  →  DETERMINISTIC DECISION LAYER  →  EXECUTION ENGINE  →  IBKR  →  RECONCILIATION
   propose        validate / allocate / limit        submit         reality       compare
```

A model may research an underlying, classify a market hypothesis and pick an
allowed strategy. A model never determines budget, risk limits, position size,
or whether an order is permitted. Those are deterministic modules, and a model
cannot override one.

Four operator-relevant consequences:

- **`NO_TRADE` is a normal outcome at every stage.** A universe was selected
  and nothing was researched, research succeeded and no strategy fit, a
  strategy was chosen and no contract qualified — none of those is a failure.
  Do not go looking for the bug.
- **"We could not look" is never "there is nothing there".** The system keeps
  those apart everywhere (`BROKER_DATA_UNAVAILABLE` vs `BROKER_RETURNED_EMPTY`,
  `REQUIRED_DATA_UNAVAILABLE` vs `NO_VALID_CONTRACT`). When you read a status,
  read which of the two it is.
- **IBKR is authoritative.** If the stored records and the broker disagree, the
  broker wins, reconciliation *reports* the disagreement, and nothing repairs
  it automatically.
- **Nothing retries a submission.** An order whose outcome was never learned is
  `UNKNOWN` and stays `UNKNOWN` until you resolve it by observing the broker.

### Two loops, different cadences

| Loop | What it does | Cadence | Can it place an order? |
|---|---|---|---|
| **Opportunity discovery** (slow) | universe → research → strategy → contract → risk → allocate → execute | daily-ish, deliberately | Only the last step, with two switches |
| **Position management** (fast) | snapshot → reconcile → exit policy → wait/exit → settle | every 5–15 min | Only `exit run --confirm` / the `exit_management` job |

Do not run research on every monitoring tick.

---

## 2. Every gate between "installed" and "an order goes out"

This is the whole safety surface. No two of these are the same decision, and
none implies another.

| # | Gate | Where | Ships | Turns off by |
|---|---|---|---|---|
| 1 | `TRADING_MODE` | `.env` | `PAPER` | `DRY_RUN` — never leaves the process, uses the simulator |
| 2 | `LIVE_TRADING_CONFIRMED` | `.env` | `false` | leave false |
| 3 | `LIVE_READINESS_CHECKLIST_SIGNED_OFF` | `.env` | `false` | leave false |
| 4 | `execution.enabled` | `config/execution.yaml` | `false` | set false |
| 5 | `--confirm` | the command line | absent | omit it |
| 6 | `IBKR_READ_ONLY` | `.env` | `true` | set true — IBKR itself then refuses |
| 7 | `allow_live` / `paper_only` | `config/execution.yaml` | `false` / `true` | leave |
| 8 | `exit_management.enabled` + `authorize_exits` | `config/schedules.yaml` | `false` + `true` | set `enabled: false` |
| 9 | `cleanup.enabled` | `config/cleanup.yaml` | `false` | set false |

**Gates 4 and 5 are both required for any entry order.** An allocation id is
not permission to trade, and a configuration flag is not a decision to trade
today.

**Gate 6 is the one that does not depend on our code being correct.** With
`IBKR_READ_ONLY=true` the API connection is opened read-only and the broker
rejects orders at its end. Every diagnostic, the data layer and every upstream
stage get a read-only connection regardless of this value; only the execution
service can ask for a writable one.

**A dry run is structural, not a flag.** `execution run --dry-run` never
constructs a broker at all. It cannot place an order because there is nothing
to place it with.

**Orphan cleanup needs four independent authorisations**: `cleanup.enabled`,
`execution.enabled`, `--confirm`, and `PAPER` with both live guards off *and*
the connected account proving it is a paper account. Without `--confirm` the
code path never reaches the method that can build a writable broker.

**LIVE is refused in three places** — the environment guards, `allow_live:
false` in the execution config, and the IBKR adapter itself. Milestone 12's
strongest verdict is `READY_FOR_LIVE_REVIEW`, which is a request for a person
to look. There is deliberately no `READY_FOR_LIVE`.

---

## 3. Setting up

```bash
uv venv && uv pip install -e '.[dev]'      # or: make install
cp .env.example .env                        # then fill in real values
```

Secrets come only from `.env` or real secret storage, never from a committed
file: `IBKR_USERNAME`, `IBKR_PASSWORD`, `IBKR_ACCOUNT`, `ANTHROPIC_API_KEY`,
`TELEGRAM_BOT_TOKEN`.

### Ports, which are easy to confuse

| | paper | live |
|---|---|---|
| IB Gateway | **4002** (project default) | 4001 |
| TWS | 7497 | 7496 |

Under Docker Compose the runtime reaches the gateway at `IBKR_HOST=ib-gateway`
on internal port 4002; `IBKR_PORT` in `.env` is the **host** port. The compose
file publishes `${IBKR_PORT:-4002}:4004` — container side **4004**, which is
the gateway image's `socat` forwarder, not the gateway's own API port. See
[§9.1](#91-gateway-says-connected-then-api-connection-failed-timeouterror).

### Market data

`IBKR_MARKET_DATA_TYPE=3` (delayed) needs no paid subscription and is honestly
labelled as delayed. It has real consequences for what the universe can select
— see [§9.3](#93-the-universe-selects-nothing).

### Verify without touching anything

```bash
python -m trading_system.cli health          # config, mode, read-only state, schemas
python -m trading_system.cli config          # validate and print the YAML policy
python -m trading_system.cli test ibkr-connection    # one read-only connection
python -m trading_system.cli test ibkr-portfolio     # read-only account + positions
```

Every diagnostic prints `orders_submitted` and it is always 0. Append
`--simulated` to any of them to run with no gateway at all.

### Docker

```bash
docker compose up -d ib-gateway trading-runtime
docker compose --profile observability up -d      # OPTIONAL telemetry stack
```

The trading system starts and runs without the observability stack. Nothing in
it can change a trading decision.

---

## 4. The slow loop — from data to an authorised order

Each stage consumes the stored output of the previous one. Run them in order.
Every stage before the last submits **zero** orders and opens no writable
broker.

### 1. Collect data

```bash
python -m trading_system.cli data collect --symbol SPY
python -m trading_system.cli data collect-options --symbol SPY --quotes --dte 21
python -m trading_system.cli data collect-options --symbol SPY --quotes --expiration 2026-09-11
python -m trading_system.cli data status
python -m trading_system.cli data quality --symbol SPY
```

`universe run` reads stored data only — it never collects and never opens a
broker connection. Run collection first or it reports `DATA_UNAVAILABLE`
rather than inventing candidates.

**Option quotes are the binding constraint on contract selection**, not the
chain. A stored `OPTION_CHAIN` gives expirations and strikes; it gives no
contract id, no bid, no delta. With chain metadata alone every selection ends
`REQUIRED_DATA_UNAVAILABLE` — correctly.

**Collect in this order, and it matters.** The quote step needs a stored chain
(for the expirations and strikes) *and* a stored underlying price (the strike
band is a percentage around it). Working either out from the broker would cost
extra round trips on a connection that reliably answers one, so the collector
reads them from the store and refuses — naming the missing command — rather
than reaching for them.

**Which contracts get quoted.** `data.yaml`'s `collection.option_quotes` states
the DTE window, the strike band and a contract cap. Without `--expiration` or
`--dte`, the expiration nearest the middle of that window is used. Do not
expect the chain's *first* expiration: it is often a day out, and quotes
collected there can never satisfy `contract_selection.yaml`'s 21-day target or
`risk.yaml`'s 14–30 DTE range. When the contract cap binds, the strikes nearest
the money are kept and the run says `CONTRACT_LIMIT_APPLIED`.

**Delayed data gives you one-sided option quotes, and that is not a cost.**
With `IBKR_MARKET_DATA_TYPE=3` and the market closed, IBKR sends the option bid
and ask as `-1` — its "no value" marker. The adapter drops them, so the quotes
arrive priced by `last` alone, and allocation cannot read an ask. Set
`IBKR_MARKET_DATA_TYPE=4` (delayed-frozen) to get the session's last two-sided
quote, with open interest and Greeks. The two are never substituted for each
other; `origin` on the stored snapshot records which answered
(`BROKER_DELAYED` vs `BROKER_FROZEN`).

### 2. Select the universe

```bash
python -m trading_system.cli universe validate    # config + data readiness
python -m trading_system.cli universe run --dry-run
python -m trading_system.cli universe run
python -m trading_system.cli universe show
python -m trading_system.cli universe explain --run-id <ID> --symbol SPY
```

Answers one question: which underlyings deserve deeper research. Output is a
list of underlyings — never a strike, expiry, direction, strategy or amount of
money. A deterministic exclusion is final: the agent runs after the pre-filter
and can only reorder what survived.

Without `ANTHROPIC_API_KEY` the run ends `AI_UNAVAILABLE` and selects nothing.
It falls back to a deterministic ordering only if `config/universe.yaml`
explicitly permits one, and such runs are stamped `DETERMINISTIC_ONLY`.

### 3. Research

```bash
python -m trading_system.cli research validate
python -m trading_system.cli research run
python -m trading_system.cli research run --symbol NVDA
python -m trading_system.cli research show --symbol NVDA
python -m trading_system.cli research explain --symbol NVDA
```

Produces an outlook, never a contract. Each underlying gets its own isolated
model context, and each failure is recorded per symbol — one unreachable call
does not stop the rest. A `--symbol` the universe did not select is refused
with `CONFIGURATION_ERROR` rather than researched.

There is **no deterministic fallback here at all**. A universe can be ordered
without a model; a market outlook synthesised without one would be a fabricated
view wearing a report's clothes.

### 4. Choose a strategy

```bash
python -m trading_system.cli strategy validate
python -m trading_system.cli strategy run
python -m trading_system.cli strategy show
```

The AI picks a *strategy*. It is never shown a chain, a strike, a contract id,
an account, or even a date — events arrive as `days_until`. A strategy that was
not offered is not merely unlikely, it is inexpressible in the output schema.

### 5. Select the contract — deterministic, no model

```bash
python -m trading_system.cli contract validate
python -m trading_system.cli contract select --dry-run
python -m trading_system.cli contract select
python -m trading_system.cli contract show --symbol NVDA
```

Takes no `--as-of` deliberately: the instant comes from each decision, so a
selection reconstructs exactly the data that was visible when the strategy was
chosen. Nothing is approximated — a missing delta is `MISSING_DELTA`, an
unquoted contract has an unknown cost, unreported option liquidity is
`OPTION_LIQUIDITY_UNKNOWN`.

### 6. Capture the account — the one broker read in the risk path

```bash
python -m trading_system.cli risk capture-account
python -m trading_system.cli risk validate       # the limits in force, by layer
python -m trading_system.cli risk evaluate       # permitted? persists nothing
python -m trading_system.cli risk explain --symbol NVDA
```

The risk engine holds no broker: it reads a stored `AccountSnapshot` by id.
That is a safety property, not just architecture — a risk check that fetched
its own account state could hang the process at the worst possible moment.

Run `risk capture-account` before allocating, or allocation reports
`ACCOUNT_SNAPSHOT_UNAVAILABLE` rather than assuming the money is there.

### 7. Allocate capital — deterministic, no model

```bash
python -m trading_system.cli allocation validate
python -m trading_system.cli allocation run --dry-run     # reserves nothing
python -m trading_system.cli allocation run
python -m trading_system.cli allocation show
python -m trading_system.cli allocation explain --symbol NVDA
```

**The campaign is not the account.** EUR 5,000 is the envelope; the paper
account's balance is irrelevant to it, except that an account holding less
binds instead. The most restrictive relevant limit always wins.

**And the campaign's currency is not the account's.** The envelope is declared
in EUR — the money you actually hold — and spent in USD, because that is what a
US-listed option is quoted in. `allocation validate` prints both, along with the
rate between them and where it came from. If it prints `FX UNAVAILABLE`, run
`risk capture-account` first: the rate is read from IBKR with the balance, and
without one every candidate is rejected `FX_RATE_UNAVAILABLE` rather than sized
against a figure in the wrong currency.

Re-running over the same upstream artifacts is idempotent: the second run
records `ALREADY_ALLOCATED` and reserves nothing. `NO_ALLOCATION` at the run
level is the ordinary answer when the campaign is already committed — a valid
strategy and a valid contract are not an entitlement to capital.

**An authorisation is not an order.** No order type, no side, no limit price,
no broker order id.

### 8. Execute — the only stage that can send an order

```bash
python -m trading_system.cli execution validate       # the policy in force
python -m trading_system.cli execution run --dry-run  # builds an order, opens no broker
python -m trading_system.cli execution run --confirm   # SUBMITS
python -m trading_system.cli execution run --allocation-id <ID> --confirm
python -m trading_system.cli execution show
python -m trading_system.cli execution history
python -m trading_system.cli execution explain --execution-id <ID>
```

Takes no `--as-of`: the authorisation's own validity window decides whether it
may still be acted on. LIMIT orders only, `DAY` time in force, no stops, no
brackets. A multi-leg structure goes out as one combo order — it fills as a
structure or not at all.

Execution changes nothing it was given. When a broker refuses because the
market moved, the answer is a recorded failure and a **new** Milestone 7
authorisation — never a smaller order that fits.

Re-running is idempotent: the second run records `ALREADY_SUBMITTED` and stores
no new attempt.

---

## 5. The fast loop — watching what you own

### Capture broker reality

```bash
python -m trading_system.cli positions snapshot          # what the broker holds
python -m trading_system.cli positions snapshot --dry-run
python -m trading_system.cli positions show              # BROKER OBSERVED
python -m trading_system.cli positions show --expected   # INTERNAL EXPECTED
python -m trading_system.cli positions explain --contract-id 12345
```

Two records that are never merged, and every command labels which it is
showing:

- **Broker observed** — what IBKR says the account holds.
- **Internal expected** — what *confirmed fills* say should exist.

Only a confirmed broker fill makes an expected position. An allocation, a
submitted order, an acknowledgement and an `UNKNOWN` submission all establish
nothing. A partial fill establishes exactly what filled.

### Reconcile

```bash
python -m trading_system.cli reconciliation run
python -m trading_system.cli reconciliation run --dry-run    # writes nothing at all
python -m trading_system.cli reconciliation show
python -m trading_system.cli reconciliation explain
python -m trading_system.cli reconcile                       # alias for run
```

One short-lived read-only connection reads account, positions, open orders and
fills. It **reports; it never repairs**. No record is edited into agreement, no
position is adopted, no order is cancelled, no compensating trade is proposed.
Every recommendation reads `ACTION REQUIRED` rather than naming a trade. Every
run prints 0 submitted and 0 corrective orders.

### Decide whether to close

```bash
python -m trading_system.cli exit validate       # policy + per-strategy narrowing
python -m trading_system.cli exit evaluate       # WAIT / EXIT / BLOCK. Submits nothing
python -m trading_system.cli exit evaluate --position-id <ID>
python -m trading_system.cli exit explain --position-id <ID>
python -m trading_system.cli exit run --dry-run
python -m trading_system.cli exit run --confirm  # SUBMITS
python -m trading_system.cli positions monitor           # the scheduled operation
python -m trading_system.cli positions monitor --capture # read the broker first
```

Deterministic — no model, no broker, no prompt anywhere in this group.
Evaluation and submission are separate commands for the same reason they are
separate jobs: judging whether a position should close must not close one.

**Precedence decides, and it is answered once.** The first policy that does not
say `WAIT` governs, in this order:

```
1 POSITION_CONSISTENCY   2 BROKER_OBSERVATION   3 EXECUTION_STATE
4 CONTRACT_VALIDITY      5 EXPIRATION           6 DATA_QUALITY
7 MAX_LOSS               8 THESIS               9 TAKE_PROFIT   10 TRAILING_STOP
```

So a position at its take-profit whose quantity the broker disputes **blocks** —
the profit was computed from a quantity nobody confirmed. A position one day
from expiry whose research report cannot be read **exits** — a missing file must
not be able to disable the most important policy in the group.

A `BLOCK` is a current verdict, re-derived every evaluation, not a memory. What
is never retried is a submission whose outcome is unknown.

### Settle capital and record what a trade made

```bash
python -m trading_system.cli reservations show
python -m trading_system.cli reservations validate   # what would move, moving nothing
python -m trading_system.cli reservations history
python -m trading_system.cli pnl show
python -m trading_system.cli pnl show --daily
python -m trading_system.cli pnl settle --dry-run
python -m trading_system.cli pnl settle
```

Capital returns **only on broker-confirmed closure** — not a requested exit,
not a submitted one, not a reported fill. Realised results come from
broker-confirmed fills only; a missing commission, an absent multiplier or an
unresolved execution produces `NOT_AVAILABLE` **with no figure attached at
all**.

`reservations release` is deliberately narrow: it refuses while any execution
against the authorisation is `UNKNOWN` or still working, and there is no
force-release anywhere. Resolve the execution against the broker instead — the
resolved state releases the capital on its own.

---

## 6. The scheduler

```bash
python -m trading_system.cli ops scheduler plan     # what would run. Side-effect free
python -m trading_system.cli ops scheduler status   # last tick, and any UNKNOWN job
python -m trading_system.cli ops scheduler tick     # run everything due, once
python -m trading_system.cli ops scheduler start --max-ticks 10
python -m trading_system.cli ops jobs               # registered jobs + run history
python -m trading_system.cli ops jobs --run reconciliation
```

The scheduler contains no trading logic and holds no broker. Every job is one
call to an already-tested service method. Timezone is `America/New_York`; tick
resolution is 30 s against one-minute cron.

| Job | Cron | Ships | Market hours | Can order? |
|---|---|---|---|---|
| `data_collection` | `*/30 9-16 * * 1-5` | on | yes | no |
| `universe_refresh` | `0 7 * * 1-5` | on | no | no |
| `opportunity_scan` | `30 10 * * 1-5` | **off** | yes | **no, under any configuration** |
| `position_monitor` | `*/5 9-16 * * 1-5` | on | yes | no (`authorize_exits: false`) |
| `exit_management` | `*/5 9-16 * * 1-5` | **off** | yes | **yes — needs `execution.enabled` too** |
| `thesis_monitor` | `0 12 * * 1-5` | **off** | yes | no — records `SKIPPED / NOT_IMPLEMENTED` |
| `reconciliation` | `*/10 * * * *` | on | no | no |
| `pnl_settlement` | `*/15 * * * *` | on | no | no |
| `operational_health` | `*/5 * * * *` | on | no | no |
| `end_of_day_report` | `0 18 * * 1-5` | on | no | no |

`opportunity_scan` runs research through allocation — everything up to an
authorisation and no further. It ships off because opening positions on a
cadence is a decision an operator makes deliberately.

Each job is also individually runnable, and running one by hand goes through
the scheduler's own guards, so it cannot do what the cadence would have
refused:

```bash
python -m trading_system.cli run universe
python -m trading_system.cli run research
python -m trading_system.cli run position-monitor      # submits nothing
python -m trading_system.cli run reconciliation
python -m trading_system.cli run pnl-settlement
python -m trading_system.cli run operational-health
python -m trading_system.cli run end-of-day-report
python -m trading_system.cli run exit-management       # CAN SUBMIT. Needs BOTH switches
```

Job statuses are five different facts and none is a synonym for another:

| Status | Means | Your move |
|---|---|---|
| `SUCCESS` | ran, completed | none |
| `SKIPPED` | deliberately did not run (disabled, out of hours) | none — not an error |
| `BLOCKED` | a guard refused it | read which guard |
| `FAILED` | ran and raised | read the record |
| `UNKNOWN` | completion was never recorded | **the job may still be in flight.** Python cannot kill a thread. Check before assuming anything |

A duplicate firing returns the stored run; it does not write a second,
contradictory line about it.

---

## 7. Monitoring

```bash
python -m trading_system.cli ops health            # trading + observability, apart
python -m trading_system.cli ops health --broker   # one read-only probe as well
python -m trading_system.cli ops alerts            # what was raised
python -m trading_system.cli ops alerts --evaluate # evaluate the rules now
python -m trading_system.cli ops metrics           # telemetry + cardinality guard
```

**Health has two verdicts computed from disjoint components.**
`trading_status` answers *can this system safely trade?*; `observability_status`
answers *can we see what it is doing?* An unreachable Grafana degrades the
second and cannot move the first. `UNKNOWN` outranks `HEALTHY` — an unprobed
broker is reported `UNKNOWN`, because "all green" must not be achievable by
not looking.

**An alert is a notification.** Nothing in the alerting path can place, cancel
or modify an order, and an alert cannot carry a recommended action that reads
as an instruction to trade. A `CRITICAL` rule cannot be disabled in
configuration — muting the notification does not mute the condition. Alerts are
stored *before* any channel sees them; the difference between the stored set
and the delivered set is exactly what an operator never saw.

The `CRITICAL` rules, i.e. the ones that should get you out of your chair:

`BROKER_UNAVAILABLE` · `EXECUTION_UNKNOWN` · `EXECUTION_DUPLICATE_ATTEMPT` ·
`LIVE_EXECUTION_ATTEMPT` · `ORDER_WITHOUT_ALLOCATION` ·
`ORDER_WITHOUT_EXECUTION_RECORD` · `EXECUTION_OUTSIDE_AUTHORIZED_PATH` ·
`RECONCILIATION_MISMATCH` · `EXIT_UNKNOWN` · `DAILY_LOSS_THRESHOLD_EXCEEDED`

The daily loss figure has three states, not two: `TRACKED` is a measurement,
`UNKNOWN` means positions closed today and at least one produced no usable
figure, `NOT_TRACKED` means no ledger was consulted. A figure is refused
alongside anything but `TRACKED` — a comfortable number next to "we could not
measure today" is exactly how an unmeasured day passes a loss limit.

### Optional telemetry stack

```bash
make observability-up        # collector, Tempo, Prometheus, Loki, Grafana
make observability-test      # emit REAL telemetry and ask each backend
make observability-down
```

Set `OBSERVABILITY_ENABLED=true` to export. If every backend is down the
trading system behaves identically — that is asserted by comparing stored
artifacts byte for byte under a working, a broken and an absent exporter.

Grafana defaults to `admin / admin` on port 3000. Change it, or put it behind
something that authenticates, before it is reachable from anywhere.

---

## 8. Reading what it tells you

Exit codes: **0** ok · **1** error or refusal · **3** the command exists but its
milestone is not built.

### Statuses you will actually see

| Status | Stage | Means |
|---|---|---|
| `NO_TRADE` | any | a decision not to act. Normal |
| `DATA_UNAVAILABLE` | universe | the store is empty. Run `data collect` |
| `VOLUME_UNAVAILABLE` | universe | no `average_daily_volume` was reported. Never read as zero, never as passing |
| `DATA_NOT_RESEARCH_USABLE` | universe | quality gates rejected the record upstream of any volume check |
| `AI_UNAVAILABLE` | universe / research | the model could not be reached. Nothing was selected |
| `AI_INVALID_OUTPUT` | universe | the response violated the contract. Rejected **in full**, never repaired |
| `DETERMINISTIC_ONLY` | universe | ordered without a model, and stamped so the record never implies otherwise |
| `SEMANTIC_VALIDATION_FAILED` | research | the report cited evidence the input did not contain, or mislabelled a hypothesis |
| `NO_VALID_CONTRACT` | contract | we looked and nothing qualified |
| `REQUIRED_DATA_UNAVAILABLE` | contract | we could not look. Usually: no option quotes |
| `MISSING_DELTA` / `OPTION_LIQUIDITY_UNKNOWN` | contract | not estimated, not assumed zero |
| `CURRENCY_MISMATCH` | risk | the contract is not quoted in the currency this campaign trades. See §9.4 |
| `FX_RATE_UNAVAILABLE` | risk | no rate carries your capital into the traded currency. Capture an account. See §9.4 |
| `FX_RATE_STALE` | risk | a rate exists and was too old at the decision instant. See §9.4 |
| `ACCOUNT_SNAPSHOT_UNAVAILABLE` | allocation | run `risk capture-account` |
| `ALREADY_ALLOCATED` | allocation | idempotent re-run. Reserved nothing |
| `NO_ALLOCATION` | allocation | authorised nothing, usually because the campaign is committed |
| `SUBMISSION_PENDING` | execution | the record was written before the send. An order **may** be in flight |
| `UNKNOWN` | execution | we sent something and never learned the outcome. **Never retried** |
| `FAILED` | execution | it provably never left the process |
| `ALREADY_SUBMITTED` | execution | idempotent re-run |
| `MULTI_LEG_UNSUPPORTED` | execution | the structure could not be expressed as one combo. A refusal, not an approximation |
| `BROKER_DATA_UNAVAILABLE` | positions | we could not read. **No comparison is made** |
| `BROKER_RETURNED_EMPTY` | positions | the broker really reported nothing held |

### Reconciliation findings worth acting on

| Finding | Means |
|---|---|
| `ORPHAN_BROKER_POSITION` | the broker holds something no execution of ours accounts for. Real, never adopted. See §9.6 |
| `EXPECTED_POSITION_MISSING` | our confirmed fills say it should be there and the broker says it is not |
| `POSITION_QUANTITY_MISMATCH` | both sides shown, with the difference |
| `PARTIAL_STRUCTURE` | a multi-leg position is only half held. Not a closed trade — possibly a naked leg |
| `FAILED_EXECUTION_HAS_BROKER_ORDER` | critical: we recorded "nothing was sent" and there is an order |
| `UNKNOWN_EXECUTION_UNRESOLVED` | capital stays locked until you resolve it |
| `RESERVATION_RETAINED_UNKNOWN` | that lock, from the capital side |

Every finding shows both sides — expected value, observed value, the
difference, both provenances and both clocks.

### Lifecycle and ledger states

```
Position   OPEN → MONITORING → TRAILING_ACTIVE → EXIT_REQUIRED → EXIT_SUBMITTED → CLOSED
                                                              ↘ EXIT_UNKNOWN     (terminal: CLOSED)
                                                  BLOCKED  (a current verdict, not a memory)

Reservation  RESERVED → PARTIALLY_CONSUMED → CONSUMED → SETTLED
                      ↘ RELEASED (never spent)      ↘ UNKNOWN (stays locked)
```

`CLOSED` is terminal and only broker reality reaches it. `SETTLED` is what
returns capital to the campaign envelope; nothing weaker does.

---

## 9. Incident runbook

### 9.1 Gateway says `Connected`, then `API connection failed: TimeoutError()`

The `ib-gateway` image trusts only `127.0.0.1` (`jts.ini` `TrustedIPs`). A
connection to the gateway's own API port from anywhere but loopback is accepted
at TCP level and then dropped without an API answer — indistinguishable from a
hang, and it survives a container restart.

The image ships a `socat` forwarder for exactly this: paper 4002 → **4004**,
live 4001 → 4003. **4004 is the only externally-bound listener in the
container.** `docker-compose.yml` publishes `${IBKR_PORT:-4002}:4004`. If you
have edited it to publish `:4002`, that is the bug.

Diagnose with a raw handshake (`API\0` + length-prefixed `v100..187`), not
`nc -zv` — a bare TCP probe passes on both ports and proves nothing. The
working path answers `187\0<time>\0`; the broken one EOFs. The container has no
`ss`/`netstat`; read `/proc/net/tcp` and `/proc/net/tcp6` — the gateway listens
on IPv6 `:::4002` and does not appear in `/proc/net/tcp` at all.

`trading-runtime` solves the same problem the other way, with
`network_mode: "service:ib-gateway"`. Anything else that needs the API must do
one or the other.

### 9.2 A broker request hangs forever

Only the **first** live, uncached request/response round trip on a freshly
opened connection is reliably answered. A second explicit request on the same
connection can go unanswered forever even though the connection is healthy and
the first worked. This was reproduced at the raw-socket level and survives a
TWS restart.

What is safe: anything covered by `ib_async`'s startup handshake cache —
account summary, positions, open orders, fills. That is why `positions
snapshot` and `reconciliation run` read all four from one connection and issue
no health probe first.

`IBKR_REQUEST_TIMEOUT_SECONDS` bounds every request (the library waits forever
by default). There is no unbounded setting. If a command hangs anyway, the fix
is a one-purpose connection, not a retry.

### 9.3 The universe selects nothing

Almost always a delayed-data consequence, and almost always correct behaviour.

- **`VOLUME_UNAVAILABLE`** — IBKR delayed market data reports no volume. The
  shipped `min_average_daily_volume: 1000000` therefore rejects everything. A
  missing measurement is never a satisfied threshold. To select from delayed
  data, set the floor to `0` **explicitly**, or use a provider that reports
  volume.
- **`DATA_NOT_RESEARCH_USABLE`** — a plausibility finding the configuration
  does not tolerate. Check which one:

  ```bash
  python -m trading_system.cli data quality --symbol SPY
  ```

  The shipped configuration tolerates exactly one finding, `SUSPICIOUS_VOLUME`,
  so seeing this code today means something *else* failed plausibility — an
  implausible price, an impossible delta, a strike or expiry out of bounds.
  Read the detail line rather than reaching for the switch: those are the
  checks the allow-list exists to keep switched on.

**Tick 74, and why nothing divides it.** IBKR's delayed *session* volume
arrives at a scale that varies per value. It behaves like a decimal
floating-point number whose mantissa survives and whose exponent does not:
IBKR moved size fields to the `Decimal` type between API V9 and V10, and
`ib_async` decodes msgId 2 with a bare `float()` and no `decimalToDouble`. It
is IBKR's number rather than a library bug — ticks 8, 74 and 21 share one
decode path and only 74 comes back scaled — and it is not the `DBL_MAX`
sentinel.

The exponent **floats, and cannot be inferred from the number**. Decoded values
for the 2026-08-21 session, checked against an external public source:

| Symbol | tick 74 ÷ 10⁶ | real session volume | divisor actually needed |
|---|---|---|---|
| SPY  | 38,583,983 | 38,892,743 | 10⁶ |
| NVDA | 98,282,719 | 98,371,121 | 10⁶ |
| DIA  |  2,960,172 |  3,032,659 | 10⁶ |
| MSFT |  2,186,102 | **21,861,968** | **10⁵** |

DIA and MSFT have the same digit count and need different divisors, so neither
magnitude nor digit count is a usable signal. **Do not add a rescaling
correction or a per-symbol table**, and do not build one around whichever
symbol looks like the exception — the outlier moves between symbols. An earlier
note named AMZN; on 2026-08-21 AMZN was normal and MSFT was the broken one. Any
fixed divisor is wrong by an order of magnitude somewhere, silently, and can be
wrong in the direction that *passes* a liquidity floor.

**Why the universe still runs.** The value is preserved verbatim and still
flagged `SUSPICIOUS_VOLUME`; the flag still fails plausibility. What lets the
record through is
`config/data.yaml`'s `research_usability.tolerated_plausibility_issues`, which
names that one finding. The justification is narrow: **no decision in this
system is permitted to read tick 74.** The liquidity floor names
`average_daily_volume` — tick 21, which arrives clean and unscaled — and a
missing average is `VOLUME_UNAVAILABLE`, never answered from the session
figure. Tick 74 is kept as evidence that the feed misbehaves and nothing more,
so a record whose only defect is a field nobody may read is not unfit to
research.

Measured on the live paper feed on 2026-08-21, same code and same session: with
the list empty, seven of ten universe symbols were rejected
`DATA_NOT_RESEARCH_USABLE`; with the shipped list, none were and nine were
selected.

Three things to know before editing that list:

1. **It is all-or-nothing.** A record stays usable only if *every* plausibility
   finding it carries is listed. One untolerated finding fails the record even
   with a tolerated one beside it.
2. **It is not `require_plausibility: false`.** That switch turns off every
   plausibility check at once — negative price, zero price, price out of
   bounds, implausible implied volatility, delta outside ±1, strike bounds,
   expiration horizon. Each entry in the allow-list is a separate decision
   needing its own justification; do not add one in passing.
3. **The verdict is stored at collection time.** Consumers read the verdict off
   the snapshot rather than recomputing it, so a change here governs records
   collected *afterwards*. Re-storing an unchanged response is recorded as a
   re-observation, not a new snapshot, so re-running collection over identical
   content will not clear a stale verdict.

### 9.4 Every contract is rejected `FX_RATE_UNAVAILABLE`

Not `CURRENCY_MISMATCH` — that was the old answer, and it is no longer the
right one. The universe is US-listed and priced in USD, your capital is in EUR,
and a mismatch between the two is the **expected** state rather than an error.
What the system needs is a rate, and it has one place to get it from:

```bash
python -m trading_system.cli risk capture-account
python -m trading_system.cli allocation validate    # prints the rate and its source
```

IBKR reports a per-currency `ExchangeRate` with the account summary the capture
already reads, so this costs no extra round trip. The rate is stored on the
account snapshot, which binds it to the balance it converts: a stored
authorisation can never have been made at a rate from a different moment.

If it still fails, the reason code says which of three things is wrong:

| Code | Meaning | Fix |
|---|---|---|
| `FX_RATE_UNAVAILABLE` | IBKR reported no rate for the pair, or no snapshot exists | Capture an account. If the capture shows no rates, the gateway is not reporting the ledger — check the account is funded and the connection is not read-limited |
| `FX_RATE_STALE` | The rate was older than `campaign.currency_policy.max_rate_age_seconds` at the decision instant | Capture a fresh account snapshot |
| `FX_RATE_INVALID` | A rate arrived and is not a usable number | Report it; nothing repairs a rate |

**Nothing converts your cash.** Trading a USD instrument does not turn your EUR
into dollars. Whether dollars must be acquired to settle a trade is IBKR's rule
for your account type, not something this system decides or triggers. The rate
exists so a EUR balance can be *compared* with a USD price.

**You do not need to change your IBKR base currency.** Leave it EUR. Nothing in
the automation reads it as a trading currency.

`CURRENCY_MISMATCH` still exists and means something narrower now: the contract
is quoted in a currency this campaign does not trade at all. An instrument price
is never converted in either direction — the limit price that reaches IBKR has
to be the number the exchange expects — so the fix is
`campaign.currency_policy.target_currency`, which should name the currency the
instruments are actually quoted in.

### 9.5 An execution is `UNKNOWN`

The order may be live at the broker right now. There is no code path from an
uncertain submission to a second one, `auto_retry_on_timeout: true` fails to
load, and no amount of elapsed time turns `UNKNOWN` into `FAILED`.

```bash
python -m trading_system.cli execution explain --execution-id <ID> --resolve
```

That **reads** broker state. Note that absence from the open-order list settles
nothing on its own — a filled, a cancelled and a never-sent order look identical
from there.

While it is unresolved: its capital stays locked, `reservations release`
refuses it, and where *any* attempt against an authorisation is unresolved
nothing at all is released. That is the intended behaviour.

If there is a live order you want gone:

```bash
python -m trading_system.cli execution cancel --execution-id <ID> --confirm
```

### 9.6 Reconciliation reports `ORPHAN_BROKER_POSITION`

The broker really holds something no execution of ours accounts for.
Reconciliation reports it and refuses to adopt it, sell it, or assign it to a
campaign — correctly. Its acquisition provenance stays `UNKNOWN` forever.

The only resolution the system offers is **cleanup**: the controlled, PAPER-only
closure of a holding this system never opened. It closes them; it never adopts
them.

```bash
# A REVIEW. Reads the broker, picks targets, evaluates every gate, builds the
# exact order it would send — and constructs no writable broker at all.
python -m trading_system.cli reconciliation cleanup-orphans

python -m trading_system.cli reconciliation cleanup-orphans --contract-id 848575117
python -m trading_system.cli reconciliation cleanup-orphans --confirm   # SUBMITS
```

What it will refuse, and you should not try to talk it out of:

- a target not explicitly reported as an orphan by a reconciliation that
  actually compared something;
- a holding addressed by anything but a broker contract id;
- a quantity that changed since the reviewed report — refused, not resized;
- a **short** holding — closing one is a purchase whose cost is unbounded above;
- bundling two orphans that "look like a straddle" into one order. There is no
  combo path here at all;
- anything at all after an `UNKNOWN` submission against that holding.

Only a **position read** closes a target — not a submitted order, not a
reported fill. `REFUSED`, `REJECTED` and `UNCERTAIN` are three different facts.

### 9.7 A reconciliation mismatch

Block new executions until it is resolved. The system will not repair it for
you and will not propose a trade that would. Read the finding — it shows both
sides — decide what is true, and act deliberately.

### 9.8 A scheduled job wrote into the wrong place

A service's `root` is the **project** root, not the data root. Handing one an
already-resolved data root nests the tree a second time (`data/data/…`) and
fails *silently*: the run succeeds, writes where nobody looks, and the next
reader sees an empty store. If a store looks empty after a successful run, look
for a doubled path first.

### 9.9 `make test` fails

**Seven failures are expected in this checkout**, and each is a tripwire
reporting a local config flip rather than a defect. Verified 2026-08-20 by
re-running all seven against the shipped configuration, where they pass.

Caused by `execution.enabled: true`:

- `tests/execution/test_zero_orders.py::test_execution_submission_is_disabled_in_the_shipped_configuration`
- `tests/cleanup/test_gates.py::test_the_shipped_configuration_disables_execution`
- `tests/readiness/test_configuration.py::test_the_shipped_execution_switch_is_reported_off`

The four that used to be caused by `treat_as_campaign_currency: [USD]` are
gone: the setting no longer exists, and the currency behaviour it worked around
is now the shipped behaviour rather than a local flip.

To confirm nothing else is wrong, check them against the shipped policy —
this reverts the flip, so re-apply it afterwards if you are still operating:

```bash
git stash push config/execution.yaml
make test
git stash pop
```

`tests/integration/test_observability_stack.py` is separate and
**environmental**: it skips when Grafana does not answer, but its guard probes
Grafana alone, so a stack whose Grafana is up while its datasources are
unprovisioned fails rather than skipping. That is a deployment problem, not a
code one. Anything outside these two groups is real.

```bash
make check                 # lint + typecheck + test
make test-safety           # the order-submission gates. Fast, no gateway
pytest -m "not ibkr and not llm"    # the default: no gateway, no API key
```

A test that reaches the real gateway is a bug in the test. `tests/conftest.py`
clamps a developer's `.env` per variable — mode, both live guards, the live-test
unlock and `IBKR_READ_ONLY` — for every test except the `paper_execution`
marker, which is itself behind two unlock variables.

---

## 10. Stopping it

In order of how fast each takes effect and how little it depends on our code
being right.

1. **Stop the process.** `Ctrl-C` a running `ops scheduler start`, or
   `docker compose stop trading-runtime`. Nothing new is decided or sent.
2. **`IBKR_READ_ONLY=true` in `.env`, then restart.** The API connection is
   opened read-only and **IBKR** refuses orders. This does not depend on our
   own code being correct — it is the strongest switch here.
3. **`execution.enabled: false` in `config/execution.yaml`.** The system-level
   order permission goes off. `execution run` still validates, still builds the
   purchase card and still shows the order it would send; it simply never
   reaches a broker.
4. **`enabled: false` in `config/schedules.yaml`.** Nothing fires on a cadence.
   Every job stays described and individually runnable.
5. **`IB_GATEWAY_READ_ONLY=yes`**, a gateway-side block independent of the
   application's own guard.
6. **`TRADING_MODE=DRY_RUN`.** The simulator is used and nothing leaves the
   process at all.

To restore this checkout to what the repository ships:

```bash
git checkout -- config/execution.yaml config/campaign.yaml
# and set IBKR_READ_ONLY=true in .env
```

**Working orders are not cancelled by any of the above.** Cancel them
explicitly with `execution cancel --execution-id <ID> --confirm`, or at the
broker.

---

## 11. The readiness gate

Answers *is this system safe and operationally complete enough to proceed to the
next trading mode?* — with an immutable, auditable artifact, never a boolean.
It **reports and never enables**: no command here changes `TRADING_MODE`,
`execution.enabled`, `IBKR_READ_ONLY` or either live guard, and every run
prints 0 orders submitted.

```bash
python -m trading_system.cli readiness validate    # what "ready" means. Collects nothing
python -m trading_system.cli readiness check       # offline: config, git, stores. Seconds
python -m trading_system.cli readiness check --toolchain     # + pytest, ruff, mypy. Minutes
python -m trading_system.cli readiness check --broker --reconciliation
python -m trading_system.cli readiness check --observability # probes a RUNNING stack
python -m trading_system.cli readiness check --full
python -m trading_system.cli readiness show --verbose
python -m trading_system.cli readiness history
python -m trading_system.cli readiness explain --criterion TEST_SUITE_PASSES
```

Three levels, and the vocabulary stops where it does on purpose:

| Level | Means |
|---|---|
| `NOT_READY` | at least one blocking criterion is unsatisfied |
| `READY_FOR_PAPER` | paper-blocking criteria are all satisfied |
| `READY_FOR_LIVE_REVIEW` | **a request for a person to look.** Not an authorisation |

There is deliberately no `READY_FOR_LIVE`. A level with that name would
eventually be read as the authorisation itself.

Reading a result:

- Every `PASS` and every `FAIL` names its evidence; a criterion cannot be
  constructed with a verdict and no evidence id.
- `UNKNOWN`, `STALE` and `NOT_TESTED` all leave a blocking criterion
  unsatisfied, and none is ever relabelled `FAIL` — a question and a defect
  call for different work.
- With no flags, everything not collected is `NOT_TESTED` rather than passing.
  The cheap default deliberately cannot certify anything.
- Freshness is checked **before** the predicate, so evidence from another
  revision never gets to say `PASS`. Most evidence expires with the clock; a
  test result expires with the **working tree**.
- Live review additionally requires accumulated history — distinct days,
  reconciliation runs, scheduler ticks — and a **clean working tree**.

Signing off records a decision and enables nothing (`enables_trading` is
`const false` in the schema). The signer is required and never inferred:

```bash
python -m trading_system.cli readiness signoff --signed-by "Full Name" --confirm
```

---

## 12. Things not to do

- **Do not commit `execution.enabled: true`.** It has happened once already; it
  left a checkout one `--confirm` away from a real paper order.
  `tests/execution/test_zero_orders.py` is the tripwire.
- **Do not treat a missing measurement as a satisfied threshold.** No volume is
  not zero volume. No implied volatility is not IV = 0. No margin data is not
  zero margin.
- **Do not "fix" suspicious data.** A crossed quote is flagged, never
  un-crossed. A corrupt volume is preserved verbatim, never rescaled.
- **Do not retry an `UNKNOWN` submission.** Resolve it by observing the broker.
- **Do not force-release a reservation.** There is no such command, on purpose.
- **Do not run full research on a monitoring tick.**
- **Do not let anything but `execution/service.py` build a writable broker.**
  It has exactly one caller and three test suites assert it.
- **Do not adopt an orphan position.** Clean it up or leave it; those are the
  two options.
- **Do not put a domain identifier in a metric label.** One time series per
  trade is how a metrics backend falls over.
- **Do not commit a real credential.** `.env` is git-ignored; `.env.example`
  holds placeholders only.
- **Do not stage anything from here into the enclosing `/home/dmytro/git/`
  repository.** That is a separate repo full of unrelated projects.

---

## 13. Quick reference

```bash
# where am I
python -m trading_system.cli health
python -m trading_system.cli config
git diff --stat config/

# the slow loop, in order
python -m trading_system.cli data collect --symbol SPY
python -m trading_system.cli data collect-options --symbol SPY --quotes
python -m trading_system.cli universe run
python -m trading_system.cli research run
python -m trading_system.cli strategy run
python -m trading_system.cli contract select
python -m trading_system.cli risk capture-account
python -m trading_system.cli allocation run
python -m trading_system.cli execution run --dry-run
python -m trading_system.cli execution run --confirm          # SUBMITS

# the fast loop
python -m trading_system.cli positions snapshot
python -m trading_system.cli reconciliation run
python -m trading_system.cli exit evaluate
python -m trading_system.cli pnl settle
python -m trading_system.cli ops health

# when something is wrong
python -m trading_system.cli reconciliation explain
python -m trading_system.cli execution explain --execution-id <ID> --resolve
python -m trading_system.cli exit explain --position-id <ID>
python -m trading_system.cli ops alerts
python -m trading_system.cli ops scheduler status

# stop
#  1. Ctrl-C / docker compose stop trading-runtime
#  2. IBKR_READ_ONLY=true      in .env, restart
#  3. execution.enabled: false in config/execution.yaml
#  4. enabled: false           in config/schedules.yaml
```

Anything appended with `--simulated` runs offline against the deterministic
simulator. Anything with `--dry-run` persists nothing. Neither can reach a real
account.
