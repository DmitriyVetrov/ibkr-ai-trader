# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**Milestones 1–7 of 12 are complete.** Built and tested: the domain layer (models, enums,
events, state machine), YAML configuration, the 26 JSON workflow schemas, the CLI surface,
structured logging, an injectable clock, the `Broker` abstraction with `SimulatedBroker` and a
read-only `IBKRBroker`, the read-only broker diagnostics, the reconciliation foundation, the
data layer (providers, canonical models, quality engine, point-in-time snapshots,
append-only history, repository), **universe selection** — the deterministic pre-filter,
the first AI agent, and immutable universe runs — **market research**: point-in-time
evidence assembly, news deduplication, the market researcher agent, deterministic hypothesis
and confidence validation, and immutable research reports — **strategy and contract
selection**: the strategy registry, the strategy selector agent, deterministic decision
validation, and the *deterministic* contract selector with immutable decision and selection
records — and **allocation and risk**: the campaign envelope, the deterministic risk engine,
the deterministic allocation engine, account snapshots, and an immutable allocation ledger.
2735 passing tests; ruff, ruff format and mypy clean.

**Not built, by design:** order execution, position lifecycle, autonomous trading, live
trading. The CLI exposes those commands but they exit `3` naming the milestone that
delivers them — they never fabricate output. Follow that pattern for anything still pending.

**Milestone 8 is next: execution** — the order builder, the simulator execution path, IBKR
Paper execution and fill tracking. It consumes the authorisations Milestone 7 produces: a
`CampaignAllocation` carries legs, a quantity, the capital committed and the maximum loss,
and *nothing* in it is an order. Milestone 8 decides how to execute an authorisation and
stays bound by every figure in it.

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
- `research/` — the first analytical layer. `universe run → point-in-time evidence → news
  deduplication → research input → agent → deterministic semantic validation → immutable
  report`. Same structural properties as `universe/`: repository only, no broker, no order
  path, one composition root in `research/service.py`. It *consumes* a universe and can never
  extend one.
- `strategies/` — the milestone where the AI stops and arithmetic starts.
  `research report → registry → deterministic gates → strategy input → agent →
  deterministic validation → decision → contract selector → selection`. Two composition
  roots in `strategies/service.py`, one per stage. The agent half imports no chain reader
  and no selector; the selector half imports no agent and no LLM client, and a test asserts
  both transitively. `strategies/__init__.py` defers everything that touches a repository
  through `__getattr__`, for the same reason `research/__init__.py` does.
- `risk/` — the deterministic risk engine: *is this position permitted?* `models · limits ·
  exposure · guards · account · engine · store`. The engine is a pure function of its
  arguments — no clock, no broker, no repository, no model — so a stored verdict is
  reproducible. Account state arrives as a captured `AccountSnapshot`, never from a live
  request.
- `allocation/` — the deterministic allocation engine: *how many units?* `models ·
  candidates · campaign · scorer · budget_allocator · store · service · report`. It imports
  `risk/`, never the reverse, and a test asserts it. `allocation/service.py` is the single
  composition root; both `risk/__init__.py` and `allocation/__init__.py` defer everything
  that touches a repository through `__getattr__`, for the same reason the other packages do.
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

## Market research (Milestone 5)

The market researcher answers exactly one question — *given information actually available as
of T, what is the most defensible expectation for this underlying over the configured
horizon, and what evidence supports it?* It produces an **outlook**, never a contract: no
model in `research/` has a field for a strike, an expiry, a right, a delta, a strategy, a
quantity or an amount of money, and tests assert their absence.

Six rules govern it, each with tests that fail loudly:

- **Evidence is cited by id, so a source cannot be invented.** Every fact in the input carries
  a derived `evidence_id`; the agent's response references those and has no field for a source
  name, a URL or a publication date. The report copies provenance from the *input*, so the
  agent interprets a fact but never gets to describe it. An id the input did not contain is
  rejected as `SEMANTIC_VALIDATION_FAILED`.
- **A and D are different claims, and the difference is enforced.** `A` is a regime — a large
  move with no required catalyst — and its evidence must include something that is not a dated
  event. `D` is a catalyst and needs a specific event from the input, inside the horizon, with
  `announced_at` recorded. An `A` naming a highly relevant in-horizon event is rejected as a
  mislabelled `D`. `B` needs `SUPPORTS_UP` evidence, `C` needs `SUPPORTS_DOWN`, `E` needs an
  explanation.
- **Confidence is constrained, not declared.** `HIGH` is refused — never quietly downgraded —
  when the evidence count, the cited source tiers, the data-quality verdict or an unresolved
  contradiction do not license it. Thresholds are in `config/research.yaml`. A confidence is
  a band; there is no percentage anywhere in a report.
- **Failure is never a market view.** A report whose status is not `SUCCESS` carries no
  hypothesis, no confidence and no catalysts — enforced by a model validator, a JSON-schema
  conditional, and a `to_research_report()` that refuses to project one. There is deliberately
  **no deterministic fallback**: a universe can be ordered without a model, but an outlook
  synthesised in place of one would be a fabricated view wearing a report's clothes.
- **One story is one piece of evidence.** News is grouped before the agent sees it (normalised
  headline similarity within a publication window), arriving as one item with a
  `duplicate_count`. Ten syndicated copies are corroboration, not ten catalysts.
- **`direction` and `stance` are separate axes.** What a fact points at
  (`SUPPORTS_UP` / `SUPPORTS_DOWN` / `SUPPORTS_LARGE_MOVE` / `NEUTRAL`) is not how it relates
  to the stated thesis (`SUPPORTS` / `CONTRADICTS` / `NEUTRAL`). Contradicting evidence is
  preserved; hiding it is what the confidence policy exists to catch.

`support` on a catalyst, risk or invalidation condition is **derived** from whether it cited
anything and any supplied value is discarded — an unsupported claim is labelled, never deleted.

## Strategy and contract selection (Milestone 6)

> **The AI selects the strategy. Deterministic code selects the contract.**

Two decisions, two stages, and the boundary between them is the milestone. The strategy agent
answers *what should we do*; `strategies/contract_selector.py` answers *which actual contract
satisfies that*. A strategy decision is a **proposal, never an order**, and the stage ends at a
purchase candidate: legs, and the cost of one unit of the structure.

Seven rules govern it, each with tests that fail loudly:

- **The agent cannot select a contract, because it is never shown one.** Its input carries a
  projection of the research report and the metadata of the eligible strategies — structure,
  leg shapes (`"BUY CALL x1"`), a DTE *window*. No chain, no strike list, no contract id, no
  account, no cash, and deliberately **no date at all**: events arrive as `days_until`, so
  there is nothing an expiration could be echoed from. Its output has no field for a strike,
  an expiry, a quantity or a price, and `extra="forbid"` means one that invented a field fails
  to parse rather than having it dropped.
- **A strategy that was not offered is inexpressible.** `strategy_output_schema` enumerates
  `selected_strategy` from the strategies the registry actually admitted for this hypothesis —
  the same technique that makes a rejected asset inexpressible in the universe input.
- **The hypothesis→strategy mapping is derived, never declared twice.** It comes from each
  strategy's own `applicable_hypotheses`. `E` maps to `NO_TRADE` because no strategy lists it,
  not because a second table says so. `D` reuses the straddle and strangle with an
  event-aligned expiration rather than inventing `EVENT_STRADDLE` strategies the specification
  does not define.
- **A strategy may narrow a global risk limit; it may never widen one.** DTE, price band,
  liquidity floors and spread ceiling are all checked, at configuration load *and* at registry
  build. The limit is never clamped silently — a clamped limit is a limit nobody can see.
- **Structure is code; policy is configuration.** A straddle is one call and one put on one
  strike because that is what the word means, so it lives in `strategies/long_straddle.py` and
  the registry refuses a configuration that describes something else. The delta, the offsets,
  the DTE window and the liquidity floors live in `config/strategies/*.yaml`, and nothing in
  the selector hard-codes one.
- **Nothing is invented and nothing is approximated.** A missing delta is `MISSING_DELTA`, not
  an estimate. An unquoted contract has an unknown cost, not a midpoint conjured from one side.
  A chain too coarse for the strike policy yields no contract, not the nearest one. Option
  liquidity that was never reported is `OPTION_LIQUIDITY_UNKNOWN`, never zero and never "fine".
  Underlying share volume is never read as evidence about a contract.
- **Selection is reproducible.** Same decision, same stored chain, same configuration, same
  `as_of` produces a byte-identical record — rejections and reasons included. Every tie breaks
  on an explicit key (lower strike, then contract id), never on iteration order.

`NO_TRADE` and `NO_VALID_CONTRACT` are first-class outcomes at their respective stages.
`REQUIRED_DATA_UNAVAILABLE` is deliberately distinct from both: "we declined" and "we could not
look" are different facts, and only the first is a judgement about a market.

DTE is counted in calendar days from the **exchange-local** date of `as_of` to the expiration,
through `config/data.yaml`'s market calendar — counting from a UTC date is wrong by one for
most of the evening. An expiration on a day the calendar says is closed is rejected; a year the
calendar does not cover answers `UNKNOWN` and is accepted as such.

## Allocation and risk (Milestone 7)

> **The campaign budget is independent of the broker account balance, and the most
> restrictive relevant limit always wins.**

The milestone where the system stops proposing and starts committing capital. Quantity and
money appear here for the first time and nowhere earlier. Two engines, deliberately separate:

```
purchase candidate (M6)  ->  RiskEngine       -> is this permitted?
                             AllocationEngine -> how many units?
                             immutable allocation ledger
```

`RiskEngine` tests *one whole unit* against every limit and can reject before a quantity
exists — a refusal reported as "we computed zero" tells nobody which limit to look at, whereas
`MAX_ALLOCATION_PER_TRADE_EXCEEDED` with an actual and a limit value tells them exactly.
`AllocationEngine` then sizes only what risk already approved, and can never overrule it.

Eight rules govern it, each with tests that fail loudly:

- **The campaign is not the account.** EUR 5,000 is the shipped envelope; the paper account's
  balance is irrelevant to it. A million-euro balance cannot widen the campaign, and an
  account holding less than the campaign permits binds instead. Both directions are tested.
- **No AI decides money.** Neither engine has a parameter, field or import through which a
  model could speak. Confidence *bands* from validated upstream artifacts feed the ordering
  and can never change a quantity, a limit or a permission — a test asserts that two
  candidates scoring 99 and 71 receive the same size.
- **Quantity is a floor, and a whole number.** `max_units()` computes an exact floor and then
  *verifies it by multiplication* in both directions, because a division that rounded the
  wrong way in the last digit would commit one contract more than any limit authorised, every
  time, silently. Boundary tests sit at exact fits and one cent either side.
- **Maximum loss comes from the strategy, not from a formula.** `MaxLossBasis` is declared on
  each `StrategyStructure` in code. "Max loss is the premium" is true of the four long-debit
  strategies shipped today and false of the first credit spread anyone adds; a basis the
  engine cannot compute is `MAX_LOSS_UNDEFINED` — a rejection, not an estimate.
- **A child limit may narrow a parent; it may never widen one.** `risk.yaml` → `campaign.yaml`
  → `config/strategies/*.yaml` → the position. Widening is a configuration *load failure*,
  never a clamp, and `RiskLimits.scopes` records which layer supplied each effective value.
- **An unevaluated limit is not a satisfied one.** `NOT_EVALUATED` is a distinct check
  outcome. Realised daily profit and loss is not tracked until Milestone 9, so the daily-loss
  limit is recorded as unevaluated rather than passed; `campaign.account.require_daily_loss_tracking`
  decides whether that unknown blocks a trade.
- **One opportunity, one reservation.** An opportunity's id is derived from the research
  report, strategy decision and contract selection it descends from, so a re-run recognises
  its own earlier authorisation and records `ALREADY_ALLOCATED` rather than reserving the
  capital twice. Campaign state is *replayed from the ledger*, never kept as a running total.
- **An authorisation is not an order.** No order type, no side, no limit price, no
  time-in-force, no broker order id — and no broker anywhere in either package's import
  graph. Milestone 7 ends at an authorisation boundary.

`NO_TRADE` is a first-class outcome, and so is `NO_ALLOCATION` at the run level: a valid
strategy and a valid contract are not an entitlement to capital, and a run that authorised
nothing because the campaign is committed is the ordinary answer rather than a failure.
`ACCOUNT_SNAPSHOT_UNAVAILABLE` is deliberately distinct — "we declined" and "we could not
look" are different facts.

**The broker is touched in exactly one place.** `risk capture-account` reads the account once
and stores an immutable `AccountSnapshot`; the engines read it back by id and hold no broker.
That is a safety property as well as an architectural one: Milestone 2 established that a
second uncached round trip on one IBKR connection can go unanswered indefinitely, so a risk
check that fetched its own account state could hang the process at the worst possible moment.
`risk/account.py` is a pure function over `BrokerAccount` and `BrokerPosition`; the CLI, which
already holds a broker, performs the read.

The score is a **weighted sum of structured upstream facts**, with every component and every
weight recorded on the stored decision so a total can be recomputed by hand. It decides
**order** only — never permission, never size. There is no hidden scoring anywhere.

Development guidance, including what to do when adding a strategy or a limit, is in
[skills/risk-allocation/README.md](skills/risk-allocation/README.md). Milestone 7 introduces
**no agent**, so there is deliberately no `.claude/agents/` entry for it — one would imply a
model is involved somewhere, and none is.

**Configuration over hardcoding.** Schedules live in `config/schedules.yaml`, risk in `risk.yaml`,
strategies in `config/strategies/*.yaml`, data policy in `data.yaml`, the candidate pool,
eligibility filters and ranking policy in `universe.yaml`, the research horizon, data
windows, cost ceilings, deduplication and confidence policy in `research.yaml`, the strategy
stage's eligibility gates and agent in `strategy.yaml`, the expiration rule, strike
policy, quote requirements and liquidity policy in `contract_selection.yaml`, and the campaign
envelope, allocation policy, position limits, currency policy and ranking weights in
`campaign.yaml`. Note the splits: `strategy.yaml` configures the *stage*,
`config/strategies/*.yaml` configure the *payoffs* — one is an agent, the others are
instruments; and `risk.yaml` states the outer boundary of the whole system while
`campaign.yaml` states what *this* campaign permits inside it. Source trust lives in
`sources.yaml` and **nowhere else** — `research/sources.py` reads it, it does not define a
second ranking. DTE policy is per-strategy, not one universal number. Ports come from config.
If a requirement is ambiguous, add a documented config option rather than a hidden assumption.

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

# Market research. Read-only with respect to the broker; submits 0 orders.
python -m trading_system.cli research validate           # config + data readiness
python -m trading_system.cli research validate --run-id <ID>   # re-check a stored run
python -m trading_system.cli research run --dry-run      # full pipeline, persists nothing
python -m trading_system.cli research run
python -m trading_system.cli research run --as-of 2026-08-10T14:30:00+00:00
python -m trading_system.cli research run --symbol NVDA  # a subset of the universe
python -m trading_system.cli research show [--run-id <ID>] [--symbol NVDA]
python -m trading_system.cli research explain --symbol NVDA [--run-id <ID>]
python -m trading_system.cli research history [--symbol NVDA]
python -m trading_system.cli run research                # the scheduled job, once

# Strategy selection. Read-only with respect to the broker; submits 0 orders.
python -m trading_system.cli strategy validate           # registry + hypothesis mapping
python -m trading_system.cli strategy validate --run-id <ID>   # re-check a stored run
python -m trading_system.cli strategy run --dry-run      # full pipeline, persists nothing
python -m trading_system.cli strategy run
python -m trading_system.cli strategy run --run-id <research-run-id>
python -m trading_system.cli strategy run --symbol NVDA  # a subset of the research run
python -m trading_system.cli strategy show [--run-id <ID>] [--symbol NVDA]
python -m trading_system.cli strategy history [--symbol NVDA]

# Contract selection. Deterministic: no model is consulted. Submits 0 orders.
python -m trading_system.cli contract validate           # the deterministic policy
python -m trading_system.cli contract validate --run-id <ID>
python -m trading_system.cli contract select --dry-run
python -m trading_system.cli contract select
python -m trading_system.cli contract select --run-id <strategy-run-id>
python -m trading_system.cli contract show [--run-id <ID>] [--symbol NVDA]
python -m trading_system.cli contract history [--symbol NVDA]

# Risk. Deterministic: no model is consulted. Submits 0 orders.
python -m trading_system.cli risk validate              # the limits in force, by layer
python -m trading_system.cli risk capture-account       # the ONE broker boundary
python -m trading_system.cli risk capture-account --simulated
python -m trading_system.cli risk evaluate              # permitted? persists nothing
python -m trading_system.cli risk show [--run-id <ID>]
python -m trading_system.cli risk explain --symbol NVDA [--run-id <ID>]

# Allocation. Deterministic; authorises capital, never an order. Submits 0 orders.
python -m trading_system.cli allocation validate        # campaign envelope + policy
python -m trading_system.cli allocation validate --run-id <ID>
python -m trading_system.cli allocation run --dry-run   # reserves nothing
python -m trading_system.cli allocation run
python -m trading_system.cli allocation run --run-id <contract-run-id>
python -m trading_system.cli allocation run --symbol NVDA
python -m trading_system.cli allocation show [--run-id <ID>] [--symbol NVDA]
python -m trading_system.cli allocation explain --symbol NVDA [--run-id <ID>]
python -m trading_system.cli allocation history [--symbol NVDA]

pytest -m "not ibkr and not llm"                # default: no gateway, no API key needed
pytest tests/universe                           # filters, point-in-time, snapshots, CLI
pytest tests/research                           # evidence, dedup, validation, snapshots, CLI
pytest tests/strategy                           # registry, boundaries, validation, service, CLI
pytest tests/strategies                         # one suite per strategy specification
pytest tests/contract_selection                 # policy, point-in-time, determinism
pytest tests/risk                               # limits, engine, account snapshots, boundaries
pytest tests/allocation                         # quantity, allocator, scorer, service, CLI
pytest tests/integration/test_research_to_allocation.py   # the whole chain; 0 orders
pytest tests/agents/test_universe_selector.py   # agent contract; needs no API key
pytest tests/agents/test_research_agent.py      # agent contract; needs no API key
pytest tests/agents/test_strategy_selector.py   # agent contract; needs no API key
ALLOW_LIVE_TESTS=true pytest -m ibkr            # requires a running IB Gateway
ALLOW_LIVE_TESTS=true pytest -m ibkr tests/data # data layer against IBKR Paper
ALLOW_LIVE_TESTS=true ANTHROPIC_API_KEY=... pytest -m llm   # one real model call, no trades
```

`universe run` reads stored data only — it never collects and never opens a broker connection,
so run `data collect` first if the store is empty. It reports `DATA_UNAVAILABLE` rather than
inventing candidates.

`research run` consumes the latest universe run plus stored data, and never re-selects: a
`--symbol` the universe did not choose is refused with `CONFIGURATION_ERROR` rather than
researched. Each underlying gets its own isolated model context — never one merged answer for
several assets — and each failure is recorded per symbol, so one unreachable call does not
stop the rest.

`strategy run` consumes a stored research run the same way: a `--symbol` research did not
cover is refused rather than decided. `contract select` deliberately takes no `--as-of` — the
instant comes from each decision, so a selection reconstructs exactly the data that was
visible when the strategy was chosen, and a selection made against a later chain would answer
a question nobody asked.

`allocation run` consumes a stored contract run and takes no `--as-of` for the same reason:
the instant comes from the run being allocated against, so an authorisation reconstructs
exactly the prices that were visible when the contract was chosen. It requires a stored
account snapshot and reports `ACCOUNT_SNAPSHOT_UNAVAILABLE` rather than assuming the money is
there — run `risk capture-account` first. Re-running over the same upstream artifacts is
idempotent: the second run records `ALREADY_ALLOCATED` and reserves nothing.

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
- **A package `__init__` is part of an agent's import graph, because Python executes it.**
  `import trading_system.research.models` runs `research/__init__.py`, so anything eager there
  is reachable from the agent. `data/__init__.py` used to import `data.service` eagerly, which
  pulled the entire broker package in behind every agent that merely wanted a canonical value
  type — invisible to an AST test that only reads direct imports. `data/__init__.py`,
  `research/__init__.py`, `universe/__init__.py` and `strategies/__init__.py` therefore defer
  everything that touches a repository, a provider or a broker via `__getattr__`, and
  `tests/research/test_boundaries.py` and `tests/strategy/test_boundaries.py` walk the closure
  *through* `__init__` files (skipping `if TYPE_CHECKING:` bodies, which never execute) to keep
  it that way. For `strategies/` the stakes are higher than tidiness: an eager re-export of
  `contract_selector` would put a chain reader in the strategy agent's import graph, which is
  precisely the boundary Milestone 6 exists to draw.
- **Agent prompts ship inside the package**, at `agents/prompts/*.md`, because the container
  installs the package and has no checkout to read from. Each prompt is fingerprinted on load
  and the hash is stored on every run, so a prompt edited without a `prompt_version` bump still
  leaves a trace. `.claude/agents/universe_selector.md`, `.claude/agents/market_researcher.md`
  and `.claude/agents/strategy_selector.md` mirror the same boundaries for development use, and
  a test asserts each pair cannot drift on the safety-critical statements. (Spec §3 names the
  third agent `options_strategist.md`; it ships as `strategy_selector.md` so the runtime prompt,
  the module and the subagent all share one name.)
- **`schemas/research_report.json` is the *narrow* Milestone 1 boundary the strategy stage
  consumes; `schemas/market_research_report.json` is the Milestone 5 audit record.** The two
  are deliberately different artifacts, exactly as `universe_selection.json` and
  `universe_selection_result.json` are, and `MarketResearchReport.to_research_report()`
  projects one onto the other. Do not merge them: the narrow one is a completed milestone's
  contract, and rewriting it would break the chain the contract tests walk.
- **`schemas/strategy_decision.json` is the *narrow* Milestone 1 boundary;
  `schemas/strategy_selection.json` is the Milestone 6 audit record**, and
  `schemas/contract_selection.json` is the selection record the purchase card will consume.
  `StrategyDecisionRecord.to_strategy_decision()` and
  `ContractSelectionResult.to_contract_selection()` project onto the M1 shapes, exactly as
  research does. The M1 `ContractSelection` model keeps its name and its meaning; the M6 record
  is deliberately a different, wider artifact rather than an extension of it.
- **The projected `confidence` float is a band representative, not a probability.** The M1
  boundary types it as a float, so `CONFIDENCE_BAND_VALUE` maps LOW/MEDIUM/HIGH onto fixed
  values. No calibration has been measured and none is claimed; the only property downstream
  code may rely on is the ordering. Never introduce a percentage into a research artifact.
- **The market calendar is transcribed, not derived.** Holidays come from the NYSE calendar at
  <https://www.nyse.com/markets/hours-calendars>. Observance has edge cases and the early-close
  list does not follow from them at all: 2027 has no Christmas Eve early close because 24
  December *is* the observed holiday, and no 3 July early close because it falls on a Saturday.
  Carrying a previous year's pattern forward would have invented a session on a closed day.
  2026 and 2027 are covered; outside them the calendar answers `UNKNOWN`.
- **Storing the same content twice does not create a second snapshot.** The payload hash
  excludes observation clocks, so re-storing identical records at a later instant is recorded
  as a *re-observation* and `get_as_of` keeps returning the earlier snapshot — which then looks
  stale to a consumer with a freshness limit. A test that wants "newer data" has to change a
  real field, not only a timestamp. This bit `tests/contract_selection/test_point_in_time.py`
  during Milestone 6 and the behaviour is correct; the test was wrong.
- **`tests/strategy` and `tests/strategies` are different suites, deliberately.** The singular
  one tests the *stage* — registry, agent boundary, decision validation, service, CLI — and the
  plural one holds a suite per strategy specification, which spec §27 requires. Both names are
  load-bearing (the Milestone 6 brief names `tests/strategy`, the specification names
  `tests/strategies/test_long_call.py`), so neither can be renamed away.
- **Option quotes are the binding constraint on contract selection, not the chain.** A stored
  `OPTION_CHAIN` gives expirations and strikes; it gives no contract id, no bid, no delta. With
  chain metadata alone every selection ends `REQUIRED_DATA_UNAVAILABLE` — correctly. Collect
  `data collect-options --quotes` first, and note that only the simulator supplies per-contract
  quotes today: the IBKR path is still deferred behind the one-round-trip constraint.
- **The shipped EUR campaign refuses a USD-quoted contract, and that is correct.** The
  universe is US-listed, the campaign is denominated in EUR, and no FX rate source is
  configured — so every US option is rejected with `CURRENCY_MISMATCH`. Converting at an
  invented rate would size a position wrongly by an amount nobody recorded. Two explicit ways
  forward: denominate the campaign in the currency it actually trades, or add the currency to
  `campaign.currency_policy.treat_as_campaign_currency` and accept that the two are being
  treated as one unit of account. `allow_conversion: true` deliberately *fails to load* until a
  deterministic rate source exists. `tests/risk/test_engine.py` pins the behaviour so it cannot
  surprise anyone.
- **`risk.yaml`'s 300-second staleness window is measured against the decision instant, not
  wall clock.** The whole chain is anchored at one `as_of`, so a quote captured at that instant
  has age zero however long ago the run happened. That is what lets a strict risk-layer window
  coexist with `contract_selection.yaml`'s 86,400-second selection window: the risk layer is
  stricter, which is the hierarchy working. A quote that was *already* 12 hours old when the
  strategy was chosen is genuinely stale and is refused.
- **An allocation run's id includes the campaign's committed state, not only its inputs.** The
  same candidates against a campaign that has since reserved capital are a different decision
  and reach a different answer, so a run id derived from the contract run alone would collide
  the two and the immutable store would refuse the second. An unchanged re-run still lands on
  the same id, which is what makes it idempotent rather than a second record of one event.
- **A Milestone 7 authorisation that was never executed still consumes campaign budget.**
  Milestone 7 cannot know whether an order filled. Double-authorising the same capital is the
  failure worth preventing; releasing stale reservations belongs to the milestone that learns
  what actually happened to them. `allocation/campaign.py` says so where it replays the ledger.
- **`ALREADY_ALLOCATED` is a risk rejection, not a third kind of outcome.** It carries
  `risk_outcome=REJECTED` with `DUPLICATE_OPPORTUNITY`, and both the model validator and
  `schemas/campaign_allocation.json` permit exactly `REJECTED` and `ALREADY_ALLOCATED` under a
  risk rejection. Narrowing that to `REJECTED` alone breaks idempotent re-runs.
- **`RiskReasonCode` was extended rather than forked.** Milestone 7 needed codes the Milestone 1
  vocabulary lacked (`INSUFFICIENT_BUYING_POWER`, `CURRENCY_MISMATCH`, `POINT_IN_TIME_ERROR`
  and the rest). Adding members is additive and keeps *one* authoritative list evaluation can
  aggregate across milestones; a parallel enum would have guaranteed the two drifted.
  `schemas/risk_decision.json` enumerates the same set and must be updated with it.

This directory is its own git repository (`git init`-ed, one commit: the specification). The
enclosing `/home/dmytro/git/` is a separate repo full of unrelated projects; nothing here should
ever be staged into it.
