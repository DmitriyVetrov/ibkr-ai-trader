# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**Milestones 1–2 of 12 are complete.** Built and tested: the domain layer (models, enums,
events, state machine), YAML configuration, the 11 JSON workflow schemas, the CLI surface,
structured logging, an injectable clock, the `Broker` abstraction with `SimulatedBroker` and a
read-only `IBKRBroker`, the read-only broker diagnostics, and the reconciliation foundation.
588 passing tests; ruff, ruff format and mypy clean.

**Not built, by design:** data providers, AI agents, order execution, autonomous trading, live
trading. The CLI exposes those commands but they exit `3` naming the milestone that delivers
them — they never fabricate output. Follow that pattern for anything still pending.

**Milestone 3 is next: the data layer** — provider interfaces, free sources, caching,
normalization, snapshots, data quality.

[CLOUD_CODE_IMPLEMENTATION_SPEC.md](CLOUD_CODE_IMPLEMENTATION_SPEC.md) remains the source of
truth for module layout, CLI surface, schemas, testing layers, and milestone order. Read the
relevant section before creating files rather than inventing a design. Everything below is a
summary of the load-bearing parts, not a replacement for it.

Build order is spec §46 (Milestones 1–12). Package directories for later milestones exist with
only a docstring naming their milestone.
That is deliberate: a stub that pretends to work is worse than an absent module (spec §48.3).

## The core architectural rule

> AI proposes. Deterministic modules validate. Broker executes. Broker state is authoritative.

```
AI AGENTS  →  DETERMINISTIC DECISION LAYER  →  EXECUTION ENGINE  →  IBKR  →  RECONCILIATION  →  PERSISTENT STATE
   propose        validate / allocate / limit        submit         reality      compare          historical truth
```

Never reverse this hierarchy. LLMs may research, classify a market hypothesis, pick an allowed
strategy, explain decisions, and flag thesis invalidation. LLMs must **never** determine budget,
risk limits, position size, or whether an order is permitted — those are deterministic modules
(`allocation/`, `risk/`), and an AI agent cannot override a risk-engine rejection.

Contract selection is deterministic too (spec §8): the strategy agent picks a *strategy*, the
`contract_selector` picks the actual strike/expiry from the chain.

## Non-negotiable invariants

These are the rules most likely to be violated by a plausible-looking change:

- **`TRADING_MODE=PAPER` is the default.** Modes are `DRY_RUN` / `PAPER` / `LIVE`. LIVE requires
  explicit configuration plus multiple safety checks, and no LLM prompt may switch modes.
- **Tests never place orders.** Read-only broker tests must assert `orders_submitted = 0`. Anything
  capable of submitting even a Paper order requires `ALLOW_LIVE_TESTS=true`, which is never the
  default. Live credentials never appear in tests.
- **IBKR is authoritative.** If the database and IBKR disagree about positions, orders, quantities,
  or fills, IBKR wins. Discrepancy → `RECONCILIATION_ERROR` → block new executions until resolved.
- **`NO_TRADE` is a first-class outcome** at every decision stage. Never trade merely because a
  universe or candidate list was produced, and never spend the whole campaign budget merely because
  candidates exist.
- **Fail safe.** Unknown broker state, stale option chain, unavailable risk engine, reconciliation
  failure, or LIVE without explicit safety config → do not trade.
- **Money is decimal-safe.** No binary floating point for accounting decisions.
- **Time is timezone-aware and UTC internally.** Exchange-local time only at the scheduling and
  presentation boundary. Preserve `as_of` / `source` / `retrieved` timestamps on every snapshot.
- **Never invent market data, API responses, or sources.** An agent may not cite a source it did
  not actually retrieve. Historical evaluation must not use future information (look-ahead bias).
- **Determinism where claimed.** Identical inputs must produce identical allocation output.
- Secrets (`IBKR_USERNAME`, `IBKR_PASSWORD`, `IBKR_ACCOUNT`, `ANTHROPIC_API_KEY`,
  `TELEGRAM_BOT_TOKEN`) come only from env/secret storage; ship `.env.example` with placeholders.

## Architecture shape

Python 3.12+ (3.13.5 is installed locally). Package root `src/trading_system/`. See spec §3 for the
full tree; the boundaries that matter:

- `domain/` — models, enums, events, and the position state machine
  (`DISCOVERED → RESEARCHED → STRATEGY_SELECTED → CONTRACT_SELECTED → ALLOCATED → RISK_APPROVED →
  ORDER_SUBMITTED → PARTIALLY_FILLED → OPEN → MONITORING → EXIT_TRIGGERED → CLOSING → CLOSED`, plus
  terminal `NO_TRADE / REJECTED / CANCELLED / FAILED / EXPIRED`). Transitions are persisted.
- `agents/` — the six LLM agents (universe selector, market researcher, options strategist, thesis
  monitor, position manager, evaluation analyst). Agents are isolated from broker mutation APIs.
- `broker/` — a `Broker` abstraction with `IBKRBroker` and `SimulatedBroker` implementations.
  Application code must never call low-level IBKR APIs directly. `broker/ibkr/` is the **only**
  package allowed to import `ib_async`; a test asserts this. The translation modules
  (`positions`, `orders`, `executions`, `market_data`) are pure functions over duck-typed
  objects and import nothing from the library, which is what lets them be tested without a
  gateway.
- `risk/`, `allocation/`, `strategies/contract_selector.py` — the deterministic layer.
- `monitoring/` — position monitor, thesis monitor, reconciliation loop, scheduler.

**Two loops, different cadences.** The Opportunity Discovery loop (universe → research → strategy →
contract → rank → allocate → risk → execute) is slow. The Position Management loop (reconcile →
snapshot → risk → exit policy → wait/exit) is frequent. Do not run full research on every
monitoring tick — that is what the separate Thesis Monitor is for, and it returns
`VALID / WEAKENING / INVALIDATED / UNKNOWN`. The original thesis is immutable; new evidence is
appended, never rewritten in place.

**Multi-leg strategies are managed as one position.** Trailing stops and exits apply at the strategy
level; a single leg is closed only if that strategy's specification explicitly permits it.

**Configuration over hardcoding.** Schedules live in `config/schedules.yaml`, risk in `risk.yaml`,
strategies in `config/strategies/*.yaml`. DTE policy is per-strategy, not one universal number.
Ports come from config. If a requirement is ambiguous, add a documented config option rather than a
hidden assumption.

## Data and persistence layout

`data/` separates `raw/` (verbatim provider responses), `normalized/` (canonical form), `cache/`
(regenerable), `historical/`, and `snapshots/` (immutable point-in-time). Never mix cache with
research snapshots.

Every closed trade gets an immutable `trades/closed/<trade_id>/` directory holding the full chain of
artifacts (research, strategy decision, purchase card, allocation, risk decision, entry/exit market
snapshots, order intent, execution, position history, thesis updates, final result) plus version
stamps (app, strategy spec, config, prompt/agent, model id). The goal is reconstructing *why* a
trade happened without consulting LLM memory.

Persistence starts as SQLite + filesystem snapshots but sits behind repository interfaces so
PostgreSQL remains possible. Do not couple business logic to SQL.

Every workflow boundary has a JSON schema in `schemas/`. Agent output must validate against it, and
contract tests verify each producer's output is consumable by the next stage.

## Commands

Dependencies live in `.venv/` (created with `uv venv`, then `uv pip install -e '.[dev]'`). Prefix
with `.venv/bin/` or activate first. `make help` lists every target.

```bash
make test                                # whole suite (make check = lint + typecheck + test)
.venv/bin/pytest tests/unit              # one layer: unit / contract / risk / allocation / …
.venv/bin/pytest tests/unit/test_state_machine.py             # one file
.venv/bin/pytest tests/unit/test_state_machine.py::test_name  # one test
.venv/bin/ruff check src tests && .venv/bin/mypy              # both currently clean

python -m trading_system.cli --help      # exposes: run, test, data, portfolio, positions,
                                         # research, opportunities, reconcile, reports, health
python -m trading_system.cli health      # works today: config + mode + schema check
python -m trading_system.cli config      # works today: validate and print configuration

# Read-only broker diagnostics. Append --simulated to run without a gateway.
python -m trading_system.cli test ibkr-connection
python -m trading_system.cli test ibkr-portfolio
python -m trading_system.cli test ibkr-market-data --symbol SPY
python -m trading_system.cli test ibkr-option-chain --symbol SPY
python -m trading_system.cli test reconciliation

pytest -m "not ibkr"                     # default: no gateway needed
ALLOW_LIVE_TESTS=true pytest -m ibkr     # requires a running IB Gateway
```

Every other command exits `3` until its milestone lands. Command help is tagged `(read-only)` or
`(mutates state)` — keep that up when adding commands, and note that Rich swallows
`[square brackets]` in help strings, hence the parentheses. Makefile shortcuts mirror the test
layout (`make test-risk`, `make ibkr-connection`). `docker-compose.yml` and `Dockerfile` build and
run `trading-runtime`; `ib-gateway` itself has not been started against a real account.

## Working in this repository

Run the relevant tests after each implementation step, and add tests alongside every module — every
new agent needs its own suite. Agent tests validate structured semantics (enum membership,
confidence bounds, required fields, presence of sources and invalidation conditions, no-trade
behavior), never exact prose. Use recorded fixtures; ordinary unit tests make no live web or broker
calls. Use the simulated broker by default in integration tests.

Prefer free data sources during the initial experiment; do not introduce paid providers without the
owner's approval. Do not generate placeholder implementations that pretend to connect to IBKR —
use interfaces and mocks, and keep mock / simulator / Paper / Live behavior clearly distinguished.

### Conventions worth keeping

- **Money in YAML must be quoted** (`"0.50"`) or an integer. An unquoted `0.50` is a binary float
  and `load_config` rejects it — that rejection is tested, not incidental.
- **Timestamps come from a `Clock`**, never a bare `datetime.now()`. `FixedClock` keeps tests
  deterministic and lets historical replay run "as of" a past instant.
- **New config keys need a model field.** Config models are `extra="forbid"`, so a typo in
  `risk.yaml` fails loudly instead of silently doing nothing.
- **Version stamps**: bump `config_version` in `config/application.yaml` whenever a change to
  `config/` would alter a decision — it is recorded in every trade artifact.
- **`Broker.place_order` is `@final`.** Subclasses implement `_submit_order` instead, so the
  read-only check and the submitted-order counter cannot be bypassed. Do not un-final it to make
  execution "easier" in Milestone 8 — add the hook implementation.
- **IBKR reports missing numbers as `NaN` or a `DBL_MAX` sentinel, not `None`.** Everything from
  the broker goes through `broker/ibkr/conversion.py`, which maps those to `None` and converts
  via `Decimal(str(x))`. A missing value must never become `0`: "no margin data" and "zero
  margin" are different facts.
- **`project_root()` must keep working for an installed package**, not just a repo checkout —
  in the container the package lives in `site-packages` and `config/`/`schemas/` sit at the
  working directory. `PROJECT_ROOT` overrides it.
- **`filterwarnings = ["error"]` needs an exemption for `ib_async`'s event-loop
  `DeprecationWarning`**, or every gateway-backed test dies with a misleading "There is no
  current event loop" instead of connecting.
- **Only the first live, uncached IBKR request/response round trip on a freshly opened
  connection is reliably answered against the validated TWS environment.** A second explicit
  request on the same connection (confirmed with `reqCurrentTime`, reproduced at the raw-socket
  level) can go unanswered forever even though the connection is healthy and the first request
  worked — this held across a TWS restart, so it is not a stuck/stale-session artifact. Data
  covered by `ib_async`'s `StartupFetchALL` handshake cache (account summary, positions, open
  orders, fills) is unaffected, since those calls are served from the cache rather than a fresh
  round trip. Do not assume arbitrary sequential uncached IBKR requests are reliable. Prefer, in
  order: (1) `StartupFetchALL`/cache-backed data over a fresh request; (2) batching a connection's
  distinct data needs into the handshake's cache rather than issuing them one by one; (3)
  one-purpose connections — open a fresh connection per uncached live call where a second live
  round trip on one connection can't be avoided; (4) an explicit timeout bound on every broker
  request (`ib_async`'s sync wrappers default to `RequestTimeout = 0`, i.e. unbounded — see
  `IBKRBroker.health_check`'s `reqCurrentTimeAsync` + `util.run(timeout=...)` pattern), so a
  request that never answers fails safe instead of hanging the process. `IBKRBroker.connect()`
  deliberately does not spend the connection's one reliable round trip on its own health probe —
  see `health_check(probe_latency=False)`.
- **Simulated data is stamped `SIMULATED`/`SIMULATOR`** on every snapshot and position. Never
  let simulated or delayed data be presented as a live broker quote, and never fall back from
  real data to a synthesized value — raise `MarketDataUnavailableError` instead.

This directory is its own git repository (`git init`-ed, one commit: the specification). The
enclosing `/home/dmytro/git/` is a separate repo full of unrelated projects; nothing here should
ever be staged into it.
