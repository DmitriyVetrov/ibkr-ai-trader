# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**Milestone 1 of 12 (project skeleton) is complete.** Built and tested: the domain layer
(models, enums, events, state machine), YAML configuration, the 11 JSON workflow schemas, the
CLI surface, structured logging, an injectable clock, and 325 passing tests.

**Not built, by design:** IBKR connectivity, data providers, AI agents, order execution, live
trading. The CLI exposes those commands but they exit `3` naming the milestone that delivers
them — they never fabricate output. Follow that pattern for anything still pending.

[CLOUD_CODE_IMPLEMENTATION_SPEC.md](CLOUD_CODE_IMPLEMENTATION_SPEC.md) remains the source of
truth for module layout, CLI surface, schemas, testing layers, and milestone order. Read the
relevant section before creating files rather than inventing a design. Everything below is a
summary of the load-bearing parts, not a replacement for it.

Build order is spec §46 (Milestones 1–12). **Milestone 2 is next: broker connectivity** — the
`Broker` abstraction, `SimulatedBroker`, `IBKRBroker`, the read-only connection and portfolio
tests, and reconciliation.

Package directories for later milestones exist with only a docstring naming their milestone.
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
  Application code must never call low-level IBKR APIs directly.
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
```

Every other command exits `3` until its milestone lands. Command help is tagged `(read-only)` or
`(mutates state)` — keep that up when adding commands, and note that Rich swallows
`[square brackets]` in help strings, hence the parentheses. Makefile shortcuts mirror the test
layout (`make test-risk`, `make ibkr-connection`). `docker-compose.yml` and `Dockerfile` are
Milestone 2 scaffolding, not yet exercised.

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

This directory is its own git repository (`git init`-ed, one commit: the specification). The
enclosing `/home/dmytro/git/` is a separate repo full of unrelated projects; nothing here should
ever be staged into it.
