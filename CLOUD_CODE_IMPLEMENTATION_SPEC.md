# Autonomous Options Trading System --- Claude Code Implementation Specification

## 0. Purpose

You are Claude Code, acting as the primary software architect and
developer for this project.

Build a modular, testable, stateful options-trading system for
Interactive Brokers (IBKR). The system is initially a **Paper Trading**
research/learning system and must be designed so that Live Trading can
be enabled later only through explicit configuration.

The system must not be implemented as one large AI agent. It must
consist of:

-   deterministic infrastructure and risk controls;
-   specialized AI agents/sub-agents;
-   strict input/output contracts between workflow stages;
-   persistent state;
-   reproducible market/research snapshots;
-   automated execution through IBKR;
-   continuous position monitoring;
-   thesis monitoring;
-   complete test coverage;
-   observability, notifications, and evaluation.

The project owner wants to use Claude Code for development and
Anthropic/Claude models for AI agents.

Use **Python 3.12+** unless there is a documented reason to change it.

The initial option maturity target is **14--30 calendar days to
expiration**.

The initial campaign budget is configurable; the first experimental
value may be **EUR 5,000**. Never use the full IBKR Paper Account
balance merely because the account contains a large simulated balance.

------------------------------------------------------------------------

# 1. Core architectural principles

## 1.1 Claude Code is the development environment, not the trading runtime

Claude Code is used to create, inspect, test, refactor, and operate the
project.

The autonomous trading runtime must be ordinary Python
services/processes that can continue operating without an interactive
Claude Code session.

Do not make the trading loop depend on an open interactive Claude Code
conversation.

AI agents are invoked by the application when an AI decision is
required.

## 1.2 LLMs do not control money directly

LLMs may:

-   research;
-   classify market hypotheses;
-   select an allowed strategy;
-   explain decisions;
-   identify thesis invalidation;
-   summarize evidence.

LLMs must not directly determine:

-   maximum campaign budget;
-   maximum risk;
-   final position size;
-   account-level risk limits;
-   whether a live order is permitted;
-   whether a safety limit can be bypassed.

Those decisions belong to deterministic modules.

## 1.3 Broker reality is authoritative

If internal state disagrees with IBKR, IBKR wins.

Examples:

-   database says an order is filled but IBKR says partially filled -\>
    treat it as partially filled;
-   database says a position exists but IBKR reports no position -\>
    reconcile and investigate;
-   database says four contracts are open but IBKR reports three -\> do
    not assume the fourth exists.

Create a reconciliation service.

## 1.4 Every trade must be reproducible

For every candidate and every executed trade, persist the relevant
inputs, decisions, market snapshots, strategy specification, allocation
decision, order details, fills, exit decisions, and final result.

Never rely on LLM memory as trading state.

## 1.5 No-trade is a first-class outcome

Every decision stage may return `NO_TRADE`.

The system must never trade simply because a universe was supplied.

## 1.6 Paper Trading first

Initial supported modes:

-   `DRY_RUN`
-   `PAPER`
-   `LIVE`

Default:

``` text
TRADING_MODE=PAPER
```

Live trading must require explicit configuration and multiple safety
checks.

Do not allow an LLM prompt to switch the system from PAPER to LIVE.

------------------------------------------------------------------------

# 2. High-level lifecycle

The complete lifecycle is:

``` text
Data Collection
      |
      v
1. Universe Selection
      |
      v
2. Market / Fundamental / News Research
      |
      v
3. Options Strategy Selection
      |
      v
4. Option Contract Selection
      |
      v
5. Opportunity Ranking
      |
      v
6. Campaign Budget Allocation
      |
      v
7. Deterministic Risk Validation
      |
      v
8. Order Construction
      |
      v
9. IBKR Execution
      |
      v
10. Position Reconciliation
      |
      v
11. Position Monitoring
      |
      +----> Trailing Stop
      |
      +----> Time-to-Expiration Policy
      |
      +----> Thesis Monitor
      |
      +----> Risk Limits
      |
      v
12. Exit Decision
      |
      v
13. IBKR Exit Execution
      |
      v
14. Trade Snapshot / Evaluation
      |
      v
15. Performance Analysis
```

There are two major recurring loops:

### Opportunity Discovery Loop

Runs on a slower cadence:

``` text
Universe -> Research -> Strategy -> Contract -> Rank -> Allocate -> Risk -> Execute
```

### Position Management Loop

Runs frequently:

``` text
Open Positions -> Reconcile -> Market Snapshot -> Risk -> Exit Policy -> WAIT/EXIT
```

Do not run the full research process every few minutes.

------------------------------------------------------------------------

# 3. Project structure

Create this initial structure:

``` text
options-trading-system/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Makefile
│
├── .claude/
│   ├── agents/
│   │   ├── universe_selector.md
│   │   ├── market_researcher.md
│   │   ├── options_strategist.md
│   │   ├── thesis_monitor.md
│   │   ├── position_manager.md
│   │   └── evaluation_analyst.md
│   ├── skills/
│   │   ├── market-research/
│   │   ├── options-analysis/
│   │   ├── strategy-specification/
│   │   ├── ibkr-operations/
│   │   ├── trade-evaluation/
│   │   └── testing/
│   ├── hooks/
│   │   ├── pre_tool_validation.sh
│   │   ├── post_edit_tests.sh
│   │   └── safety_guard.sh
│   └── settings.json
│
├── config/
│   ├── application.yaml
│   ├── risk.yaml
│   ├── campaign.yaml
│   ├── schedules.yaml
│   ├── sources.yaml
│   └── strategies/
│       ├── long_call.yaml
│       ├── long_put.yaml
│       ├── long_straddle.yaml
│       └── long_strangle.yaml
│
├── src/
│   └── trading_system/
│       ├── __init__.py
│       ├── cli.py
│       │
│       ├── domain/
│       │   ├── models.py
│       │   ├── enums.py
│       │   ├── events.py
│       │   └── state_machine.py
│       │
│       ├── agents/
│       │   ├── universe_selector.py
│       │   ├── market_researcher.py
│       │   ├── options_strategist.py
│       │   ├── thesis_monitor.py
│       │   ├── position_manager.py
│       │   └── evaluation_analyst.py
│       │
│       ├── data/
│       │   ├── providers/
│       │   │   ├── base.py
│       │   │   ├── market.py
│       │   │   ├── options.py
│       │   │   ├── news.py
│       │   │   ├── fundamentals.py
│       │   │   └── regulatory.py
│       │   ├── collectors/
│       │   ├── normalizers/
│       │   ├── cache/
│       │   ├── repository.py
│       │   └── quality.py
│       │
│       ├── strategies/
│       │   ├── registry.py
│       │   ├── base.py
│       │   ├── contract_selector.py
│       │   ├── long_call.py
│       │   ├── long_put.py
│       │   ├── long_straddle.py
│       │   └── long_strangle.py
│       │
│       ├── allocation/
│       │   ├── scorer.py
│       │   ├── budget_allocator.py
│       │   └── campaign.py
│       │
│       ├── risk/
│       │   ├── engine.py
│       │   ├── limits.py
│       │   ├── exposure.py
│       │   └── guards.py
│       │
│       ├── broker/
│       │   ├── base.py
│       │   ├── ibkr/
│       │   │   ├── client.py
│       │   │   ├── market_data.py
│       │   │   ├── orders.py
│       │   │   ├── positions.py
│       │   │   ├── executions.py
│       │   │   └── reconciliation.py
│       │   └── simulator/
│       │       ├── market.py
│       │       └── execution.py
│       │
│       ├── execution/
│       │   ├── order_builder.py
│       │   ├── execution_engine.py
│       │   └── fill_tracker.py
│       │
│       ├── portfolio/
│       │   ├── repository.py
│       │   ├── position_service.py
│       │   └── pnl.py
│       │
│       ├── monitoring/
│       │   ├── position_monitor.py
│       │   ├── thesis_monitor.py
│       │   ├── scheduler.py
│       │   ├── reconciliation_loop.py
│       │   └── health.py
│       │
│       ├── notifications/
│       │   └── telegram.py
│       │
│       ├── evaluation/
│       │   ├── backtest.py
│       │   ├── forward_test.py
│       │   ├── attribution.py
│       │   └── reports.py
│       │
│       └── infrastructure/
│           ├── logging.py
│           ├── settings.py
│           ├── clock.py
│           └── persistence.py
│
├── schemas/
│   ├── universe_selection.json
│   ├── research_report.json
│   ├── strategy_decision.json
│   ├── purchase_card.json
│   ├── allocation_decision.json
│   ├── risk_decision.json
│   ├── order_intent.json
│   ├── execution_result.json
│   ├── position_snapshot.json
│   ├── exit_decision.json
│   └── trade_snapshot.json
│
├── data/
│   ├── README.md
│   ├── raw/
│   ├── normalized/
│   ├── cache/
│   ├── historical/
│   └── snapshots/
│
├── trades/
│   ├── open/
│   ├── closed/
│   └── rejected/
│
├── reports/
│   ├── daily/
│   ├── weekly/
│   └── evaluation/
│
└── tests/
    ├── unit/
    ├── agents/
    ├── strategies/
    ├── allocation/
    ├── risk/
    ├── data/
    ├── broker/
    ├── execution/
    ├── monitoring/
    ├── integration/
    ├── contract/
    └── fixtures/
```

------------------------------------------------------------------------

# 4. CLAUDE.md requirements

Create a root `CLAUDE.md`.

It must tell Claude Code:

1.  This is an autonomous options-trading system.
2.  Python is the implementation language.
3.  PAPER is the default trading mode.
4.  Never enable LIVE trading without explicit configuration.
5.  Never bypass deterministic risk controls.
6.  Never expose secrets.
7.  Never place an order in a unit test.
8.  Never use production/live credentials in tests.
9.  IBKR state is authoritative for actual positions and orders.
10. All agent outputs must validate against schemas.
11. All trade decisions must be persisted.
12. Every code change must preserve tests.
13. Prefer deterministic code over LLM reasoning for financial
    constraints.
14. Never invent market data.
15. Never claim a source was consulted if it was not actually retrieved.
16. Never use future information in historical evaluation.
17. Preserve timestamps and source timestamps.
18. Keep AI agents isolated from direct broker mutation APIs.
19. Use mocks/simulators for most tests.
20. Integration tests against IBKR must be explicitly named and
    separately invoked.
21. All monetary calculations must use decimal-safe representations, not
    binary floating-point for accounting decisions.
22. All times must be timezone-aware and normalized to UTC internally.
23. Exchange-local time must be used only at the scheduling/presentation
    boundary.

The root `CLAUDE.md` should also define the rule:

> AI proposes. Deterministic modules validate. Broker executes. Broker
> state is authoritative.

------------------------------------------------------------------------

# 5. AI agents

## 5.1 Universe Selector Agent

### Input

-   current date;
-   historical universe data;
-   liquidity metrics;
-   option activity metrics;
-   configured selection rules.

### Output

A ranked list of approximately 10 liquid underlying tickers.

The first experiment should start with a broader candidate pool and
filter down to 10.

Example output:

``` json
{
  "as_of": "...",
  "candidates": [
    {
      "ticker": "NVDA",
      "rank": 1,
      "selection_score": 94
    }
  ]
}
```

This agent must select **underlyings**, not option contracts.

It must not select a strategy.

------------------------------------------------------------------------

# 6. Market Researcher Agent

Input:

-   selected underlying tickers;
-   analysis timestamp;
-   14--30 day target horizon;
-   trusted-source policy.

Research:

-   historical price behavior;
-   current price behavior;
-   earnings;
-   guidance;
-   company filings;
-   official investor relations;
-   regulatory information;
-   macroeconomic events;
-   sector developments;
-   relevant geopolitical events;
-   reputable financial news;
-   known upcoming catalysts;
-   historical reactions to similar catalysts;
-   volatility context where available.

Source priority:

### Tier 1

-   SEC / regulatory filings;
-   company investor relations;
-   official government sources;
-   exchanges;
-   official corporate announcements.

### Tier 2

-   Reuters;
-   Financial Times;
-   Bloomberg;
-   Wall Street Journal;
-   Associated Press;
-   other established financial news providers.

### Tier 3

-   specialized financial/industry publications.

### Tier 4

-   general web sources.

Do not treat a low-tier source as authoritative merely because it
appears in search results.

### Output classification

Each underlying must receive one primary hypothesis:

``` text
A = Strong move expected in either direction; direction uncertain
B = Predominantly bullish
C = Predominantly bearish
D = Sharp move expected after a specific event; direction uncertain
E = Other; free-text explanation required
```

Also output:

-   direction;
-   expected magnitude category;
-   confidence;
-   expected horizon;
-   key catalysts;
-   invalidation conditions;
-   evidence;
-   source references;
-   timestamp.

The agent must not select the option contract.

------------------------------------------------------------------------

# 7. Options Strategy Agent

Input:

-   research reports;
-   market hypothesis;
-   expected move;
-   confidence;
-   horizon;
-   current underlying price;
-   current option chain;
-   IV;
-   Greeks;
-   bid/ask;
-   volume;
-   open interest;
-   expiration structure;
-   configured strategy library.

Allowed strategies are defined in `config/strategies/`.

Examples:

-   Long Call;
-   Long Put;
-   Long Straddle;
-   Long Strangle;
-   additional strategies added later.

The agent may return:

``` text
BUY
```

or:

``` text
NO_TRADE
```

The agent must explain why the selected strategy matches the hypothesis.

It must not override liquidity rules or risk limits.

------------------------------------------------------------------------

# 8. Option Contract Selector

This component is deterministic.

Do not allow the LLM to arbitrarily select a contract.

Input:

-   selected strategy;
-   strategy specification;
-   underlying;
-   option chain;
-   DTE range 14--30;
-   liquidity limits;
-   IV constraints;
-   delta/strike requirements.

Output:

A concrete option contract or multi-leg combination satisfying the
strategy specification.

For example:

``` text
Underlying: NVDA
Strategy: LONG_CALL
DTE: 21
Target Delta: 0.60
Selected Contract: ...
```

For multi-leg strategies, produce a structured list of legs.

------------------------------------------------------------------------

# 9. Opportunity Ranking

Every candidate should receive a deterministic opportunity score.

The score can incorporate:

-   research confidence;
-   expected move;
-   strategy fit;
-   option liquidity;
-   IV conditions;
-   spread quality;
-   event timing;
-   historical evidence;
-   execution quality.

Do not force all 10 candidates into trades.

Candidates can be ranked:

``` text
1 -> 94
2 -> 88
3 -> 81
4 -> 67
...
```

Only candidates above configured thresholds continue.

------------------------------------------------------------------------

# 10. Campaign Budget Allocator

The campaign has its own capital budget.

Example:

``` text
CAMPAIGN_BUDGET_EUR=5000
```

This is independent from IBKR account buying power.

The allocator decides how much of the campaign budget can be allocated
across opportunities.

Example:

``` text
Opportunity A -> EUR 1,500
Opportunity B -> EUR 1,000
Opportunity C -> EUR 750
Opportunity D -> EUR 500
Opportunity E -> EUR 0
```

The allocation must be deterministic and reproducible.

The allocator must not spend the entire budget simply because candidates
exist.

Maintain:

-   total campaign budget;
-   allocated capital;
-   unallocated reserve;
-   capital per position;
-   capital per underlying;
-   exposure by direction;
-   exposure by strategy.

------------------------------------------------------------------------

# 11. Risk Engine

The Risk Engine is deterministic.

It must validate:

-   campaign budget;
-   maximum allocation per trade;
-   maximum number of positions;
-   maximum total open risk;
-   maximum underlying concentration;
-   maximum strategy concentration;
-   maximum directional exposure;
-   maximum daily loss;
-   liquidity requirements;
-   minimum/maximum option price;
-   spread constraints;
-   DTE constraints;
-   account/broker state;
-   trading mode.

Output:

``` text
APPROVED
```

or:

``` text
REJECTED
```

with machine-readable reason codes.

The LLM cannot override a rejection.

------------------------------------------------------------------------

# 12. Purchase Card

Before execution, create an immutable Purchase Card containing:

-   underlying;
-   strategy;
-   hypothesis;
-   confidence;
-   expected move;
-   expected horizon;
-   selected contract(s);
-   strike(s);
-   expiration;
-   quantity;
-   requested allocation;
-   risk limits;
-   entry conditions;
-   exit policy;
-   thesis invalidation conditions;
-   source evidence;
-   timestamps;
-   configuration version;
-   strategy specification version.

This is the authoritative explanation of why the system intends to
trade.

------------------------------------------------------------------------

# 13. Execution Engine

Execution must be separated from strategy.

The Execution Engine receives an approved `OrderIntent`.

It must:

1.  validate current broker state;
2.  validate current market data;
3.  verify the contract still exists;
4.  verify price/slippage constraints;
5.  construct the order;
6.  submit through IBKR;
7.  track order status;
8.  handle partial fills;
9.  persist executions;
10. reconcile the resulting position.

Support:

-   single-leg orders;
-   multi-leg combination orders;
-   cancellation;
-   controlled replacement;
-   fill tracking.

Never allow an AI agent to directly call arbitrary `place_order()`
without deterministic validation.

------------------------------------------------------------------------

# 14. IBKR adapter

Use a broker abstraction:

``` python
class Broker:
    def get_account(self): ...
    def get_positions(self): ...
    def get_open_orders(self): ...
    def get_executions(self): ...
    def get_market_data(self, ...): ...
    def get_option_chain(self, ...): ...
    def place_order(self, ...): ...
    def cancel_order(self, ...): ...
```

Implement:

``` text
IBKRBroker
SimulatedBroker
```

The application must not depend directly on low-level IBKR API calls.

Use the Dockerized IB Gateway + IBC setup chosen by the project owner.

Keep IBKR credentials in environment variables/secrets, never in Git.

------------------------------------------------------------------------

# 15. IBKR connection smoke test

Create a dedicated test command that is safe and read-only.

Required command:

``` bash
python -m trading_system.cli test ibkr-connection
```

Expected behavior:

1.  connect to IBKR;
2.  report connection status;
3.  identify the account;
4.  read account summary;
5.  read current positions;
6.  read open orders;
7.  report current trading mode;
8.  disconnect cleanly;
9.  place **zero orders**.

Example output:

``` text
IBKR CONNECTION TEST
--------------------
Status: CONNECTED
Mode: PAPER
Account: ********
Buying Power: ...
Positions: 4
Open Orders: 0

PASS
No orders were submitted.
```

Also implement:

``` bash
python -m trading_system.cli test ibkr-portfolio
```

This must only read:

-   positions;
-   quantities;
-   contract identifiers;
-   market values;
-   P&L where available.

Never submit orders.

Create a separate explicit test for order submission:

``` bash
python -m trading_system.cli test ibkr-order-simulation
```

This must use the simulator unless explicitly configured to use the
Paper account.

------------------------------------------------------------------------

# 16. Position State Machine

Use persistent state.

Example:

``` text
DISCOVERED
RESEARCHED
STRATEGY_SELECTED
CONTRACT_SELECTED
ALLOCATED
RISK_APPROVED
ORDER_SUBMITTED
PARTIALLY_FILLED
OPEN
MONITORING
EXIT_TRIGGERED
CLOSING
CLOSED
```

Terminal/rejection states:

``` text
NO_TRADE
REJECTED
CANCELLED
FAILED
EXPIRED
```

State transitions must be deterministic and persisted.

------------------------------------------------------------------------

# 17. Position Monitoring

The Position Monitor runs frequently.

Input:

-   current open positions;
-   latest market data;
-   purchase card;
-   exit policy;
-   time to expiration;
-   current strategy state.

It evaluates:

### A. Trailing stop

For the whole strategy where the strategy specification says the
position is a combined structure.

For example, a four-leg straddle/strangle must normally be managed as a
single strategy-level position unless its strategy specification
explicitly permits independent leg exits.

### B. Time-to-expiration

The exit policy must become stricter as expiration approaches.

Do not hard-code one universal number for every strategy.

Use strategy-specific policy configuration.

### C. Risk

Check:

-   current loss;
-   campaign exposure;
-   position concentration;
-   unexpected broker state.

### D. Thesis status

Do not perform full research on every monitoring tick.

Use a separate Thesis Monitor.

------------------------------------------------------------------------

# 18. Thesis Monitor

The Thesis Monitor checks whether the original reason for entering the
trade is still valid.

It may return:

``` text
VALID
WEAKENING
INVALIDATED
UNKNOWN
```

If:

``` text
INVALIDATED
```

the Position Manager must evaluate an exit.

Persist the new evidence.

Do not silently rewrite the original thesis.

The original thesis remains immutable.

------------------------------------------------------------------------

# 19. Exit Policy

Exit decisions can be triggered by:

1.  trailing stop;
2.  expiration policy;
3.  thesis invalidation;
4.  risk limit;
5.  broker/account safety condition;
6.  emergency condition.

The exit decision must identify the reason.

Example:

``` json
{
  "decision": "SELL",
  "reason": "TRAILING_STOP",
  "strategy_id": "...",
  "timestamp": "..."
}
```

For a combined multi-leg strategy, the default behavior is to close the
strategy as a whole.

Do not independently close one leg unless the strategy specification
explicitly permits it.

------------------------------------------------------------------------

# 20. Reconciliation

Run continuously.

Compare:

``` text
Internal State
     vs
IBKR State
```

Reconcile:

-   positions;
-   quantities;
-   orders;
-   fills;
-   average prices;
-   market values.

If discrepancy occurs:

``` text
RECONCILIATION_ERROR
```

and prevent new execution until the discrepancy is resolved or
explicitly classified as safe.

------------------------------------------------------------------------

# 21. Historical data strategy

Do not require an expensive 8-year options dataset before the project
can start.

Use free/available sources initially.

At the same time, build a persistent data collection pipeline.

The system should gradually accumulate:

-   underlying market snapshots;
-   option chain snapshots;
-   IV;
-   Greeks;
-   bid/ask;
-   volume;
-   open interest;
-   news;
-   events;
-   research outputs;
-   decisions;
-   orders;
-   fills;
-   positions.

Every snapshot must include:

``` text
as_of_timestamp
source_timestamp
retrieved_timestamp
source_id
data_quality
```

Do not use future information in historical analysis.

The system must explicitly protect against look-ahead bias.

------------------------------------------------------------------------

# 22. Data directory README

Create `data/README.md`.

Explain:

``` text
raw/
```

Original provider responses.

``` text
normalized/
```

Canonical internal representation.

``` text
cache/
```

Temporary/reusable data that can be regenerated.

``` text
historical/
```

Long-lived historical datasets.

``` text
snapshots/
```

Point-in-time snapshots used for research, decisions, backtesting, and
reproducibility.

Never mix temporary cache with immutable research snapshots.

------------------------------------------------------------------------

# 23. Scheduling

Create a persistent scheduler.

Required jobs:

``` text
data_collection
universe_refresh
opportunity_scan
position_monitor
thesis_monitor
reconciliation
end_of_day_report
```

Do not hard-code schedules into individual agents.

Keep them in:

``` text
config/schedules.yaml
```

Each job must be independently runnable from the CLI.

Examples:

``` bash
python -m trading_system.cli run universe
python -m trading_system.cli run research
python -m trading_system.cli run opportunities
python -m trading_system.cli run position-monitor
python -m trading_system.cli run thesis-monitor
python -m trading_system.cli run reconciliation
```

------------------------------------------------------------------------

# 24. Testing strategy

Testing is a first-class requirement.

Every workflow component must have independent tests.

Minimum test layers:

## 24.1 Unit tests

Test deterministic functions without network or broker access.

Examples:

``` bash
pytest tests/unit
pytest tests/risk
pytest tests/allocation
pytest tests/strategies
```

## 24.2 Agent tests

Every AI agent must have its own test suite.

Required:

``` text
tests/agents/test_universe_selector.py
tests/agents/test_market_researcher.py
tests/agents/test_options_strategist.py
tests/agents/test_thesis_monitor.py
tests/agents/test_position_manager.py
tests/agents/test_evaluation_analyst.py
```

Agent tests must validate:

-   input schema;
-   output schema;
-   required fields;
-   allowed enum values;
-   refusal/no-trade behavior;
-   malformed input;
-   missing data;
-   conflicting evidence;
-   source attribution;
-   deterministic post-processing.

Use recorded fixtures/mocks for normal tests.

Do not require live web calls for ordinary unit tests.

------------------------------------------------------------------------

# 25. Contract tests

Every workflow boundary must have a schema.

Example:

``` text
UniverseSelection
      ↓
ResearchReport
      ↓
StrategyDecision
      ↓
PurchaseCard
      ↓
AllocationDecision
      ↓
RiskDecision
      ↓
OrderIntent
      ↓
ExecutionResult
      ↓
PositionSnapshot
      ↓
ExitDecision
      ↓
TradeSnapshot
```

Create tests that verify that the producer output can be consumed by the
next stage.

Commands:

``` bash
pytest tests/contract
```

------------------------------------------------------------------------

# 26. Data provider tests

Every provider adapter gets isolated tests.

Examples:

``` bash
pytest tests/data/test_market_provider.py
pytest tests/data/test_options_provider.py
pytest tests/data/test_news_provider.py
pytest tests/data/test_fundamentals_provider.py
pytest tests/data/test_regulatory_provider.py
```

Use fixtures for provider responses.

Test:

-   valid response;
-   missing fields;
-   stale data;
-   malformed data;
-   provider timeout;
-   rate limit;
-   duplicate records;
-   timestamp normalization.

------------------------------------------------------------------------

# 27. Strategy tests

Each strategy must have independent tests.

Examples:

``` bash
pytest tests/strategies/test_long_call.py
pytest tests/strategies/test_long_put.py
pytest tests/strategies/test_long_straddle.py
pytest tests/strategies/test_long_strangle.py
```

Test:

-   valid entry;
-   invalid entry;
-   DTE boundaries;
-   strike selection;
-   liquidity filters;
-   IV constraints;
-   multi-leg construction;
-   strategy-level exit;
-   no-trade conditions.

------------------------------------------------------------------------

# 28. Allocation tests

Test the EUR 5,000 campaign budget independently.

Examples:

``` bash
pytest tests/allocation
```

Test scenarios:

1.  one excellent opportunity;
2.  ten opportunities;
3.  budget exhausted;
4.  minimum allocation;
5.  maximum allocation;
6.  concentration limit;
7.  reserve cash;
8.  low-quality opportunities;
9.  ties;
10. deterministic repeatability.

Given identical inputs, allocation must produce identical results.

------------------------------------------------------------------------

# 29. Risk tests

Run:

``` bash
pytest tests/risk
```

Test:

-   max trade risk;
-   max portfolio risk;
-   max underlying exposure;
-   max strategy exposure;
-   max number of positions;
-   campaign budget;
-   daily loss limit;
-   liquidity constraints;
-   trading mode;
-   live-mode guards.

Critical invariant:

> No risk-engine rejection can be overridden by an AI agent.

------------------------------------------------------------------------

# 30. Broker tests

Create:

``` text
tests/broker/
├── test_ibkr_adapter.py
├── test_ibkr_positions.py
├── test_ibkr_orders.py
├── test_reconciliation.py
└── test_simulator.py
```

Most tests use mocks.

Live IBKR connection tests are separate.

Commands:

``` bash
python -m trading_system.cli test ibkr-connection
python -m trading_system.cli test ibkr-portfolio
python -m trading_system.cli test ibkr-market-data
python -m trading_system.cli test ibkr-option-chain
```

All read-only tests must explicitly guarantee:

``` text
orders_submitted = 0
```

------------------------------------------------------------------------

# 31. Integration tests

Integration tests connect multiple workflow components.

Examples:

``` bash
pytest tests/integration/test_research_to_strategy.py
pytest tests/integration/test_strategy_to_allocation.py
pytest tests/integration/test_allocation_to_risk.py
pytest tests/integration/test_risk_to_execution.py
pytest tests/integration/test_position_lifecycle.py
```

Use the simulated broker by default.

------------------------------------------------------------------------

# 32. End-to-end dry-run test

Create:

``` bash
python -m trading_system.cli test e2e-dry-run
```

This should execute:

``` text
Universe
→ Research fixture
→ Strategy
→ Contract selection
→ Allocation
→ Risk
→ Simulated order
→ Simulated fill
→ Position monitoring
→ Exit
→ Trade snapshot
```

No external broker order must be submitted.

Expected result:

``` text
PASS
Complete lifecycle executed in simulation.
```

------------------------------------------------------------------------

# 33. Paper end-to-end test

Create a separate command:

``` bash
python -m trading_system.cli test e2e-paper
```

This may connect to IBKR Paper but must be clearly labeled.

It should initially perform a controlled, harmless workflow.

Do not create an arbitrary market order merely to prove connectivity.

Use the simulator for execution logic tests and reserve IBKR Paper tests
for broker integration.

------------------------------------------------------------------------

# 34. CLI testing interface

Implement a consistent CLI.

Examples:

``` bash
# Run all tests
pytest

# Run only agent tests
pytest tests/agents

# Test one agent
pytest tests/agents/test_market_researcher.py

# Test one workflow stage
python -m trading_system.cli test workflow research

# Test strategy selection
python -m trading_system.cli test strategy-selection --ticker NVDA

# Test contract selection
python -m trading_system.cli test contract-selection --ticker NVDA

# Test allocation
python -m trading_system.cli test allocation

# Test risk
python -m trading_system.cli test risk

# Test IBKR connection
python -m trading_system.cli test ibkr-connection

# Read IBKR portfolio
python -m trading_system.cli test ibkr-portfolio

# Test reconciliation
python -m trading_system.cli test reconciliation

# Full simulated lifecycle
python -m trading_system.cli test e2e-dry-run
```

Also add Makefile shortcuts:

``` bash
make test
make test-agents
make test-strategies
make test-risk
make test-allocation
make test-broker
make test-integration
make test-e2e
make ibkr-connection
make ibkr-portfolio
```

------------------------------------------------------------------------

# 35. Test safety rules

Tests must never:

-   use live credentials;
-   submit a live order;
-   change live positions;
-   delete real broker data;
-   bypass risk limits.

Any test capable of submitting an IBKR Paper order must require explicit
configuration.

Live tests must be disabled unless:

``` text
ALLOW_LIVE_TESTS=true
```

and additional safeguards are satisfied.

Never make `ALLOW_LIVE_TESTS=true` the default.

------------------------------------------------------------------------

# 36. LLM evaluation

AI agents need more than ordinary unit tests.

Create evaluation fixtures.

Example:

``` text
tests/fixtures/research/
    nvda_bullish.json
    nvda_bearish.json
    event_uncertain.json
    insufficient_evidence.json
```

For each fixture, define expected constraints rather than one exact
prose answer.

Example:

``` text
classification must be one of A/B/C/D/E
confidence must be between 0 and 1
expected_horizon must be 14–30 days
sources must be present
invalidation_conditions must be present
```

Do not compare AI responses by exact text.

Validate structured semantics.

------------------------------------------------------------------------

# 37. Source and research integrity

Research output must include source metadata.

Do not allow the agent to say:

> "According to Reuters..."

unless the source was actually retrieved.

Persist:

``` text
source_name
source_url_or_identifier
published_at
retrieved_at
relevance
source_tier
```

For historical evaluation, only use information available at the
relevant historical timestamp.

------------------------------------------------------------------------

# 38. Notifications

Implement Telegram notifications as an output channel.

Notifications should include:

### Opportunity

-   ticker;
-   score;
-   hypothesis;
-   confidence;
-   strategy;
-   proposed allocation.

### Entry

-   contract;
-   quantity;
-   fill;
-   allocation;
-   thesis;
-   reason.

### Exit

-   P&L;
-   R;
-   exit reason;
-   DTE;
-   maximum favorable excursion;
-   maximum adverse excursion.

### Errors

-   broker disconnected;
-   reconciliation mismatch;
-   data provider failure;
-   risk rejection;
-   execution failure.

Telegram must not be the authoritative state.

------------------------------------------------------------------------

# 39. Dashboard

Create a simple dashboard later in the project.

Minimum views:

-   campaign budget;
-   open positions;
-   realized P&L;
-   unrealized P&L;
-   P&L by strategy;
-   P&L by underlying;
-   R multiples;
-   research accuracy;
-   strategy accuracy;
-   execution slippage;
-   fill rates;
-   reconciliation errors;
-   thesis invalidation rate.

Do not make dashboard implementation block the first trading-system
milestone.

------------------------------------------------------------------------

# 40. Trade snapshots

For every trade, create an immutable directory:

``` text
trades/closed/<trade_id>/
```

Include:

``` text
research.json
strategy_decision.json
purchase_card.json
allocation.json
risk_decision.json
entry_market_snapshot.json
order_intent.json
execution.json
position_history.json
thesis_updates.json
exit_decision.json
exit_market_snapshot.json
final_result.json
```

The system must be able to reconstruct why the trade happened.

------------------------------------------------------------------------

# 41. Versioning

Every trade snapshot must store:

-   application version;
-   strategy specification version;
-   configuration version;
-   prompt/agent version;
-   model identifier;
-   data-source versions where available.

This is necessary to compare strategy versions later.

------------------------------------------------------------------------

# 42. Security

Secrets must only come from environment variables or secure secret
storage.

Never commit:

``` text
IBKR_USERNAME
IBKR_PASSWORD
IBKR_ACCOUNT
ANTHROPIC_API_KEY
TELEGRAM_BOT_TOKEN
```

to Git.

`.env` must be in `.gitignore`.

Provide:

``` text
.env.example
```

with placeholders only.

The IBKR Docker/IBC layer must be isolated from arbitrary application
code as much as practical.

------------------------------------------------------------------------

# 43. Docker architecture

Use Docker Compose for local infrastructure.

At minimum:

``` text
ib-gateway
trading-runtime
```

Optional later:

``` text
database
dashboard
```

The IBKR Gateway/IBC service must support:

``` text
PAPER / LIVE
auto-start
healthcheck
environment-based credentials
```

The Python trading runtime connects to the broker through an internal
adapter.

Do not hard-code ports.

Use environment/configuration.

------------------------------------------------------------------------

# 44. Database / persistence

Start simple if necessary, but design repositories behind interfaces.

Recommended initial persistence:

-   SQLite for local development;
-   filesystem snapshots for immutable artifacts.

The architecture must allow later migration to PostgreSQL.

Do not couple business logic directly to SQLite queries.

------------------------------------------------------------------------

# 45. Error handling

Every external dependency can fail.

Handle:

-   network timeout;
-   provider unavailable;
-   stale market data;
-   malformed option chain;
-   IBKR disconnect;
-   order rejection;
-   partial fill;
-   duplicate event;
-   scheduler restart;
-   process restart.

Prefer fail-safe behavior.

Examples:

``` text
unknown broker state -> do not trade
stale option chain -> do not trade
risk engine unavailable -> do not trade
reconciliation failure -> do not open new positions
live mode without explicit safety configuration -> do not trade
```

------------------------------------------------------------------------

# 46. Development milestones

Implement in this order.

## Milestone 1 --- Skeleton

-   project structure;
-   configuration;
-   domain models;
-   schemas;
-   CLI;
-   logging;
-   tests.

## Milestone 2 --- Broker connectivity

-   simulated broker;
-   IBKR adapter;
-   IBKR connection test;
-   portfolio read test;
-   reconciliation.

## Milestone 3 --- Data layer

-   provider interfaces;
-   free sources;
-   caching;
-   normalization;
-   snapshots;
-   data quality.

## Milestone 4 --- Universe

-   universe selector;
-   10-ticker output;
-   tests.

## Milestone 5 --- Research

-   research agent;
-   source policy;
-   structured research report;
-   tests/evaluations.

## Milestone 6 --- Strategy

-   strategy registry;
-   long call;
-   long put;
-   straddle;
-   strangle;
-   contract selector;
-   tests.

## Milestone 7 --- Allocation and risk

-   campaign budget;
-   deterministic allocation;
-   risk engine;
-   tests.

## Milestone 8 --- Execution

-   order builder;
-   simulator;
-   IBKR Paper execution;
-   fill tracking;
-   tests.

## Milestone 9 --- Position lifecycle

-   state machine;
-   reconciliation;
-   trailing stop;
-   DTE policy;
-   thesis monitor;
-   exit engine.

## Milestone 10 --- Automation

-   scheduler;
-   recurring jobs;
-   Telegram;
-   health checks.

## Milestone 11 --- Evaluation

-   forward testing;
-   attribution;
-   reports;
-   dashboard.

## Milestone 12 --- Live readiness

Do not enable Live automatically.

Create a formal readiness checklist first.

------------------------------------------------------------------------

# 47. Definition of done

The project is not considered ready merely because:

``` text
Claude can call IBKR.
```

It is ready for the Paper Trading experiment only when:

-   all major modules have tests;
-   all agent outputs validate;
-   IBKR connection test passes;
-   portfolio-read test passes;
-   reconciliation works;
-   simulator works;
-   risk engine is tested;
-   campaign allocation is deterministic;
-   complete dry-run lifecycle works;
-   Paper lifecycle works;
-   trade snapshots are generated;
-   failures are observable;
-   Telegram notifications work;
-   no live credentials are required;
-   live mode remains disabled.

The project is not ready for Live Trading until there is sufficient
Paper forward-test evidence and an explicit manual decision to enable
Live.

------------------------------------------------------------------------

# 48. Required developer behavior for Claude Code

When implementing this specification:

1.  Inspect the repository before creating files.
2.  Create the architecture incrementally.
3.  Do not generate fake implementations that claim to connect to IBKR.
4.  Use interfaces and mocks where external dependencies are
    unavailable.
5.  Add tests with every module.
6.  Run the relevant tests after each implementation step.
7.  Do not silently change the architecture.
8.  If a requirement is ambiguous, create a documented configuration
    option rather than inventing a hidden assumption.
9.  Never invent financial data.
10. Never invent API responses.
11. Clearly distinguish mock, simulator, Paper, and Live behavior.
12. Keep deterministic financial/risk logic outside LLM prompts.
13. Keep all external integrations behind adapters.
14. Preserve point-in-time timestamps.
15. Preserve immutable trade snapshots.
16. Prefer small, independently testable modules.
17. Do not introduce paid data providers unless explicitly approved by
    the project owner.
18. Prefer free data sources during the initial experiment.
19. Make all test commands discoverable through `--help`.
20. Every new agent must receive a dedicated test suite.

------------------------------------------------------------------------

# 49. Required CLI discovery

The following must work:

``` bash
python -m trading_system.cli --help
```

and show commands for:

``` text
run
test
data
portfolio
positions
research
opportunities
reconcile
reports
health
```

Examples:

``` bash
python -m trading_system.cli test --help
python -m trading_system.cli test ibkr-connection
python -m trading_system.cli test ibkr-portfolio
python -m trading_system.cli test e2e-dry-run
```

The CLI should clearly distinguish commands that can mutate state from
read-only commands.

------------------------------------------------------------------------

# 50. Final architectural rule

The system must enforce this hierarchy:

``` text
AI AGENTS
   |
   | propose / analyze / explain
   v
DETERMINISTIC DECISION LAYER
   |
   | validate / allocate / limit
   v
EXECUTION ENGINE
   |
   | submit
   v
IBKR
   |
   | actual state
   v
RECONCILIATION
   |
   v
PERSISTENT SYSTEM STATE
```

Never reverse this hierarchy.

The AI is not the source of truth for money, positions, orders, or
broker state.

The AI provides intelligence.

The deterministic layer provides constraints.

IBKR provides execution reality.

The persistence layer provides historical truth.

The evaluation layer determines whether the system actually works.

Build the project around these boundaries from the beginning.
