"""Architectural boundaries around the strategy agent (brief sections 32, 40).

These are the tests that make the milestone's central claim structural rather
than stylistic. The prompt *asks* the agent to choose a strategy and not a
contract; these assert that it has no other option available — no chain to read
a strike from, no field to write one into, and no import through which either
could arrive.

Import boundaries are checked by parsing the source, not by grepping: the words
"broker", "chain" and "contract" appear in docstrings that explain the
boundary, and those must not count as violations of it. Both direct imports and
the whole transitive closure are checked, following package ``__init__`` files,
because importing ``a.b.c`` executes ``a.b.__init__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Modules the strategy agent must never reach. ``strategies.contract_selector``
#: and ``strategies.chain`` are the ones this milestone turns on: an agent that
#: could reach the selector could reach a chain, and an agent that can see a
#: chain can name a contract.
FORBIDDEN_MODULES = (
    "trading_system.broker",
    "trading_system.data.providers",
    "trading_system.data.repository",
    "trading_system.data.service",
    "trading_system.data.cache",
    "trading_system.data.collectors",
    "trading_system.research.context",
    "trading_system.research.store",
    "trading_system.research.service",
    "trading_system.strategies.chain",
    "trading_system.strategies.contract_selector",
    "trading_system.strategies.context",
    "trading_system.strategies.service",
    "trading_system.strategies.store",
    "trading_system.execution",
    "trading_system.risk",
    "trading_system.allocation",
    "trading_system.portfolio",
    "ib_async",
)

FORBIDDEN_STDLIB = ("socket", "urllib", "http", "subprocess", "requests", "httpx")

AGENT_MODULE = "strategy_selector.py"


def _imports_of(path: Path) -> set[str]:
    """Every module a file imports at runtime, parsed rather than grepped."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking_guard(child.test):
                for orelse in child.orelse:
                    walk(orelse)
                continue
            if isinstance(child, ast.Import):
                imported.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
                imported.add(child.module)
            walk(child)

    walk(tree)
    return imported


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _module_path(repo_root: Path, module: str) -> Path | None:
    if not module.startswith("trading_system"):
        return None
    base = repo_root / "src" / Path(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _ancestor_packages(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:index]) for index in range(len(parts) - 1, 0, -1)]


def _transitive_imports(repo_root: Path, entry: Path) -> set[str]:
    seen_files = {entry}
    modules: set[str] = set()
    queue = [entry]
    while queue:
        current = queue.pop()
        for module in _imports_of(current):
            modules.add(module)
            for candidate in (module, *_ancestor_packages(module)):
                source = _module_path(repo_root, candidate)
                if source is not None and source not in seen_files:
                    modules.add(candidate)
                    seen_files.add(source)
                    queue.append(source)
    return modules


def _agent_file(repo_root: Path) -> Path:
    return repo_root / "src" / "trading_system" / "agents" / AGENT_MODULE


# ---------------------------------------------------------------------------
# The agent module exists and imports nothing it must not
# ---------------------------------------------------------------------------
def test_the_strategy_agent_module_exists(repo_root: Path) -> None:
    assert _agent_file(repo_root).is_file()


@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_agent_does_not_import_a_selector_a_broker_or_a_repository(
    repo_root: Path, forbidden: str
) -> None:
    offenders = sorted(
        module for module in _imports_of(_agent_file(repo_root)) if module.startswith(forbidden)
    )
    assert offenders == [], f"the strategy agent must not import {forbidden}: {offenders}"


@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_agent_cannot_reach_a_selector_or_a_broker_transitively(
    repo_root: Path, forbidden: str
) -> None:
    """``strategies/__init__.py`` defers the selector for exactly this reason."""
    reachable = _transitive_imports(repo_root, _agent_file(repo_root))

    offenders = sorted(module for module in reachable if module.startswith(forbidden))
    assert offenders == [], f"the strategy agent transitively reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_agent_cannot_open_a_connection_of_its_own(repo_root: Path, forbidden: str) -> None:
    reachable = _transitive_imports(repo_root, _agent_file(repo_root))

    offenders = sorted(
        module for module in reachable if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"the strategy agent transitively reaches {forbidden}: {offenders}"


def test_the_agent_takes_its_input_as_a_validated_contract() -> None:
    """It receives an object, not a handle it could use to fetch a chain."""
    import inspect

    from trading_system.agents.strategy_selector import StrategySelectorAgent
    from trading_system.strategies.models import StrategySelectionInput

    signature = inspect.signature(StrategySelectorAgent.select)

    assert signature.parameters["selection_input"].annotation in (
        StrategySelectionInput,
        "StrategySelectionInput",
    )


def test_the_agent_cannot_be_constructed_with_a_repository() -> None:
    import inspect

    from trading_system.agents.strategy_selector import StrategySelectorAgent

    parameters = set(inspect.signature(StrategySelectorAgent.__init__).parameters)

    assert parameters == {
        "self",
        "client",
        "config",
        "max_output_tokens",
        "timeout_seconds",
        "effort",
    }


def test_the_agent_depends_on_the_protocol_not_on_a_vendor() -> None:
    import inspect

    from trading_system.agents.base import LLMClient
    from trading_system.agents.strategy_selector import StrategySelectorAgent

    annotation = inspect.signature(StrategySelectorAgent.__init__).parameters["client"].annotation

    assert annotation in (LLMClient, "LLMClient")


def test_the_agent_module_names_no_vendor_client(repo_root: Path) -> None:
    source = _agent_file(repo_root).read_text(encoding="utf-8")

    assert "AnthropicLLMClient" not in source
    assert "import anthropic" not in source


# ---------------------------------------------------------------------------
# 32. What the agent may see
# ---------------------------------------------------------------------------
FORBIDDEN_INPUT_FIELDS = (
    "strike",
    "strikes",
    "expiration",
    "expiry",
    "contract_id",
    "contracts",
    "chain",
    "option_chain",
    "delta",
    "implied_volatility",
    "quantity",
    "price",
    "premium",
    "limit_price",
    "budget",
    "allocation",
    "cash",
    "buying_power",
    "account",
    "positions",
    "portfolio",
)


@pytest.mark.parametrize("field", FORBIDDEN_INPUT_FIELDS)
def test_the_agent_input_has_no_field_for_a_contract_or_an_account(field: str) -> None:
    """There is nowhere for a chain, a balance or a size to appear."""
    from trading_system.strategies.models import (
        ResearchClaim,
        ResearchEventSummary,
        ResearchQualitySnapshot,
        ResearchSummary,
        StrategyOption,
        StrategySelectionInput,
    )

    for model in (
        StrategySelectionInput,
        ResearchSummary,
        ResearchClaim,
        ResearchEventSummary,
        ResearchQualitySnapshot,
        StrategyOption,
    ):
        assert field not in model.model_fields, f"{model.__name__} exposes {field}"


@pytest.mark.parametrize("field", FORBIDDEN_INPUT_FIELDS)
def test_the_agent_output_has_no_field_for_a_contract_or_a_size(field: str) -> None:
    from trading_system.strategies.models import StrategyAgentOutput

    assert field not in StrategyAgentOutput.model_fields


def test_the_agent_output_rejects_an_invented_field() -> None:
    """A response that adds a strike fails to parse rather than losing it."""
    import json

    from trading_system.agents.strategy_selector import (
        AgentInvalidOutputError,
        StrategySelectorAgent,
    )

    payload = {
        "run_id": "r",
        "symbol": "NVDA",
        "action": "BUY",
        "selected_strategy": "LONG_CALL",
        "confidence": "MEDIUM",
        "reasons": ["HYPOTHESIS_MATCH"],
        "rationale": "because",
        "strike": 190,
    }

    with pytest.raises(AgentInvalidOutputError):
        StrategySelectorAgent.parse(json.dumps(payload))


def test_the_generation_schema_offers_no_field_for_a_contract(make_report, system_config) -> None:
    """Checked on the schema the model actually generates against."""
    import json

    from trading_system.agents.strategy_selector import strategy_output_schema
    from trading_system.strategies.context import build_selection_input
    from trading_system.strategies.registry import StrategyRegistry

    registry = StrategyRegistry.from_config(system_config)
    report = make_report()
    assert report.hypothesis is not None
    selection_input = build_selection_input(
        run_id="strategy-run-test",
        report=report,
        eligible=registry.options_for(report.hypothesis),
    )

    serialised = json.dumps(strategy_output_schema(selection_input))

    for field in ("strike", "expiration", "contract_id", "quantity", "limit_price", "budget"):
        assert f'"{field}"' not in serialised


def test_the_generation_schema_only_offers_eligible_strategies(make_report, system_config) -> None:
    """A strategy the deterministic layer did not admit is inexpressible."""
    from trading_system.agents.strategy_selector import strategy_output_schema
    from trading_system.domain.enums import MarketHypothesis
    from trading_system.strategies.context import build_selection_input
    from trading_system.strategies.registry import StrategyRegistry

    registry = StrategyRegistry.from_config(system_config)
    report = make_report(hypothesis=MarketHypothesis.B)
    selection_input = build_selection_input(
        run_id="strategy-run-test",
        report=report,
        eligible=registry.options_for(MarketHypothesis.B),
    )

    schema = strategy_output_schema(selection_input)

    assert schema["properties"]["selected_strategy"]["enum"] == ["LONG_CALL", None]


def test_the_payload_sent_to_the_model_carries_no_chain(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    """Asserted on what was actually sent, not on what the model was asked."""
    import json

    from .conftest import FakeLLMClient

    researchable("NVDA")
    store_research([make_report()])
    client = FakeLLMClient(decision_text)

    make_service(llm_client=client).run()

    assert client.requests, "the agent was consulted"
    payload = json.loads(client.requests[0].user_content)
    assert set(payload) == {
        "instruction",
        "run_id",
        "symbol",
        "as_of",
        "research",
        "eligible_strategies",
    }
    serialised = json.dumps(payload).lower()
    for forbidden in ("contract_id", "option_chain", "buying_power", "net_liquidation"):
        assert forbidden not in serialised


def test_the_input_carries_no_date_the_agent_could_echo() -> None:
    """An expiration is a date. The agent is shown days, never dates."""
    from trading_system.strategies.models import ResearchEventSummary

    fields = set(ResearchEventSummary.model_fields)

    assert "event_date" not in fields
    assert "event_time" not in fields
    assert "expected_event_time" not in fields
    assert "days_until" in fields


# ---------------------------------------------------------------------------
# The prompt states the boundaries, in both places it lives
# ---------------------------------------------------------------------------
#: Boundary statements every copy of the prompt must make. Compared with
#: markdown emphasis stripped: the two files may differ in wording, but not on
#: what the agent is forbidden to do.
REQUIRED_PROMPT_STATEMENTS = (
    "not select option contracts",
    "not select a strike",
    "not select an expiration",
    "not decide quantity",
    "not allocate money",
    "no_trade",
    "never invent",
    "a band, never a probability",
)


def _plain(text: str) -> str:
    return text.replace("*", "").replace("`", "").lower()


@pytest.mark.parametrize("statement", REQUIRED_PROMPT_STATEMENTS)
def test_the_runtime_prompt_states_its_boundaries(statement: str) -> None:
    from trading_system.agents.prompts import load_prompt

    assert statement in _plain(load_prompt("strategy_selector"))


@pytest.mark.parametrize("statement", REQUIRED_PROMPT_STATEMENTS)
def test_the_development_subagent_states_the_same_boundaries(
    repo_root: Path, statement: str
) -> None:
    path = repo_root / ".claude" / "agents" / "strategy_selector.md"
    if not path.is_file():
        pytest.skip("the Claude Code subagent definition is not present in this checkout")
    assert statement in _plain(path.read_text(encoding="utf-8"))


def test_the_prompt_is_fingerprinted_so_an_unversioned_edit_is_visible() -> None:
    from trading_system.agents.prompts import load_prompt, prompt_fingerprint

    fingerprint = prompt_fingerprint("strategy_selector")

    assert len(fingerprint) == 32
    assert fingerprint == prompt_fingerprint("strategy_selector")
    assert load_prompt("strategy_selector")


def test_the_prompt_ships_inside_the_package() -> None:
    """The container installs the package and has no checkout to read from."""
    from importlib.resources import files

    resource = files("trading_system.agents.prompts").joinpath("strategy_selector.md")

    assert resource.is_file()


# ---------------------------------------------------------------------------
# Every strategy has a written specification
# ---------------------------------------------------------------------------
REQUIRED_SKILL_SECTIONS = (
    "## Purpose",
    "## Applicable hypotheses",
    "## Required data",
    "## Legs",
    "## Expiration policy",
    "## Strike policy",
    "## Liquidity",
    "## Invalidation",
    "## Prohibited behaviour",
    "## Failure conditions",
)

SKILL_FILES = ("long_call.md", "long_put.md", "straddle.md", "strangle.md")


@pytest.mark.parametrize("name", SKILL_FILES)
def test_every_strategy_has_a_skill_document(repo_root: Path, name: str) -> None:
    assert (repo_root / "skills" / "strategies" / name).is_file()


@pytest.mark.parametrize("name", SKILL_FILES)
@pytest.mark.parametrize("section", REQUIRED_SKILL_SECTIONS)
def test_every_skill_document_covers_the_required_sections(
    repo_root: Path, name: str, section: str
) -> None:
    text = (repo_root / "skills" / "strategies" / name).read_text(encoding="utf-8")

    assert section in text, f"{name} is missing {section}"


@pytest.mark.parametrize("name", SKILL_FILES)
def test_a_skill_document_points_at_the_configuration_rather_than_copying_it(
    repo_root: Path, name: str
) -> None:
    """Executable rules live in code and config; prose that copied a number
    would be a second source of truth, and second sources drift."""
    text = (repo_root / "skills" / "strategies" / name).read_text(encoding="utf-8")

    assert "config/strategies/" in text
    assert "This document is a specification. It states no numbers" in text
