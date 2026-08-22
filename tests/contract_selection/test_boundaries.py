"""Architectural boundaries around the contract selector (brief section 40).

The claim this milestone rests on is that contract selection is *deterministic*.
A selector that could reach a model, a news feed or a broker would not be —
it would merely be deterministic today.

Import boundaries are checked by parsing the source, not by grepping: the words
"agent", "LLM" and "broker" appear in docstrings that explain the boundary, and
those must not count as violations of it. Both direct imports and the whole
transitive closure are checked, following package ``__init__`` files, because
importing ``a.b.c`` executes ``a.b.__init__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Modules the contract selector must never reach. The LLM ones are the point:
#: a selector that could consult a model is not a deterministic selector, and
#: "one model call per strike" is the cost failure the brief names explicitly.
FORBIDDEN_MODULES = (
    "trading_system.agents",
    "trading_system.broker",
    "trading_system.data.providers",
    "trading_system.data.service",
    "trading_system.data.collectors",
    "trading_system.execution",
    "anthropic",
    "ib_async",
)

#: Network and process-control modules. A selector that could open a socket
#: could fetch its own quotes, which is exactly what the one-reliable-round-trip
#: constraint from Milestone 2 makes unsafe.
FORBIDDEN_STDLIB = ("socket", "urllib", "http", "subprocess", "requests", "httpx")

SELECTOR_MODULES = ("contract_selector.py", "chain.py")


def _imports_of(path: Path) -> set[str]:
    """Every module a file imports at runtime, parsed rather than grepped.

    ``if TYPE_CHECKING:`` bodies are skipped: an annotation-only import creates
    no runtime edge, and counting one would make the deliberate use of the
    pattern look like a violation.
    """
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


def _selector_file(repo_root: Path, name: str) -> Path:
    return repo_root / "src" / "trading_system" / "strategies" / name


# ---------------------------------------------------------------------------
# No model, no broker, no provider
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", SELECTOR_MODULES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_selector_does_not_import_an_agent_or_a_broker(
    repo_root: Path, module: str, forbidden: str
) -> None:
    offenders = sorted(
        name
        for name in _imports_of(_selector_file(repo_root, module))
        if name.startswith(forbidden)
    )
    assert offenders == [], f"{module} must not import {forbidden}: {offenders}"


@pytest.mark.parametrize("module", SELECTOR_MODULES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_selector_cannot_reach_an_agent_or_a_broker_transitively(
    repo_root: Path, module: str, forbidden: str
) -> None:
    reachable = _transitive_imports(repo_root, _selector_file(repo_root, module))

    offenders = sorted(name for name in reachable if name.startswith(forbidden))
    assert offenders == [], f"{module} transitively reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("module", SELECTOR_MODULES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_selector_cannot_open_a_connection_of_its_own(
    repo_root: Path, module: str, forbidden: str
) -> None:
    reachable = _transitive_imports(repo_root, _selector_file(repo_root, module))

    offenders = sorted(
        name for name in reachable if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"{module} reaches {forbidden}: {offenders}"


def test_the_selector_module_names_no_model_client(repo_root: Path) -> None:
    source = _selector_file(repo_root, "contract_selector.py").read_text(encoding="utf-8")

    for forbidden in ("LLMClient", "AnthropicLLMClient", "StructuredRequest", "load_prompt"):
        assert forbidden not in source, f"the contract selector references {forbidden}"


def test_no_strategy_module_can_reach_an_order_path(repo_root: Path) -> None:
    """The whole package may read data; none of it may touch the broker."""
    package = repo_root / "src" / "trading_system" / "strategies"
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        imports = _imports_of(path)
        if any(name.startswith("trading_system.broker") or name == "ib_async" for name in imports):
            offenders.append(str(path.relative_to(repo_root)))
    assert offenders == []


def test_no_strategy_module_names_an_order_api(repo_root: Path) -> None:
    package = repo_root / "src" / "trading_system" / "strategies"
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("place_order", "placeOrder", "_submit_order", "OrderIntent"):
            assert forbidden not in source, f"{path.name} references {forbidden}"


# ---------------------------------------------------------------------------
# The selector is constructed from data and configuration only
# ---------------------------------------------------------------------------
def test_the_selector_takes_a_repository_and_configuration() -> None:
    import inspect

    from trading_system.strategies.contract_selector import ContractSelector

    parameters = set(inspect.signature(ContractSelector.__init__).parameters)

    assert parameters == {"self", "repository", "config", "calendar", "clock"}


def test_the_contract_selection_service_constructs_no_model_client(repo_root: Path) -> None:
    """The composition root itself is evidence: there is nowhere to call one."""
    import inspect

    from trading_system.strategies.service import ContractSelectionService

    parameters = set(inspect.signature(ContractSelectionService.__init__).parameters)

    assert "llm_client" not in parameters
    source = inspect.getsource(ContractSelectionService)
    assert "AnthropicLLMClient" not in source
    assert "_build_client" not in source


# ---------------------------------------------------------------------------
# 41. No capital allocation anywhere in this stage
# ---------------------------------------------------------------------------
FORBIDDEN_SIZING_FIELDS = (
    "quantity",
    "allocation",
    "allocated",
    "budget",
    "account_percentage",
    "campaign_percentage",
    "position_size",
    "buying_power",
    "cash",
)


@pytest.mark.parametrize("field", FORBIDDEN_SIZING_FIELDS)
def test_no_milestone_6_artifact_can_carry_a_position_size(field: str) -> None:
    """Brief section 41: how much is the risk and allocation engines' answer."""
    from trading_system.strategies.models import (
        ContractCostEstimate,
        ContractSelectionResult,
        SelectedLeg,
        StrategyAgentOutput,
        StrategyDecisionRecord,
        StrategySelectionInput,
    )

    for model in (
        StrategySelectionInput,
        StrategyAgentOutput,
        StrategyDecisionRecord,
        ContractSelectionResult,
        SelectedLeg,
        ContractCostEstimate,
    ):
        assert field not in model.model_fields, f"{model.__name__} exposes {field}"


def test_a_cost_estimate_is_not_a_position_size(priced_chain, select) -> None:
    """One unit of the structure, and nothing about how many to buy."""
    priced_chain()

    cost = select().cost

    assert cost is not None and cost.available
    assert cost.estimated_debit is not None
    assert not hasattr(cost, "quantity")
