.DEFAULT_GOAL := help
PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
CLI := $(PYTHON) -m trading_system.cli

.PHONY: help install test test-unit test-contract test-agents test-strategies \
        test-allocation test-risk test-broker test-data test-integration test-e2e \
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

test-agents:  ## AI agent test suites (Milestone 4+)
	$(PYTEST) tests/agents

test-strategies:  ## Strategy test suites (Milestone 6)
	$(PYTEST) tests/strategies

test-allocation:  ## Campaign allocation tests (Milestone 7)
	$(PYTEST) tests/allocation

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

ibkr-connection:  ## Read-only IBKR connection test (Milestone 2)
	$(CLI) test ibkr-connection

ibkr-portfolio:  ## Read-only IBKR portfolio read (Milestone 2)
	$(CLI) test ibkr-portfolio

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
