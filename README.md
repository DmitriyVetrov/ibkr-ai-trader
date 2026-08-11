# Autonomous Options Trading System

A modular, testable, stateful options-trading system for Interactive Brokers.

This is a **paper-trading research system**. Live trading is not enabled and is
not reachable by configuration alone — see [Trading modes](#trading-modes).

> AI proposes. Deterministic modules validate. Broker executes.
> Broker state is authoritative.

## Status

**Milestone 4 of 12 — universe selection.** What exists today:

- domain models, enums, events and the position state machine;
- YAML configuration for campaign, risk, schedules, sources, strategies and data;
- the 14 JSON schemas for the workflow boundaries;
- structured logging and an injectable clock;
- a `Broker` abstraction with a deterministic `SimulatedBroker` and a
  **read-only** `IBKRBroker` over IB Gateway;
- read-only diagnostics for connection, portfolio, market data and option chain;
- the reconciliation foundation — detects discrepancies, never resolves them;
- **the data layer**: provider interfaces and free providers, canonical models,
  an eight-dimension quality engine, immutable point-in-time snapshots, an
  append-only historical ledger, and a repository behind an interface;
- **universe selection**: a configurable candidate pool, a deterministic
  eligibility pre-filter, the first AI agent, deterministic validation of what
  it returns, and immutable append-only universe runs;
- the CLI surface, with every command tagged read-only or state-mutating.

What deliberately does **not** exist yet: research reasoning, strategy
selection, contract selection, allocation, order execution, autonomous trading
and any form of live trading. Commands covering those exist in the CLI but exit
`3` naming the milestone that delivers them. They never fabricate output.

**No code path in this repository can submit an order.** See
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
make test-agents       # AI agent contract tests; needs no API key
make test-contract     # workflow-boundary schema compatibility
make test-integration  # multi-component, simulated broker
pytest tests/unit/test_state_machine.py            # one file
pytest tests/unit/test_state_machine.py::test_name # one test
pytest -m "not ibkr"                               # explicitly exclude gateway tests
make lint typecheck    # ruff + mypy
```

`pytest` needs **no** IB Gateway. Tests requiring one are marked `ibkr` (or
`paper`) and are skipped unless unlocked:

```bash
ALLOW_LIVE_TESTS=true pytest -m ibkr            # requires a running gateway
ALLOW_LIVE_TESTS=true pytest -m ibkr tests/data # data layer against IBKR Paper
ALLOW_LIVE_TESTS=true pytest -m llm             # one real model call; places no trades
```

Those tests are read-only and assert `orders_submitted == 0`. They fail loudly
if the gateway is unreachable — they never fake a pass.

## Safety properties

Enforced in code and covered by tests, not left to convention:

- **No order can be submitted.** `Broker.place_order` is `@final`, refuses
  while read-only, and its submission hook raises `NOT_IMPLEMENTED` in every
  implementation. Order execution arrives in Milestone 8.
- **Three independent order blocks**: the application's `read_only` flag,
  `IB.connect(readonly=True)` so IBKR itself rejects orders, and
  `READ_ONLY_API=yes` on the gateway container.
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
- **An AI failure is never a silent success.** An unreachable model, a refusal,
  a truncated generation, malformed JSON or a fabricated justification each end
  the run with a named status and an empty universe.
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
  plausibility bounds, research-usability policy and market calendar, and the
  universe's candidate pool, eligibility filters and ranking policy. Committed
  and reviewable, so a change to a risk limit shows up in a diff.

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
  agents/          LLM agents and their shipped prompts
  infrastructure/  settings, config loading, logging, clock
data/              market data, snapshots and caches (contents git-ignored)
trades/            immutable per-trade artifact directories (contents git-ignored)
reports/           generated reports (contents git-ignored)
tests/             unit, broker, contract, integration and per-component suites
```
