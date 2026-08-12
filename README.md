# Autonomous Options Trading System

A modular, testable, stateful options-trading system for Interactive Brokers.

This is a **paper-trading research system**. Live trading is not enabled and is
not reachable by configuration alone — see [Trading modes](#trading-modes).

> AI proposes. Deterministic modules validate. Broker executes.
> Broker state is authoritative.

## Status

**Milestone 8 of 12 — execution.** What exists today:

- domain models, enums, events and the position state machine;
- YAML configuration for campaign, risk, schedules, sources, strategies, data,
  universe, research and execution;
- the 30 JSON schemas for the workflow boundaries;
- structured logging and an injectable clock;
- a `Broker` abstraction with a deterministic `SimulatedBroker` and an
  `IBKRBroker` over IB Gateway, read-only unless deliberately opened for paper
  trading;
- read-only diagnostics for connection, portfolio, market data and option chain;
- the reconciliation foundation — detects discrepancies, never resolves them;
- **the data layer**: provider interfaces and free providers, canonical models,
  an eight-dimension quality engine, immutable point-in-time snapshots, an
  append-only historical ledger, and a repository behind an interface;
- **universe selection**: a configurable candidate pool, a deterministic
  eligibility pre-filter, the first AI agent, deterministic validation of what
  it returns, and immutable append-only universe runs;
- **market research**: point-in-time evidence assembly with news deduplication,
  the Market Researcher agent, deterministic hypothesis and confidence
  validation, and immutable append-only research reports;
- **strategy selection**: a strategy registry resolved against the global risk
  policy, the Strategy Selector agent, deterministic validation of what it
  returns, and immutable append-only strategy decisions;
- **contract selection**: a fully deterministic selector — no model, at all —
  that resolves a strategy into concrete legs from a stored option chain, or
  reports explicitly that no valid contract exists;
- **allocation and risk**: a campaign capital envelope independent of the broker
  account balance, a deterministic risk engine whose rejections no agent can
  override, a deterministic allocation engine that sizes positions by exact
  decimal arithmetic, immutable account snapshots, and an append-only
  allocation ledger;
- **execution**: the purchase card the specification requires before a trade, a
  deterministic order builder, an explicit execution state machine, idempotent
  submission that cannot place the same trade twice, combo orders so a
  multi-leg structure fills as one thing or not at all, fill tracking that
  never infers a fill, and an immutable execution ledger with append-only
  history;
- the CLI surface, with every command tagged read-only or state-mutating.

What deliberately does **not** exist yet: the position lifecycle, autonomous
trading and any form of live trading. Commands covering those exist in the CLI
but exit `3` naming the milestone that delivers them. They never fabricate
output.

**Submitting an order requires three deliberate acts**: `execution.enabled` in
`config/execution.yaml` (ships `false`), `IBKR_READ_ONLY=false` in the
environment (ships `true`), and `--confirm` on the command line. Nothing else in
the repository can reach a broker's order path. See
[Safety properties](#safety-properties).

See section 46 of [the implementation specification](CLOUD_CODE_IMPLEMENTATION_SPEC.md)
for the full milestone plan.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (or pip) for dependency management

## Setup

```bash
uv venv
uv pip install -e '.[dev]'        # dev extra includes ib_async
uv pip install -e '.[ibkr]'       # runtime only: just the broker client
cp .env.example .env              # then fill in real values; .env is git-ignored
```

`ib_async` is an **optional** dependency. The domain layer, the simulator and
the entire unit test suite run without it.

## Usage

```bash
python -m trading_system.cli --help            # full command surface
python -m trading_system.cli health            # config + mode + schema check (offline)
python -m trading_system.cli health --broker   # also probe broker connectivity
python -m trading_system.cli config            # validate and print configuration
python -m trading_system.cli version
```

Every command's help text is tagged `(read-only)` or `(mutates state)`.
Commands belonging to later milestones exit with code `3`.

> Parentheses, not square brackets: Rich interprets `[text]` as markup and
> swallows it, so the tag would vanish from `--help`.

### Broker diagnostics (all read-only)

```bash
python -m trading_system.cli test ibkr-connection
python -m trading_system.cli test ibkr-portfolio
python -m trading_system.cli test ibkr-market-data --symbol SPY
python -m trading_system.cli test ibkr-option-chain --symbol SPY
python -m trading_system.cli test reconciliation
```

Append `--simulated` to run any of them against the deterministic simulator
with no gateway. Each prints its access level (`READ-ONLY / PAPER` or
`READ-ONLY / SIMULATED`) and ends with `Orders submitted: 0`, read off the
broker rather than hard-coded. They exit `0` on success and `1` on failure —
never a fabricated success.

### Data layer

```bash
python -m trading_system.cli data providers                 # tier, cost, availability
python -m trading_system.cli data collect --symbol SPY      # store a quote snapshot
python -m trading_system.cli data collect-options --symbol SPY
python -m trading_system.cli data snapshot --symbol SPY
python -m trading_system.cli data snapshot --symbol SPY --as-of 2026-08-10T14:30:00+00:00
python -m trading_system.cli data quality --symbol SPY
python -m trading_system.cli data history --symbol SPY
python -m trading_system.cli data status
python -m trading_system.cli run data-collection            # the scheduled job, once
```

Every one is read-only with respect to the broker and reports what the data
*is* — `REAL`, `CACHED`, `HISTORICAL`, `SIMULATED` or `UNAVAILABLE` — not
merely whether the command succeeded. `--simulated` runs any of them offline.

See [data/README.md](data/README.md) for the storage layout, the quality model
and how to inspect a stored snapshot.

### Universe selection

```bash
python -m trading_system.cli universe validate            # config + data readiness
python -m trading_system.cli universe run --dry-run       # full pipeline, persists nothing
python -m trading_system.cli universe run
python -m trading_system.cli universe run --as-of 2026-08-10T14:30:00+00:00
python -m trading_system.cli universe show
python -m trading_system.cli universe history
python -m trading_system.cli universe explain --run-id <ID> --symbol SPY
```

Every one is read-only with respect to the broker and reports
`Orders submitted: 0`. The universe layer constructs no broker at all, so that
is structural rather than a check performed at the end.

`universe run` reads stored data only — it never collects and never connects —
so run `data collect` first if the store is empty. It reports
`DATA_UNAVAILABLE` rather than inventing candidates.

### Market research

```bash
python -m trading_system.cli research validate                # config + data readiness
python -m trading_system.cli research run --dry-run           # full pipeline, persists nothing
python -m trading_system.cli research run
python -m trading_system.cli research run --as-of 2026-08-10T14:30:00+00:00
python -m trading_system.cli research run --symbol NVDA       # a subset of the universe
python -m trading_system.cli research show
python -m trading_system.cli research explain --symbol NVDA
python -m trading_system.cli research history --symbol NVDA
python -m trading_system.cli research validate --run-id <ID>  # re-check a stored run
python -m trading_system.cli run research                     # the scheduled job, once
```

Every one is read-only with respect to the broker and reports
`Orders submitted: 0`. The research layer constructs no broker at all, so that
is structural rather than a check performed at the end.

`research run` consumes the latest universe run and stored data only. It never
collects, never connects, and never selects its own subjects: a `--symbol` the
universe did not choose is refused rather than researched.

### Strategy and contract selection

```bash
python -m trading_system.cli strategy validate            # registry + hypothesis mapping
python -m trading_system.cli strategy run --dry-run       # full pipeline, persists nothing
python -m trading_system.cli strategy run
python -m trading_system.cli strategy run --run-id <research-run-id>
python -m trading_system.cli strategy run --symbol NVDA
python -m trading_system.cli strategy show [--run-id <ID>] [--symbol NVDA]
python -m trading_system.cli strategy history [--symbol NVDA]
python -m trading_system.cli strategy validate --run-id <ID>   # re-check a stored run

python -m trading_system.cli contract validate            # the deterministic policy
python -m trading_system.cli contract select --dry-run
python -m trading_system.cli contract select
python -m trading_system.cli contract select --run-id <strategy-run-id>
python -m trading_system.cli contract show [--run-id <ID>] [--symbol NVDA]
python -m trading_system.cli contract history [--symbol NVDA]
python -m trading_system.cli contract validate --run-id <ID>
```

Every one is read-only with respect to the broker and reports
`Orders submitted: 0`. Neither layer constructs a broker at all, so that is
structural rather than a check performed at the end.

`strategy run` consumes a stored research run: it never re-researches, and a
`--symbol` research did not cover is refused. `contract select` takes no
`--as-of` on purpose — the instant comes from each decision, so a selection
reconstructs exactly the data that was visible when the strategy was chosen.

### Allocation and risk

```bash
# The one command that reads the broker. Read-only; submits zero orders.
python -m trading_system.cli risk capture-account --simulated

python -m trading_system.cli risk validate            # the limits in force, by layer
python -m trading_system.cli risk evaluate            # permitted? persists nothing
python -m trading_system.cli risk explain --symbol NVDA

python -m trading_system.cli allocation validate      # campaign envelope + policy
python -m trading_system.cli allocation run --dry-run # reserves nothing
python -m trading_system.cli allocation run
python -m trading_system.cli allocation show [--run-id <ID>] [--symbol NVDA]
python -m trading_system.cli allocation explain --symbol NVDA
python -m trading_system.cli allocation history [--symbol NVDA]
```

Every one submits zero orders, and neither engine constructs a broker, so that
is structural rather than a check performed at the end.

`allocation run` consumes a stored contract run and takes no `--as-of` for the
same reason `contract select` does not: the instant comes from the run being
allocated against, so an authorisation reconstructs exactly the prices that
were visible when the contract was chosen. It requires a stored account
snapshot and reports `ACCOUNT_SNAPSHOT_UNAVAILABLE` rather than assuming the
money is there. Re-running over the same upstream artifacts is idempotent: the
second run records `ALREADY_ALLOCATED` and reserves nothing.

### Execution

```bash
python -m trading_system.cli execution validate        # the policy in force
python -m trading_system.cli execution run --dry-run   # builds an order; opens no broker
python -m trading_system.cli execution run --confirm   # SUBMITS
python -m trading_system.cli execution show [--execution-id <ID>] [--run-id <ID>]
python -m trading_system.cli execution history [--allocation-id <ID>]
python -m trading_system.cli execution explain --execution-id <ID> [--resolve]
python -m trading_system.cli execution cancel --execution-id <ID> --confirm
```

The only command group that can place an order, and it needs
`execution.enabled: true`, `IBKR_READ_ONLY=false` and `--confirm` — all three,
each shipped in the safe position. Without `--confirm` nothing is built and
nothing is sent; with `--dry-run` no broker is constructed at all. See
[Execution](#execution).

## Data architecture

```
PROVIDERS      retrieve, and say honestly who they are
    |
RAW            preserved verbatim, never edited - the evidence
    |
NORMALIZATION  canonical shape, provenance attached, values untouched
    |
QUALITY        eight independent dimensions; flags, never fixes
    |
SNAPSHOT       immutable, hashed, version-stamped, point-in-time
    |
HISTORICAL     append-only ledger; accumulates, never overwrites
    |
REPOSITORY     the only interface consumers see
```

## Universe selection

```
CONFIGURED SOURCE       explicit and versioned; index lists are not approximated
    |
POINT-IN-TIME EVIDENCE  DataRepository only - no provider, no broker
    |
DETERMINISTIC FILTER    decides eligibility; nothing above reverses it
    |
AGENT INPUT CONTRACT    structurally cannot carry a rejected asset
    |
UNIVERSE SELECTOR       ranks; cannot admit, cannot exceed the size cap
    |
DETERMINISTIC VALIDATION rejects a violating response in full, never repairs
    |
IMMUTABLE RUN           appended, never overwritten
```

The output is a list of **underlyings** — never option contracts, never a
strategy, never a direction, never an amount of money. Five rules do the work:

- a deterministic exclusion is final — no ranking can restore it;
- a violating model response is rejected whole, never partially accepted;
- an unreachable or unusable model fails closed and selects nothing, unless a
  deterministic ordering is explicitly configured (and then it is stamped as one);
- `UNKNOWN` optionability is never read as `TRUE` or as `FALSE`;
- underlying share volume is not option liquidity, and nothing claims otherwise.

Providers shipped, all free, none required to be paid:

| Provider         | Data                        | Cost                | Status                             |
| ---------------- | --------------------------- | ------------------- | ---------------------------------- |
| `IBKR`           | quotes, option chains       | free with account   | implemented, paper-validated       |
| `SEC_EDGAR`      | filing metadata             | free                | implemented, validated live        |
| `SEC_XBRL`       | reported fundamentals       | free                | implemented, validated live        |
| `FIXTURE_NEWS`   | news articles               | free                | interface + replay; live deferred  |
| `FIXTURE_EVENTS` | corporate events            | free                | interface + replay; live deferred  |
| `SIMULATOR`      | synthetic quotes and chains | free                | offline runs and tests             |

Three rules do most of the work:

- an unavailable value is `None`, never `0`;
- a suspicious value is preserved and flagged, never corrected;
- a record is invisible to any reconstruction of a time before we retrieved it.

## Market research

```
UNIVERSE RUN             consumed, never re-selected
    |
POINT-IN-TIME EVIDENCE   DataRepository only; retrieval binds
    |
NEWS DEDUPLICATION       one story is one item, however often it was told
    |
RESEARCH INPUT           bounded, provenance-carrying, every fact has an id
    |
MARKET RESEARCHER        one isolated context per underlying
    |
DETERMINISTIC VALIDATION rejects a violating outlook in full, never repairs
    |
IMMUTABLE REPORT         appended, never overwritten
```

The output is a **structured outlook** — a hypothesis, the evidence behind it,
the events that matter, the risks, and what would prove it wrong. It is never
an option contract, never a strategy, never a position size and never an order.

### The hypotheses

| | Meaning | What the code requires |
| --- | --- | --- |
| `A` | Large move likely, **no specific catalyst required**, direction uncertain | `SUPPORTS_LARGE_MOVE` evidence that is *not* a dated event |
| `B` | Predominantly up | at least one `SUPPORTS_UP` supporting item |
| `C` | Predominantly down | at least one `SUPPORTS_DOWN` supporting item |
| `D` | Sharp move around a **specific identified event**, direction uncertain | a real event, inside the horizon, with its announcement recorded |
| `E` | Other | a structured explanation |

**`A` and `D` are different claims** — a regime versus a catalyst — and the
difference decides how a position would later be timed. An `A` justified by a
highly relevant event inside the horizon is rejected as a mislabelled `D`.

Six rules do the work, each with tests that fail loudly:

- **retrieval binds** — `research(as_of=T)` sees only what had been fetched by
  T; a future-dated *event* is legitimate once it was announced;
- **evidence is cited by id** — the agent has no field for a source name, a URL
  or a date, so an invented source is not merely discouraged, it is detectable;
- **the hypothesis must be earned** — see the table above;
- **confidence is constrained, not declared** — `HIGH` is refused, never
  quietly downgraded, when the evidence, the tiers, the data quality or an
  unresolved contradiction do not license it;
- **insufficient evidence is a valid outcome** — nothing is forced into `B` or
  `C`, and no failure is ever a market view;
- **no contract is expressible** — the agent is shown aggregate option context
  keyed by days to expiration, never a strike, an expiry or a right.

Source trust comes from `config/sources.yaml` and nowhere else. Where the
policy recognises a source, the configured tier wins over the provider's own
claim; an unlisted source keeps the tier it declared rather than being demoted
by a judgement nobody made.

## Strategy and contract selection

```
RESEARCH REPORT           consumed, never re-researched
    |
STRATEGY REGISTRY         the allow-list, resolved against config/risk.yaml
    |
DETERMINISTIC GATES       no eligible strategy, thin evidence, no chain -> NO_TRADE
    |
STRATEGY SELECTOR         chooses WHAT: one configured strategy, or none
    |
DETERMINISTIC VALIDATION  rejects a violating decision in full, never repairs
    |
STRATEGY DECISION         a proposal, never an order
    |
CONTRACT SELECTOR         chooses WHICH: arithmetic over a stored chain, no model
    |
CONTRACT SELECTION        concrete legs, or an explicit "no valid contract"
```

> **The AI selects the strategy. Deterministic code selects the contract.**

The agent is shown a research conclusion and the metadata of the strategies
eligible for its hypothesis — structure, leg shapes, DTE window. It is never
shown an option chain, a strike list, a contract id, an account balance or a
budget, and its response has no field for a strike, an expiration, a quantity
or a price. It cannot select a contract because it has neither the information
nor the vocabulary.

The hypothesis-to-strategy mapping is derived from each strategy's own
`applicable_hypotheses`, so there is exactly one place it is written down:

| | Eligible strategies |
| --- | --- |
| `A` | `LONG_STRADDLE`, `LONG_STRANGLE` |
| `B` | `LONG_CALL` |
| `C` | `LONG_PUT` |
| `D` | `LONG_STRADDLE`, `LONG_STRANGLE` (expiration aligned to the event) |
| `E` | none — `NO_TRADE` |

Six rules do the work, each with tests that fail loudly:

- **a strategy may narrow a global risk limit, never widen one** — the registry
  refuses to build otherwise, and does not clamp silently;
- **structure is code, policy is configuration** — a straddle is one call and
  one put on one strike because that is what the word means; the delta, the
  offsets and the DTE window are in `config/strategies/*.yaml`;
- **nothing is invented** — a missing delta is a rejection, not an estimate; an
  unquoted contract has an unknown cost, not a guessed midpoint; a trading class
  is copied from the chain, never derived from the ticker;
- **no contract is approximated** — "no valid contract" is a first-class
  outcome, and the nearest miss is recorded as a rejection rather than returned;
- **selection is reproducible** — same decision, same chain, same configuration,
  same `as_of` produces a byte-identical record, rejections included;
- **`NO_TRADE` is a first-class outcome** at both stages, and neither has to
  produce something because the stage above it did.

Milestone 6 ends at a **purchase candidate**: legs, and what one unit of the
structure would cost. There is no quantity and no allocation — those belong to
the risk and allocation engines, and no artifact here has a field for them.

## Allocation and risk

```
PURCHASE CANDIDATE        legs, and the cost of ONE unit
    |
ACCOUNT SNAPSHOT          captured once, stored, read back by id
CAMPAIGN SNAPSHOT         replayed from the allocation ledger
    |
RISK ENGINE               is this permitted?  APPROVED / REJECTED + reason codes
    |
ALLOCATION ENGINE         how many units?  floor(min(every ceiling))
    |
CAMPAIGN ALLOCATION       an authorisation, never an order
```

> **The campaign budget is independent of the broker account balance, and the
> most restrictive relevant limit always wins.**

```
IBKR paper account:   EUR 1,000,000    <- irrelevant to what may be spent
configured campaign:  EUR 5,000        <- the envelope
less a 20% reserve:   EUR 4,000        <- the most that may ever be committed
```

A large account balance can never widen the campaign. A small one *can* narrow
it: where the broker reports less available capital than the campaign permits,
the account binds instead.

**No AI is involved at any point in this milestone.** Neither engine has a
parameter, a field or an import through which a model could speak. Confidence
bands from validated upstream artifacts decide the *order* candidates are
considered in; they can never change a quantity, a limit or a permission — two
candidates scoring 99 and 71 receive the same size, and a test asserts it.

Limits form a hierarchy in which a child may narrow a parent and may never
widen one:

```
config/risk.yaml           GLOBAL     the outer boundary of the whole system
config/campaign.yaml       CAMPAIGN   what this campaign permits within it
config/strategies/*.yaml   STRATEGY   what this strategy permits within that
the candidate itself       POSITION   what one position may commit
```

Widening is a configuration **load failure**, never a silent clamp — a clamped
limit runs correctly and is invisible in the diff that introduced it.

Quantity is the floor of the tightest ceiling, computed in exact decimal and
then verified by multiplication:

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

It never rounds up and is never fractional. Every ceiling is recorded on the
stored decision, so "why two contracts and not three" is answerable from the
record without re-deriving anything.

Maximum loss comes from the strategy's own declared `MaxLossBasis`, not from a
generic formula: "max loss is the premium" is true of the four long-debit
strategies shipped today and false of the first credit spread anyone adds. A
basis the engine cannot compute is a rejection, not an estimate.

The broker is touched in exactly **one** place — `risk capture-account`, which
reads the account once and stores an immutable snapshot. The engines read it
back by id and hold no broker, which is a safety property as much as an
architectural one: a second uncached round trip on one IBKR connection can go
unanswered indefinitely (see [IBKR architecture](#ibkr-architecture)), so a
risk check that fetched its own account state could hang the process.

Milestone 7 ends at an **authorisation**, which is not an order. There is no
order type, no side, no limit price, no time-in-force and no broker order id
anywhere in the artifact, and tests assert their absence. Milestone 8 decides
how to execute an authorisation and stays bound by every figure in it.

Known limitations, stated rather than hidden:

- realised daily profit and loss is not tracked until Milestone 9, so the
  daily-loss limit is recorded as `NOT_EVALUATED` — never as passed;
- an authorisation that was never executed still consumes campaign budget;
  releasing stale reservations belongs to the milestone that learns whether an
  order filled — and Milestone 8 records *submission*, not settlement, so the
  gap is narrower but still open;
- the shipped EUR campaign refuses a USD-quoted contract with
  `CURRENCY_MISMATCH`, because no FX rate is invented. Either denominate the
  campaign in the currency it trades, or list the currency explicitly in
  `campaign.currency_policy.treat_as_campaign_currency`;
- correlation is not modelled. Concentration is explicit and countable: per
  underlying, per strategy, per direction, plus a position count.

## Execution

The first stage permitted to send an order, and the only one.

```
APPROVED CampaignAllocation
      |
      v
ExecutionRequest          a deliberate authorisation, never implied by an id
      |
      v
deterministic validation  windows, market session, currency, structure
      |
      v
PurchaseCard + RiskDecision   the Milestone 1 artifacts, minted not forked
      |
      v
OrderIntent               the one place a price becomes an instruction
      |
      v
idempotency check         has this exact trade already been sent?
      |
      v
Broker                    one submission, one short-lived connection
      |
      v
immutable record + append-only events
```

Nothing here re-decides anything. The quantity, the capital committed and the
maximum loss are copied from the authorisation; if the broker refuses because
the market moved, that is recorded as a failure and the trade needs a *new*
Milestone 7 authorisation. Execution never shrinks an order to fit.

Three distinctions carry the milestone:

**An acknowledgement is not a fill.** `SUBMITTED` means IBKR took the order.
Only an execution report produces `FILLED` or `PARTIALLY_FILLED`, and where a
broker's status disagrees with its own counts, the counts win — reporting three
of ten as `FILLED` would claim a position that does not exist. The
disagreement is recorded rather than smoothed over.

**"We did not send it" and "we do not know whether we sent it" are different
facts.** `FAILED` is reserved for attempts that provably never left the
process. Anything else — a timeout, a dropped connection — is `UNKNOWN`, and an
`UNKNOWN` execution is resolved by *observing* the broker. There is no code
path from an uncertain submission to a second one, and the configuration that
would enable one fails to load.

**A multi-leg structure is one order.** A straddle goes to IBKR as a combo, so
it fills as a structure or not at all. Submitting the legs independently could
leave a naked long call where a straddle was authorised — a position nobody
approved, against limits nobody checked for it — so that configuration is
refused at load time rather than at runtime.

```bash
python -m trading_system.cli execution validate   # the policy in force
python -m trading_system.cli execution run --dry-run   # builds an order, opens no broker
python -m trading_system.cli execution run --confirm   # SUBMITS
python -m trading_system.cli execution explain --execution-id <ID> --resolve
```

`--dry-run` never constructs a broker at all, which makes "a dry run cannot
place an order" structural rather than a flag someone has to check correctly.

One subtlety worth knowing before reading the code: Milestone 7 records the
cost of a structure as **money** (`ask x multiplier x ratio`, summed over legs),
while a broker limit price is a **quote**, per multiplier unit. The conversion
happens exactly once, in `order_builder.py`. Sending 605 where 6.05 was meant
is a hundredfold overpayment that every downstream number would faithfully
reproduce, so the record names the two apart rather than relying on a comment,
and rounding is always *down*.

Validating against a real paper account is opt-in and doubly gated — a
developer who unlocked the gateway for a read-only diagnostic must not thereby
have authorised an order:

```bash
make test-paper-execution   # submits ONE far-from-market paper order, then cancels it
```

That test handles the possibility that its order fills. "Extremely unlikely to
fill" is not "guaranteed not to fill", and if it does, the fill is reported
rather than hidden.

## IBKR architecture

```
application code
      |                    (never imports ib_async)
      v
  Broker  (broker/base.py)          abstraction + zero-order guard
      |
      +-- SimulatedBroker           deterministic, offline, labelled SIMULATED
      |
      +-- IBKRBroker                broker/ibkr/ — the only ib_async importer
              |
              +-- client.py         connection, account identity, health
              +-- positions.py      \
              +-- orders.py          |  pure translation to domain models,
              +-- executions.py      |  duck-typed, no ib_async import
              +-- market_data.py    /
              +-- conversion.py     NaN/sentinel handling, decimal-safe money
              +-- reconciliation.py compares internal vs broker state
```

**Client library: [`ib_async`](https://github.com/ib-api-reloaded/ib-async)**
— the maintained successor to the abandoned `ib_insync`. Chosen over the
official `ibapi` because `ibapi` is not reliably installable from PyPI (IBKR
distributes it as a download), and because `IB.connect(readonly=True)` gives a
broker-enforced order block that `ibapi` does not expose as directly.

Every IBKR response is translated into a domain model before it crosses the
`Broker` boundary. No `ib_async` type is visible outside `broker/ibkr/`, and a
test enforces that by parsing imports.

## Connecting to IBKR Paper

1. **Ports.** These four differ and are easy to confuse:

   | Setup            | Port   |                     |
   | ---------------- | ------ | ------------------- |
   | IB Gateway paper | `4002` | ← project default   |
   | IB Gateway live  | `4001` |                     |
   | TWS paper        | `7497` |                     |
   | TWS live         | `7496` |                     |

   Nothing hard-codes a port; set `IBKR_PORT`.

2. **Enable the API** in IB Gateway / TWS: *Configure → Settings → API →
   Enable ActiveX and Socket Clients*, and add `127.0.0.1` to trusted IPs.

3. **Configure `.env`** (see `.env.example` for every variable):

   ```bash
   TRADING_MODE=PAPER
   IBKR_HOST=127.0.0.1
   IBKR_PORT=4002
   IBKR_CLIENT_ID=1
   IBKR_ACCOUNT=DU1234567     # required if the login manages >1 account
   IBKR_READ_ONLY=true
   IBKR_MARKET_DATA_TYPE=3    # delayed; needs no subscription
   ```

4. **Verify:**

   ```bash
   python -m trading_system.cli test ibkr-connection
   python -m trading_system.cli test ibkr-portfolio
   ```

If the gateway is not running you get a `FAIL` with a diagnostic and exit `1`.
No placeholder account or position data is ever substituted.

## Docker

```bash
docker compose build
docker compose up -d              # starts ib-gateway, then trading-runtime
docker compose logs -f trading-runtime
docker compose ps                 # health status of both services
docker compose down
```

`ib-gateway` runs IB Gateway + IBC with a TCP healthcheck; `trading-runtime`
waits for it to become healthy, then runs the read-only connection test. The
runtime image runs as a non-root user and includes the `ibkr` extra.
Credentials come from the environment — none are in `docker-compose.yml`.

**Verified:** the `trading-runtime` image builds, and `health` and
`test ibkr-connection --simulated` run correctly inside the container. The
`ib-gateway` service has **not** been started — that needs real IBKR
credentials.

## Testing

```bash
make test              # whole suite
make test-unit         # deterministic unit tests
make test-broker       # broker abstraction, simulator, IBKR adapter
make test-data         # providers, quality, point-in-time, storage
make test-universe     # filters, point-in-time, snapshots, reproducibility, CLI
make test-research     # evidence, deduplication, point-in-time, validation, CLI
make test-strategy     # registry, agent boundary, decision validation, service, CLI
make test-strategies   # one suite per strategy specification
make test-contract-selection  # policy, point-in-time, determinism, boundaries
make test-risk         # limits, engine, account snapshots, boundaries
make test-allocation   # quantity, allocator, scorer, service, CLI
make test-allocation-unit         # the engine arithmetic alone
make test-allocation-integration  # research -> allocation, end to end
make test-execution    # state machine, idempotency, fills, boundaries, CLI
make test-execution-unit          # the pure layers alone
make test-execution-integration   # research -> execution, end to end
make test-agents       # AI agent contract tests; needs no API key
make test-contract     # workflow-boundary schema compatibility
make test-integration  # multi-component, simulated broker
pytest tests/unit/test_state_machine.py            # one file
pytest tests/unit/test_state_machine.py::test_name # one test
pytest -m "not ibkr and not llm"                   # exclude gateway and model tests
make lint typecheck    # ruff + mypy
```

`pytest` needs **no** IB Gateway and **no** API key. Tests requiring one are
marked `ibkr` (or `paper`, or `llm`) and are skipped unless unlocked:

```bash
ALLOW_LIVE_TESTS=true pytest -m ibkr            # requires a running gateway
ALLOW_LIVE_TESTS=true pytest -m ibkr tests/data # data layer against IBKR Paper
ALLOW_LIVE_TESTS=true ANTHROPIC_API_KEY=... \
  pytest -m llm tests/agents/test_research_agent.py
ALLOW_LIVE_TESTS=true ANTHROPIC_API_KEY=... \
  pytest -m llm tests/agents/test_strategy_selector.py
```

Those tests are read-only and assert `orders_submitted == 0`. They fail loudly
if the gateway is unreachable — they never fake a pass.

**One test can place a real order**, and it needs a second variable of its own:

```bash
ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true IBKR_READ_ONLY=false \
  pytest -m paper_execution -s        # or: make test-paper-execution
```

Two variables rather than one is the point: unlocking the gateway for a
read-only diagnostic must not also authorise an order. It refuses to run in any
mode but PAPER, submits exactly one far-from-market order, cancels it, verifies
the cancellation, and reports what actually happened — including a fill, if the
order fills despite being priced not to.

The opt-in `llm` tests in `tests/agents/test_research_agent.py` and
`tests/agents/test_strategy_selector.py` each make **one** real model call
against the shipped prompt and the shipped schema. They are doubly gated
(`ALLOW_LIVE_TESTS=true` *and* a key must be present), touch no broker, place
no order, and assert only structure — never that the model reached a particular
conclusion, because that is not a property of the system. A validation failure
there is the deterministic layer catching a real model's real mistake, which is
its job.

## Safety properties

Enforced in code and covered by tests, not left to convention:

- **Submitting an order takes three deliberate acts**, and no two of them are in
  the same place: `execution.enabled: true` in `config/execution.yaml`,
  `IBKR_READ_ONLY=false` in the environment, and `--confirm` on
  `execution run`. Each ships in the safe position. `Broker.place_order` stays
  `@final` and refuses while read-only, so the guard cannot be subclassed away.
- **Only the execution service can obtain a writable broker.**
  `build_broker` — what every diagnostic, the data layer and every upstream
  stage calls — returns a read-only connection whatever the settings say.
  `build_execution_broker` is the only alternative, and a test asserts the
  execution service is its only caller. Research, strategy, contract selection,
  risk and allocation cannot import the broker package at all.
- **Three independent order blocks** on a read-only connection: the
  application's `read_only` flag, `IB.connect(readonly=True)` so IBKR itself
  rejects orders, and `READ_ONLY_API=yes` on the gateway container.
- **An acknowledgement is never reported as a fill**, and an ambiguous
  submission is never retried. A client timeout after a send is `UNKNOWN` — the
  order may be live — and resolution is by asking the broker, never by sending
  again. `auto_retry_on_timeout: true` fails to load.
- **The same trade cannot be submitted twice.** Submission identity is derived
  from the authorisation and how it would be sent, and any prior attempt that
  *may* have reached the broker blocks another.
- **`pytest` cannot place an order.** The one test that submits needs both
  `ALLOW_LIVE_TESTS=true` and `RUN_PAPER_EXECUTION_TESTS=true`, and refuses to
  run in any mode but PAPER.
- **Every read-only command proves it submitted nothing**, printing a count
  read from the broker. Tests run each command against a *writable*
  mutation-recording broker and assert nothing was even attempted.
- **LIVE is unreachable**: refused by `Settings`, again by the broker factory,
  and again in `IBKRBroker.__init__`.
- **No invented data.** Missing broker values stay `None`, never zero.
  Unavailable quotes raise `MARKET_DATA_UNAVAILABLE` rather than returning a
  price. Simulated data is stamped `SIMULATED` and cannot be read as live.
- **No invented history.** A suspicious value is preserved and flagged, never
  corrected. A snapshot is immutable and hash-verified on read. A record is
  invisible to any reconstruction of a time before it was retrieved.
- **Bounded broker requests.** Every IBKR request carries a timeout;
  `ib_async`'s default is to wait forever, and only the first uncached round
  trip per connection is reliably answered. Data retrievals open one
  short-lived connection each.
- **The broker is authoritative.** Reconciliation reports discrepancies and
  blocks new executions; it never resolves them or trades to correct them.
- **The AI cannot widen its own remit.** The universe agent is handed a
  validated contract that cannot express a rejected asset, and everything it
  returns is checked against that contract before storage. It has no field for
  a strike, a direction or an allocation, and no reason code for one either.
  The research agent has no field for a strike, an expiry, a right, a delta, a
  strategy, a quantity or a currency amount, and is never shown a contract.
  The strategy agent is offered only the strategies the registry admitted for
  the hypothesis — a strategy outside that list is not expressible in its
  response schema — and is shown no option chain, no account and no budget.
- **The AI never selects a contract.** The strike, the expiration and the legs
  are resolved by `strategies/contract_selector.py`, which imports no agent, no
  LLM client and no news provider; a test asserts that transitively. Identical
  inputs produce an identical selection, rejections included. No model is
  consulted per selection, and certainly not per strike.
- **A strategy specification cannot overrule the risk policy.** A strategy may
  narrow the DTE window, the price band, the liquidity floors or the spread
  ceiling in `config/risk.yaml`; widening one is refused at configuration load
  *and* at registry build, and is never clamped silently.
- **The AI cannot invent a source.** Research cites evidence by an id the input
  actually carried, and has no field for a source name, a URL or a publication
  date — those are copied from the input into the report. An unknown id rejects
  the whole outlook.
- **An AI failure is never a silent success.** An unreachable model, a refusal,
  a truncated generation, malformed JSON, a fabricated justification or an
  unsupported hypothesis each end with a named status and **no view** — no
  hypothesis, no confidence, no catalysts, and at the strategy stage no
  strategy. There is no deterministic fallback for a market outlook or for a
  strategy choice, by design.
- **Nothing is approximated into existence.** A missing delta fails the
  selection rather than being estimated; an unquoted contract has an unknown
  cost rather than an invented midpoint; a chain too coarse for the configured
  strike policy yields no contract rather than the nearest one.
- **Confidence is not the model's to declare.** `HIGH` is refused, never
  quietly downgraded, when the evidence count, the source tiers, the data
  quality or an unresolved contradiction do not license it.
- monetary fields reject binary floats; naive datetimes are rejected; illegal
  state transitions raise; an `APPROVED` risk decision cannot carry a rejection
  reason; `NO_TRADE` is valid at every stage; the allocator's books must
  balance.

## Trading modes

| Mode      | Meaning                                          |
| --------- | ------------------------------------------------ |
| `DRY_RUN` | Nothing leaves the process                        |
| `PAPER`   | IBKR paper account — **the default**              |
| `LIVE`    | Real money — refused unless explicitly unlocked   |

`TRADING_MODE=LIVE` on its own is **rejected at startup**. It additionally
requires `LIVE_TRADING_CONFIRMED=true` and
`LIVE_READINESS_CHECKLIST_SIGNED_OFF=true`. No prompt, agent or model output
can change the trading mode.

## Configuration

Two separate concerns, deliberately split:

- **`.env`** — secrets and deployment switches. Never committed.
- **`config/*.yaml`** — trading policy: campaign budget, risk limits, schedules,
  source trust tiers, strategy definitions, the data layer's freshness windows,
  plausibility bounds, research-usability policy and market calendar, the
  universe's candidate pool, eligibility filters and ranking policy, the
  research horizon, data windows, cost ceilings and confidence policy, and the
  strategy stage's eligibility gates plus the deterministic contract-selection
  policy. Committed and reviewable, so a change to a risk limit shows up in a
  diff.

Note the deliberate split between `config/strategy.yaml` — how the strategy
*stage* behaves — and `config/strategies/*.yaml` — what each strategy *is*. The
first configures an agent; the second configures a payoff.

Monetary values in YAML are quoted strings or integers (`"0.50"`, `5000`) so
they parse as exact decimals. An unquoted `0.50` is a binary float and is
rejected by design.

## Repository layout

```
config/            trading policy (committed, reviewable)
schemas/           JSON Schema for each workflow boundary
src/
  broker/          Broker abstraction, simulator, IBKR adapter
  data/            providers, normalizers, quality, snapshots, repository
  domain/          models, enums, events, state machine (pure, no I/O)
  universe/        candidate pool, pre-filter, ranking, immutable runs
  research/        point-in-time evidence, hypothesis validation, immutable reports
  strategies/      registry, strategy structures, deterministic contract selector
  risk/            deterministic risk engine, limits, exposure, account snapshots
  allocation/      deterministic campaign allocation, scoring, immutable ledger
  execution/       purchase card, order builder, submission, fills, execution ledger
  agents/          LLM agents and their shipped prompts
  infrastructure/  settings, config loading, logging, clock
skills/            strategy specifications and development guidance (never executable)
data/              market data, snapshots and caches (contents git-ignored)
trades/            immutable per-trade artifact directories (contents git-ignored)
reports/           generated reports (contents git-ignored)
tests/             unit, broker, contract, integration and per-component suites
```
