.DEFAULT_GOAL := help
PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
CLI := $(PYTHON) -m trading_system.cli

.PHONY: help install test test-unit test-contract test-agents test-universe \
        test-research test-strategy test-strategies test-contract-selection \
        test-allocation test-allocation-unit test-allocation-integration test-risk \
        test-execution test-execution-unit test-execution-integration test-paper-execution \
        test-positions test-reservations test-reconciliation test-position-integration \
        test-exit test-exit-unit test-exit-integration test-exit-safety test-exit-cli \
        test-paper-exit \
        test-observability test-scheduler test-pnl test-operations test-alerts \
        test-operations-integration \
        ops-health ops-scheduler-plan ops-jobs ops-alerts ops-metrics \
        pnl-show pnl-history pnl-settle \
        observability-up observability-down observability-validate \
        test-broker test-data test-integration \
        test-e2e universe-validate universe-run universe-show research-validate \
        research-run research-show strategy-validate strategy-run strategy-show \
        contract-validate contract-select contract-show \
        risk-validate risk-evaluate risk-capture-account \
        allocation-validate allocation-run allocation-show \
        execution-validate execution-dry-run execution-show execution-history \
        positions-validate positions-snapshot positions-show \
        reservations-show reservations-validate \
        reconciliation-validate reconciliation-run reconciliation-show \
        exit-validate exit-evaluate exit-show exit-history positions-monitor \
        lint format typecheck check health config ibkr-connection ibkr-portfolio clean

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install the package with dev extras
	uv venv
	uv pip install -e '.[dev]'

# --- tests -----------------------------------------------------------------
test:  ## Run the whole test suite
	$(PYTEST)

test-unit:  ## Deterministic unit tests
	$(PYTEST) tests/unit

test-contract:  ## Workflow-boundary schema compatibility tests
	$(PYTEST) tests/contract

test-agents:  ## AI agent test suites (Milestone 4+). Needs no API key.
	$(PYTEST) tests/agents

test-universe:  ## Universe selection: config, filters, point-in-time, snapshots
	$(PYTEST) tests/universe

test-research:  ## Market research: evidence, dedup, point-in-time, validation, CLI
	$(PYTEST) tests/research

test-strategy:  ## Strategy stage: registry, agent boundary, validation, service
	$(PYTEST) tests/strategy

test-strategies:  ## One suite per strategy specification (Milestone 6)
	$(PYTEST) tests/strategies

test-contract-selection:  ## Deterministic contract selection: policy, point-in-time, determinism
	$(PYTEST) tests/contract_selection

test-allocation:  ## Campaign allocation tests (Milestone 7)
	$(PYTEST) tests/allocation

test-allocation-unit:  ## Allocation engine, scorer and quantity arithmetic only
	$(PYTEST) tests/allocation -m unit

test-allocation-integration:  ## Research to allocation, end to end, simulated broker
	$(PYTEST) tests/integration/test_research_to_allocation.py

test-risk:  ## Risk engine tests (Milestone 7)
	$(PYTEST) tests/risk

test-execution:  ## Execution tests (Milestone 8). Submits no orders.
	$(PYTEST) tests/execution

test-execution-unit:  ## Execution units: state machine, order builder, validation
	$(PYTEST) tests/execution -m unit

test-execution-integration:  ## Research to execution, end to end, simulated broker
	$(PYTEST) tests/integration/test_research_to_execution.py

test-paper-execution:  ## SUBMITS A REAL PAPER ORDER. Needs a running IB Gateway.
	@echo "This submits a REAL order to your IBKR PAPER account."
	@echo "It needs: ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true IBKR_READ_ONLY=false"
	@echo "Press Ctrl-C within 5 seconds to abort."
	@sleep 5
	ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true \
		$(PYTEST) tests/integration/test_paper_execution.py -m paper_execution -s

test-positions:  ## Position ledger tests (Milestone 9). Submits no orders.
	$(PYTEST) tests/positions

test-reservations:  ## Reservation lifecycle tests (Milestone 9). Submits no orders.
	$(PYTEST) tests/reservations

test-reconciliation:  ## Reconciliation tests (Milestone 9). Submits no orders.
	$(PYTEST) tests/reconciliation

test-position-integration:  ## Execution to fill to position to reconciliation, simulated
	$(PYTEST) tests/integration/test_execution_to_position.py \
		tests/integration/test_reconciliation_workflow.py

test-exit:  ## Exit management tests (Milestone 10). Submits no orders.
	$(PYTEST) tests/exit

test-exit-unit:  ## Exit units: lifecycle, trailing, expiration, thesis, policies
	$(PYTEST) tests/exit -m unit

test-exit-integration:  ## Exit to execution to reconciliation, simulated broker
	$(PYTEST) tests/exit -m integration \
		tests/integration/test_exit_to_execution_to_reconciliation.py

test-exit-safety:  ## The structural claims: no broker, no model, no leg-by-leg exit
	$(PYTEST) tests/exit/test_boundaries.py tests/exit/test_idempotency.py \
		tests/exit/test_point_in_time.py tests/exit/test_multi_leg.py

test-exit-cli:  ## The exit command group, positions monitor and test exit
	$(PYTEST) tests/exit/test_cli.py

test-paper-exit:  ## CAN SUBMIT A REAL PAPER SELL ORDER. Needs a running IB Gateway.
	@echo "This can submit a REAL exit order to your IBKR PAPER account."
	@echo "It needs: ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true IBKR_READ_ONLY=false"
	@echo "It sells a contract the account already holds, priced not to fill."
	@echo "Press Ctrl-C within 5 seconds to abort."
	@sleep 5
	ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true \
		$(PYTEST) tests/integration/test_paper_exit.py -m paper_execution -s

test-observability:  ## Telemetry: spans, privacy, cardinality, failure isolation
	$(PYTEST) tests/observability

test-scheduler:  ## Scheduler: cadence, isolation, idempotency, restart safety
	$(PYTEST) tests/operations/test_scheduler.py tests/operations/test_cron.py

test-pnl:  ## Realised profit and loss, settlement and daily loss (Milestone 11)
	$(PYTEST) tests/pnl

test-operations:  ## Every operations suite: scheduler, alerts, health, boundaries
	$(PYTEST) tests/operations

test-alerts:  ## Alert rules, and the claim that an alert cannot trade
	$(PYTEST) tests/operations/test_alerts.py

test-operations-integration:  ## Allocation to settlement to daily loss, end to end
	$(PYTEST) tests/integration/test_operations_lifecycle.py

test-broker:  ## Broker adapter and simulator tests (Milestone 2)
	$(PYTEST) tests/broker

test-data:  ## Data provider tests (Milestone 3)
	$(PYTEST) tests/data

test-integration:  ## Multi-component tests, simulated broker only
	$(PYTEST) tests/integration

test-e2e:  ## Full simulated lifecycle (Milestone 8)
	$(CLI) test e2e-dry-run

# --- quality ---------------------------------------------------------------
lint:  ## Lint with ruff
	.venv/bin/ruff check src tests

format:  ## Format with ruff
	.venv/bin/ruff format src tests

typecheck:  ## Type-check with mypy
	.venv/bin/mypy

check: lint typecheck test  ## Lint, type-check and test

# --- operations ------------------------------------------------------------
health:  ## Report configuration, mode and schema availability
	$(CLI) health

config:  ## Validate and print the YAML configuration
	$(CLI) config

universe-validate:  ## Validate universe config and data readiness (Milestone 4)
	$(CLI) universe validate

universe-run:  ## Select the research universe. Submits no orders (Milestone 4)
	$(CLI) universe run

universe-show:  ## Show the current universe (Milestone 4)
	$(CLI) universe show

research-validate:  ## Validate research config and data readiness (Milestone 5)
	$(CLI) research validate

research-run:  ## Research the universe. Submits no orders (Milestone 5)
	$(CLI) research run

research-show:  ## Show the latest research run (Milestone 5)
	$(CLI) research show

strategy-validate:  ## Validate the strategy registry and configuration (Milestone 6)
	$(CLI) strategy validate

strategy-run:  ## Choose a strategy per researched underlying. Submits no orders
	$(CLI) strategy run

strategy-show:  ## Show the latest strategy run (Milestone 6)
	$(CLI) strategy show

contract-validate:  ## Validate the deterministic contract-selection policy
	$(CLI) contract validate

contract-select:  ## Select contracts for the latest strategy run. Submits no orders
	$(CLI) contract select

contract-show:  ## Show the latest contract selection (Milestone 6)
	$(CLI) contract show

risk-validate:  ## Print the deterministic limits in force (Milestone 7)
	$(CLI) risk validate

risk-evaluate:  ## Evaluate risk for the latest contract run. Persists nothing
	$(CLI) risk evaluate

risk-capture-account:  ## Capture an account snapshot. Read-only; submits no orders
	$(CLI) risk capture-account

allocation-validate:  ## Validate the campaign envelope and allocation policy
	$(CLI) allocation validate

allocation-run:  ## Allocate campaign capital. Submits no orders (Milestone 7)
	$(CLI) allocation run

allocation-show:  ## Show the latest allocation run (Milestone 7)
	$(CLI) allocation show

execution-validate:  ## Print the execution policy in force (Milestone 8)
	$(CLI) execution validate

execution-dry-run:  ## Show what would be submitted. Contacts no broker (Milestone 8)
	$(CLI) execution run --dry-run

execution-show:  ## Show the latest execution run (Milestone 8)
	$(CLI) execution show

execution-history:  ## List recorded executions (Milestone 8)
	$(CLI) execution history

positions-validate:  ## Print the position ledger policy in force (Milestone 9)
	$(CLI) positions validate

positions-snapshot:  ## Capture what the broker holds. Read-only; submits no orders
	$(CLI) positions snapshot

positions-show:  ## Show the latest broker position snapshot (Milestone 9)
	$(CLI) positions show

reservations-show:  ## Show committed campaign capital (Milestone 9)
	$(CLI) reservations show

reservations-validate:  ## Show what would move, without moving it (Milestone 9)
	$(CLI) reservations validate

reconciliation-validate:  ## Print the reconciliation policy in force (Milestone 9)
	$(CLI) reconciliation validate

reconciliation-run:  ## Compare records against broker reality. Places no orders
	$(CLI) reconciliation run

reconciliation-show:  ## Show the latest reconciliation (Milestone 9)
	$(CLI) reconciliation show

exit-validate:  ## Print the exit policy and per-strategy narrowing (Milestone 10)
	$(CLI) exit validate

exit-evaluate:  ## Decide whether open positions should close. Submits no orders
	$(CLI) exit evaluate

exit-show:  ## Show the latest exit run or decision (Milestone 10)
	$(CLI) exit show

exit-history:  ## List recorded exit evaluations (Milestone 10)
	$(CLI) exit history

positions-monitor:  ## Evaluate every open position for exit. Submits no orders
	$(CLI) positions monitor

# Deliberately no `execution-run` or `exit-run` target. Submitting requires
# `--confirm` on the command line, and a make target that wrapped either would
# be a way to open — or close — a position by typing four characters.

ops-health:  ## Trading health and observability health, separately (Milestone 11)
	$(CLI) ops health

ops-scheduler-plan:  ## What would run now, and what fires next. Side-effect free
	$(CLI) ops scheduler plan

ops-jobs:  ## Registered jobs and their run history (Milestone 11)
	$(CLI) ops jobs

ops-alerts:  ## Recorded operational alerts. Alerts notify; they never trade
	$(CLI) ops alerts

ops-metrics:  ## The telemetry configuration, the metrics and the cardinality guard
	$(CLI) ops metrics

pnl-show:  ## Realised results, from broker-confirmed fills only (Milestone 11)
	$(CLI) pnl show

pnl-history:  ## List realised results, newest first
	$(CLI) pnl history

pnl-settle:  ## Compute results and return capital for confirmed-closed positions
	$(CLI) pnl settle

# Deliberately no `ops-scheduler-start` target. Starting the loop is a decision
# an operator makes deliberately, and the one job that can place an order needs
# two switches on top of it.

observability-validate:  ## Validate the observability compose profile and configs
	docker compose --profile observability config --quiet && echo "compose OK"

observability-up:  ## Start the OPTIONAL telemetry stack. Trading runs without it
	docker compose --profile observability up -d
	@echo "Grafana:     http://localhost:$${GRAFANA_PORT:-3000}  (admin / $${GRAFANA_ADMIN_PASSWORD:-admin})"
	@echo "Collector:   http://localhost:$${OTLP_HTTP_PORT:-4318}  (OTLP in)"
	@echo "Set OBSERVABILITY_ENABLED=true to export to it."

observability-down:  ## Stop the telemetry stack. Trading is unaffected
	docker compose --profile observability down

ibkr-connection:  ## Read-only IBKR connection test (Milestone 2)
	$(CLI) test ibkr-connection

ibkr-portfolio:  ## Read-only IBKR portfolio read (Milestone 2)
	$(CLI) test ibkr-portfolio

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
