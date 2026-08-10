# Autonomous Options Trading System

A modular, testable, stateful options-trading system for Interactive Brokers.

This is a **paper-trading research system**. Live trading is not enabled and is
not reachable by configuration alone — see [Trading modes](#trading-modes).

> AI proposes. Deterministic modules validate. Broker executes.
> Broker state is authoritative.

## Status

**Milestone 1 of 12 — project skeleton.** What exists today:

- domain models, enums, events and the position state machine;
- YAML configuration for campaign, risk, schedules, sources and strategies;
- the 11 JSON schemas for the workflow boundaries;
- the CLI surface, with every command tagged read-only or state-mutating;

- structured logging, an injectable clock, and the test suite.

What deliberately does **not** exist yet: IBKR connectivity, market/news data
providers, AI agents, order execution, and any form of live trading. Commands
covering those exist in the CLI but exit with a "not implemented" message
naming the milestone that delivers them. They never fabricate output.

See section 46 of [the implementation specification](CLOUD_CODE_IMPLEMENTATION_SPEC.md)
for the full milestone plan.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (or pip) for dependency management

## Setup

```bash
uv venv
uv pip install -e '.[dev]'
cp .env.example .env      # then fill in real values; .env is git-ignored
```

## Usage

```bash
python -m trading_system.cli --help      # full command surface
python -m trading_system.cli health      # config + mode + schema check
python -m trading_system.cli config      # validate and print configuration
python -m trading_system.cli version
```

Every command's help text is tagged `(read-only)` or `(mutates state)`.
Commands belonging to later milestones exit with code `3`.

## Testing

```bash
make test              # whole suite
make test-unit         # deterministic unit tests
make test-contract     # workflow-boundary schema compatibility
pytest tests/unit/test_state_machine.py            # one file
pytest tests/unit/test_state_machine.py::test_name # one test
make lint typecheck    # ruff + mypy
```

No test makes a network call, connects to a broker, or submits an order.
Tests capable of reaching a real account require `ALLOW_LIVE_TESTS=true`, which
is never the default and is not needed by anything in Milestone 1.

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
  source trust tiers, strategy definitions. Committed and reviewable, so a
  change to a risk limit shows up in a diff.

Monetary values in YAML are quoted strings or integers (`"0.50"`, `5000`) so
they parse as exact decimals. An unquoted `0.50` is a binary float and is
rejected by design.

## Repository layout

```
config/     trading policy (committed, reviewable)
schemas/    JSON Schema for each workflow boundary
src/        the trading_system package
data/       market data, snapshots and caches (contents git-ignored)
trades/     immutable per-trade artifact directories (contents git-ignored)
reports/    generated reports (contents git-ignored)
tests/      unit, contract, integration and per-component suites
```

## Safety properties

These are enforced in code and covered by tests, not left to convention:

- monetary fields reject binary floats;
- naive datetimes are rejected; everything is stored as UTC;
- illegal position state transitions raise rather than silently proceeding;
- an `APPROVED` risk decision cannot carry a rejection reason, and vice versa;
- `NO_TRADE` is a valid outcome at every decision stage;
- the allocator's books must balance: allocated + reserve == total budget.
