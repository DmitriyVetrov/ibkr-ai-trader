.DEFAULT_GOAL := help
PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
CLI := $(PYTHON) -m trading_system.cli

.PHONY: help install test test-unit test-contract test-agents test-universe \
        test-research test-strategy test-strategies test-contract-selection \
        test-allocation test-allocation-unit test-allocation-integration test-risk \
        test-broker test-data test-integration \
        test-e2e universe-validate universe-run universe-show research-validate \
        research-run research-show strategy-validate strategy-run strategy-show \
        contract-validate contract-select contract-show \
        risk-validate risk-evaluate risk-capture-account \
        allocation-validate allocation-run allocation-show \
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

ibkr-connection:  ## Read-only IBKR connection test (Milestone 2)
	$(CLI) test ibkr-connection

ibkr-portfolio:  ## Read-only IBKR portfolio read (Milestone 2)
	$(CLI) test ibkr-portfolio

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
