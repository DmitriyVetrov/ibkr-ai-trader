# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**Milestones 1–4 of 12 are complete.** Built and tested: the domain layer (models, enums,
events, state machine), YAML configuration, the 14 JSON workflow schemas, the CLI surface,
structured logging, an injectable clock, the `Broker` abstraction with `SimulatedBroker` and a
read-only `IBKRBroker`, the read-only broker diagnostics, the reconciliation foundation, the
data layer (providers, canonical models, quality engine, point-in-time snapshots,
append-only history, repository), and **universe selection** — the deterministic pre-filter,
the first AI agent, and immutable universe runs. 1304 passing tests; ruff, ruff format and
mypy clean.

**Not built, by design:** research reasoning, strategy selection, contract selection,
allocation, order execution, autonomous trading, live trading. The CLI exposes those commands
but they exit `3` naming the milestone that delivers them — they never fabricate output.
Follow that pattern for anything still pending.

**Milestone 5 is next: research** — the market researcher agent, the source trust policy, and
a structured research report. It consumes the universe; it does not re-select one.

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
  An agent depends on `LLMClient`, a protocol — never a vendor — so a fake, a replayed fixture
  and a live model are one code path and no test needs a credential. A test parses imports and
  asserts no agent module reaches a broker, a provider, a repository or a socket, directly *or
  transitively*. `anthropic` is an optional extra imported lazily, exactly like `ib_async`.
- `broker/` — a `Broker` abstraction with `IBKRBroker` and `SimulatedBroker` implementations.
  Application code must never call low-level IBKR APIs directly. `broker/ibkr/` is the **only**
  package allowed to import `ib_async`; a test asserts this. The translation modules
  (`positions`, `orders`, `executions`, `market_data`) are pure functions over duck-typed
  objects and import nothing from the library, which is what lets them be tested without a
  gateway.
- `data/` — the pipeline `providers → raw → normalization → quality → snapshot → historical →
  repository`. Consumers see `DataRepository` and canonical records, never a path or a raw
  provider payload. `data/service.py` is the composition root that the CLI and the future
  scheduler both use, so a command and a scheduled job cannot get differently configured
  pipelines. The data layer must never import `ib_async` or open its own IBKR connection —
  it goes through the Milestone 2 broker adapter; a test enforces both.
- `universe/` — the bridge from data to AI. `source → evidence → deterministic pre-filter →
  agent input contract → AI ranking → deterministic validation → immutable run`. It reads
  through `DataRepository` only; it constructs no broker and has no reachable order path, so
  "zero orders" is structural rather than checked. `universe/service.py` is the composition
  root the CLI and the future scheduler share.
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

## Universe selection (Milestone 4)

The universe selector answers exactly one question — *which underlying assets deserve deeper
research?* — and the model shapes are what stop it answering another. Its output is a list of
**underlyings**; there is no field anywhere in `universe/models.py` for a strike, an expiry, a
right, a direction, a strategy or an amount of money, and tests assert their absence.

Five rules govern it, each with tests that fail loudly:

- **A deterministic exclusion is final.** The agent runs after the pre-filter and can only
  reorder what survives. This is structural, not prompted: `UniverseSelectionInput` refuses to
  carry a rejected asset, so an excluded symbol is not merely unlikely to be ranked, it is not
  expressible in the agent's input. Naming one is indistinguishable from inventing one.
- **A violating response is rejected in full, never repaired.** Not an unknown symbol dropped,
  not a duplicate rank renumbered, not an over-long selection truncated. Repairing would store
  a universe the model did not choose while recording it as the model's output — a fiction that
  looks exactly like a clean run. `AI_INVALID_OUTPUT`, and nothing is selected.
- **Fail closed.** An unreachable or unusable model ends the run with a status naming what
  happened and *no* selected assets. A deterministic ordering is substituted only when
  `allow_deterministic_fallback` is explicitly set, and such runs are stamped
  `DETERMINISTIC_ONLY` so the record never implies a model was involved.
- **`UNKNOWN` optionability is never coerced.** Not to `TRUE`, not to `FALSE`. A chain snapshot
  with expirations means `TRUE`; one with none means `FALSE`; no visible chain means `UNKNOWN`.
  Policy decides whether unresolved may proceed, and the unresolved state travels with it.
- **Underlying liquidity is not option liquidity.** `underlying_volume` is share volume. It
  does not establish that an option is liquid — that needs option-level data this milestone
  does not collect. The wording is enforced in a test.

Reason codes are a closed vocabulary checked against the candidate's own evidence: claiming
`OPTIONS_AVAILABLE` for an `UNKNOWN` asset, or any liquidity code with no volume figure,
invalidates the response. Only *facts* are enforced — which liquidity band applies is the
agent's judgement and is not second-guessed.

Runs are immutable and append-only under `data/universe/`, with a content-derived `run_id`, so
a re-run over unchanged inputs is idempotent and "what did we consider on date T, and why"
stays answerable after the config, the data and the model have all moved on.

**Configuration over hardcoding.** Schedules live in `config/schedules.yaml`, risk in `risk.yaml`,
strategies in `config/strategies/*.yaml`, data policy in `data.yaml`, and the candidate pool,
eligibility filters and ranking policy in `universe.yaml`. DTE policy is per-strategy, not one
universal number. Ports come from config. If a requirement is ambiguous, add a documented config
option rather than a hidden assumption.

## Data and persistence layout

`data/` separates `raw/` (verbatim provider responses), `normalized/` (canonical form), `cache/`
(regenerable), `historical/` (an append-only ledger per data type and key), and `snapshots/`
(immutable point-in-time). Never mix cache with research snapshots. See
[data/README.md](data/README.md).

Three rules govern everything stored there, and each has tests that fail loudly:

- **An unavailable value is `None`, never `0`.** "No implied volatility" and "IV = 0" are
  different claims about a market.
- **A suspicious value is preserved and flagged, never corrected.** Real paper validation
  returned an implausible SPY volume. Silently fixing it invents data; dropping the record
  destroys the evidence that the feed misbehaved. So: raw value kept, `SUSPICIOUS_VOLUME`
  recorded, `plausibility_valid=False`, `research_usable=False`. A crossed quote is flagged,
  never un-crossed. Quality has **eight independent dimensions**, not one boolean, because a
  record can be perfectly well-formed and economically impossible at once.
- **A record is invisible to any reconstruction of a time before it was retrieved.** Retrieval
  binds, not publication: a filing published in 2019 and downloaded today did not inform last
  week's decision. Future-dated *content* is fine — corporate events gate on `announced_at`,
  not `event_time`.

Snapshot ids are derived (data type, key, provider, schema version, `as_of`, payload hash), so
re-collecting an unchanged response records a re-observation in the ledger instead of
duplicating history. The payload hash deliberately excludes observation clocks (`as_of`,
`retrieved_at`, `source_timestamp`) and our own verdicts, and deliberately includes event times
(`published_at`, `filed_at`, `period_end`) — it answers "is this the same information?".

Thresholds, freshness windows, plausibility bounds, research-usability policy and the market
calendar live in `config/data.yaml`, never as constants in code.

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

# Data layer. Append --simulated to run any of them offline.
python -m trading_system.cli data providers
python -m trading_system.cli data collect --symbol SPY
python -m trading_system.cli data collect-options --symbol SPY
python -m trading_system.cli data snapshot --symbol SPY --as-of 2026-08-10T14:30:00+00:00
python -m trading_system.cli data quality --symbol SPY
python -m trading_system.cli data history --symbol SPY
python -m trading_system.cli data status
python -m trading_system.cli run data-collection

# Universe selection. Read-only with respect to the broker; submits 0 orders.
python -m trading_system.cli universe validate           # config + data readiness
python -m trading_system.cli universe run --dry-run      # full pipeline, persists nothing
python -m trading_system.cli universe run
python -m trading_system.cli universe run --as-of 2026-08-10T14:30:00+00:00
python -m trading_system.cli universe show
python -m trading_system.cli universe history
python -m trading_system.cli universe explain --run-id <ID> [--symbol SPY]
python -m trading_system.cli run universe                # the scheduled job, once

pytest -m "not ibkr"                            # default: no gateway needed
pytest tests/universe                           # filters, point-in-time, snapshots, CLI
pytest tests/agents/test_universe_selector.py   # agent contract; needs no API key
ALLOW_LIVE_TESTS=true pytest -m ibkr            # requires a running IB Gateway
ALLOW_LIVE_TESTS=true pytest -m ibkr tests/data # data layer against IBKR Paper
ALLOW_LIVE_TESTS=true pytest -m llm             # opt-in: one real model call, no trades
```

`universe run` reads stored data only — it never collects and never opens a broker connection,
so run `data collect` first if the store is empty. It reports `DATA_UNAVAILABLE` rather than
inventing candidates.

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
  see `health_check(probe_latency=False)`. `IBKR_REQUEST_TIMEOUT_SECONDS` now sets
  `ib.RequestTimeout`, so the synchronous wrappers are bounded; there is no unbounded setting.
  Data providers get one short-lived connection per retrieval via
  `data/providers/broker_session.py`, which structurally cannot issue a second uncached request
  on the same connection.
- **IBKR returns one option-chain row per exchange *and trading class*, and one exchange appears
  many times.** Real SPY validation returned 39 rows including two on SMART: `SMART/SPY` with 35
  expirations and 491 strikes, and `SMART/2SPY` with 3 and 3. Taking the first SMART row stored
  3 of 491 strikes silently. `to_option_chain_snapshot` now picks the widest-coverage row for the
  preferred exchange, tie-breaking deterministically. Note what the `2SPY` finding actually means:
  the trading class cannot be *derived* from the symbol — not that SPY options are always `2SPY`.
  Whatever the broker reports is stored verbatim either way.
- **Simulated data is stamped `SIMULATED`/`SIMULATOR`** on every snapshot and position. Never
  let simulated or delayed data be presented as a live broker quote, and never fall back from
  real data to a synthesized value — raise `MarketDataUnavailableError` instead. The same rule
  applies to the cache: anything replayed from it is relabelled `CACHED` on the way out, and an
  expired entry is a miss rather than a stale hit.
- **A fallback names who answered, never who was asked.** `source.provider` is always the
  provider that actually produced the record; `source.requested_provider` records the one that
  did not answer. Merging fields from several sources requires `source.field_provenance`.
- **`structlog`'s `stdlib.add_logger_name` cannot be used with `PrintLoggerFactory`** — it reads
  `logger.name`, which `PrintLogger` does not have, so every log call raised `AttributeError`.
  `infrastructure/logging.py` binds the name in `get_logger` instead; `tests/unit/test_logging.py`
  keeps that from regressing.
- **A test that reaches the real gateway is a bug in the test.** Data CLI tests pass
  `--simulated` and repoint `data.service.project_root` at `tmp_path`, so they neither connect
  nor write into the repository's own `data/`. Universe CLI tests monkeypatch
  `cli._universe_service` for the same reason, and a test asserts no stray
  `data/universe/history.jsonl` is left behind — a stale "not implemented" case in
  `tests/unit/test_cli.py` once invoked `run universe` for real and wrote runs into the repo.
- **IBKR delayed market data reports no volume.** Real paper validation returns
  `volume=None` on a delayed SPY quote, which is why the shipped
  `min_average_daily_volume: 1000000` rejects everything with `VOLUME_UNAVAILABLE` rather than
  reading an unknown volume as passing. That is the correct behaviour, not a bug: to select a
  universe from delayed data, set the floor to 0 explicitly, or use a provider that reports
  volume. Never treat a missing measurement as a satisfied threshold.
- **`universe/__init__.py` loads its service lazily.** The service imports the agent, the agent
  imports the universe contract models, and an eager re-export closes that loop. Do not "tidy"
  the `__getattr__` away; moving the contract models out of the universe package would put a
  universe artifact somewhere it does not belong.
- **Agent prompts ship inside the package**, at `agents/prompts/*.md`, because the container
  installs the package and has no checkout to read from. Each prompt is fingerprinted on load
  and the hash is stored on every run, so a prompt edited without a `prompt_version` bump still
  leaves a trace. `.claude/agents/universe_selector.md` mirrors the same boundaries for
  development use, and a test asserts the two cannot drift on the safety-critical statements.
- **The market calendar is transcribed, not derived.** Holidays come from the NYSE calendar at
  <https://www.nyse.com/markets/hours-calendars>. Observance has edge cases and the early-close
  list does not follow from them at all: 2027 has no Christmas Eve early close because 24
  December *is* the observed holiday, and no 3 July early close because it falls on a Saturday.
  Carrying a previous year's pattern forward would have invented a session on a closed day.
  2026 and 2027 are covered; outside them the calendar answers `UNKNOWN`.

This directory is its own git repository (`git init`-ed, one commit: the specification). The
enclosing `/home/dmytro/git/` is a separate repo full of unrelated projects; nothing here should
ever be staged into it.
