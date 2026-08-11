"""Architectural boundaries around the research agent (brief sections 34, 51, 52).

These are the tests that make the milestone's claims structural rather than
stylistic. The prompt *asks* the agent to stay in its lane; these assert that it
has no other lane available.

Import boundaries are checked by parsing the source, not by grepping: the words
``broker`` and ``ib_async`` appear in several docstrings that explain the
boundary, and those must not count as violations of it. Both the direct imports
of each agent module and the whole transitive closure are checked — a forbidden
module reached two hops away is reached just the same.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Modules an agent must never reach — a broker, a data provider, or the
#: filesystem internals of the data layer. ``data.models`` and ``data.hashing``
#: are deliberately absent: they are pure canonical value types and a pure
#: function, which is what an input contract is *made of*.
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
    "trading_system.execution",
    "trading_system.risk",
    "trading_system.allocation",
    "ib_async",
)

#: Network and process-control modules. An agent that could open a socket could
#: retrieve its own evidence, which is exactly what the data layer exists to
#: prevent it from doing.
FORBIDDEN_STDLIB = ("socket", "urllib", "http", "subprocess", "requests", "httpx")

AGENT_MODULE = "market_researcher.py"


def _imports_of(path: Path) -> set[str]:
    """Every module a file imports **at runtime**, parsed rather than grepped.

    ``if TYPE_CHECKING:`` bodies are skipped because they genuinely never
    execute — an annotation-only import creates no runtime edge, and counting
    one would make the honest, deliberate use of the pattern look like a
    boundary violation.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking_guard(child.test):
                for orelse in child.orelse:  # the runtime branch, if any
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
    """Whether an ``if`` guards a typing-only block."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _module_path(repo_root: Path, module: str) -> Path | None:
    """Locate a first-party module's source file, if it is one."""
    if not module.startswith("trading_system"):
        return None
    base = repo_root / "src" / Path(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _transitive_imports(repo_root: Path, entry: Path) -> set[str]:
    """Every first-party module reachable from ``entry``, plus third-party names.

    Package ``__init__`` files are followed too, because importing
    ``a.b.models`` executes ``a.b.__init__`` — a package that eagerly imported
    a repository would drag one in behind the agent's back.
    """
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


def _ancestor_packages(module: str) -> list[str]:
    """``a.b.c`` -> ``['a.b', 'a']``. Importing a module runs its packages."""
    parts = module.split(".")
    return [".".join(parts[:index]) for index in range(len(parts) - 1, 0, -1)]


def _agent_file(repo_root: Path) -> Path:
    return repo_root / "src" / "trading_system" / "agents" / AGENT_MODULE


# ---------------------------------------------------------------------------
# 34. The agent consumes the input contract and nothing else
# ---------------------------------------------------------------------------
def test_the_research_agent_module_exists(repo_root: Path) -> None:
    assert _agent_file(repo_root).is_file()


@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_agent_does_not_import_a_broker_provider_or_repository(
    repo_root: Path, forbidden: str
) -> None:
    offenders = sorted(
        module for module in _imports_of(_agent_file(repo_root)) if module.startswith(forbidden)
    )
    assert offenders == [], f"the research agent must not import {forbidden}: {offenders}"


@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_agent_cannot_open_a_connection_of_its_own(repo_root: Path, forbidden: str) -> None:
    offenders = sorted(
        module
        for module in _imports_of(_agent_file(repo_root))
        if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"the research agent must not import {forbidden}: {offenders}"


# ---------------------------------------------------------------------------
# 51. Two hops away is still reachable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_agent_cannot_reach_a_broker_or_repository_transitively(
    repo_root: Path, forbidden: str
) -> None:
    """The whole closure is checked, including package ``__init__`` files.

    ``research/__init__.py`` loads the service and the input assembler lazily
    precisely so this holds: the agent imports the research *contract models*
    without dragging the data repository in behind them.
    """
    reachable = _transitive_imports(repo_root, _agent_file(repo_root))

    offenders = sorted(module for module in reachable if module.startswith(forbidden))
    assert offenders == [], f"the research agent transitively reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_agent_cannot_reach_a_socket_transitively(repo_root: Path, forbidden: str) -> None:
    reachable = _transitive_imports(repo_root, _agent_file(repo_root))

    offenders = sorted(
        module for module in reachable if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"the research agent transitively reaches {forbidden}: {offenders}"


def test_the_agent_takes_its_input_as_a_validated_contract() -> None:
    """It receives an object, not a handle it could use to fetch more."""
    import inspect

    from trading_system.agents.market_researcher import MarketResearcherAgent
    from trading_system.research.models import ResearchInput

    signature = inspect.signature(MarketResearcherAgent.research)
    assert signature.parameters["research_input"].annotation in (
        ResearchInput,
        "ResearchInput",
    )


def test_the_agent_cannot_be_constructed_with_a_repository() -> None:
    import inspect

    from trading_system.agents.market_researcher import MarketResearcherAgent

    parameters = set(inspect.signature(MarketResearcherAgent.__init__).parameters)
    assert parameters == {
        "self",
        "client",
        "config",
        "max_output_tokens",
        "timeout_seconds",
        "effort",
    }


def test_the_agent_depends_on_the_protocol_not_on_a_vendor() -> None:
    """Brief section 35: LLMClient, never AnthropicClient."""
    import inspect

    from trading_system.agents.base import LLMClient
    from trading_system.agents.market_researcher import MarketResearcherAgent

    annotation = inspect.signature(MarketResearcherAgent.__init__).parameters["client"].annotation
    assert annotation in (LLMClient, "LLMClient")


def test_the_agent_module_names_no_vendor_client(repo_root: Path) -> None:
    source = _agent_file(repo_root).read_text(encoding="utf-8")

    assert "AnthropicLLMClient" not in source
    assert "import anthropic" not in source


# ---------------------------------------------------------------------------
# 52. Zero orders, structurally
# ---------------------------------------------------------------------------
def test_no_research_module_can_reach_an_order_path(repo_root: Path) -> None:
    """The research package may read data; it may not touch the broker."""
    package = repo_root / "src" / "trading_system" / "research"
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        imports = _imports_of(path)
        if any(i.startswith("trading_system.broker") or i == "ib_async" for i in imports):
            offenders.append(str(path.relative_to(repo_root)))
    assert offenders == []


def test_no_research_module_names_an_order_api(repo_root: Path) -> None:
    package = repo_root / "src" / "trading_system" / "research"
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("place_order", "placeOrder", "_submit_order", "OrderIntent"):
            assert forbidden not in source, f"{path.name} references {forbidden}"


# ---------------------------------------------------------------------------
# 23, 43-45. The output vocabulary cannot express a trade
# ---------------------------------------------------------------------------
FORBIDDEN_OUTPUT_FIELDS = (
    "strike",
    "expiration",
    "expiry",
    "right",
    "delta",
    "strategy_type",
    "legs",
    "quantity",
    "allocated_eur",
    "budget",
    "limit_price",
    "order_type",
)


@pytest.mark.parametrize("field", FORBIDDEN_OUTPUT_FIELDS)
def test_the_agent_output_has_no_field_for_a_trade(field: str) -> None:
    """There is nowhere to put a contract, a size or a price."""
    from trading_system.research.models import (
        AgentEventAssessment,
        AgentEvidenceAssessment,
        Catalyst,
        InvalidationCondition,
        ResearchAgentOutput,
        RiskAssessment,
    )

    for model in (
        ResearchAgentOutput,
        AgentEvidenceAssessment,
        AgentEventAssessment,
        Catalyst,
        RiskAssessment,
        InvalidationCondition,
    ):
        assert field not in model.model_fields, f"{model.__name__} exposes {field}"


@pytest.mark.parametrize("field", FORBIDDEN_OUTPUT_FIELDS)
def test_the_generation_schema_offers_no_field_for_a_trade(field: str) -> None:
    """Checked on the schema the model actually generates against."""
    import json

    from trading_system.agents.market_researcher import research_output_schema

    serialised = json.dumps(research_output_schema())
    assert f'"{field}"' not in serialised


def test_the_agent_output_states_no_probability() -> None:
    """Brief section 17: confidence is a band, never a percentage."""
    from trading_system.research.models import ResearchAgentOutput

    fields = " ".join(ResearchAgentOutput.model_fields).lower()
    for forbidden in ("probability", "percent", "pct", "odds"):
        assert forbidden not in fields


def test_the_agent_cannot_name_a_source_it_was_not_given() -> None:
    """There is no field for a source name, a URL or a publication date."""
    from trading_system.research.models import AgentEvidenceAssessment

    fields = set(AgentEvidenceAssessment.model_fields)
    for forbidden in (
        "source",
        "source_name",
        "url",
        "published_at",
        "retrieved_at",
        "snapshot_id",
    ):
        assert forbidden not in fields


def test_the_agent_cannot_restate_an_events_timing() -> None:
    """Timing comes from the input, so it cannot be restated incorrectly."""
    from trading_system.research.models import AgentEventAssessment

    fields = set(AgentEventAssessment.model_fields)
    for forbidden in ("announced_at", "expected_event_time", "source", "source_tier"):
        assert forbidden not in fields


# ---------------------------------------------------------------------------
# The prompt states the boundaries, in both places it lives
# ---------------------------------------------------------------------------
#: Boundary statements every copy of the prompt must make. Compared with
#: markdown emphasis stripped: the two files are allowed to differ in wording
#: and formatting, but not on what the agent is forbidden to do.
REQUIRED_PROMPT_STATEMENTS = (
    "not a trader",
    "not select option contracts",
    "not allocate money",
    "never invent",
    "cite evidence",
    "a and d are not the same claim",
    "a band, never a probability",
    "not available",
)


def _plain(text: str) -> str:
    """Lower-cased with markdown emphasis removed, so wording is what is compared."""
    return text.replace("*", "").replace("`", "").lower()


@pytest.mark.parametrize("statement", REQUIRED_PROMPT_STATEMENTS)
def test_the_runtime_prompt_states_its_boundaries(statement: str) -> None:
    from trading_system.agents.prompts import load_prompt

    assert statement in _plain(load_prompt("market_researcher"))


@pytest.mark.parametrize("statement", REQUIRED_PROMPT_STATEMENTS)
def test_the_development_subagent_states_the_same_boundaries(
    repo_root: Path, statement: str
) -> None:
    """The two copies may differ in detail; they may not differ on the limits."""
    path = repo_root / ".claude" / "agents" / "market_researcher.md"
    if not path.is_file():
        pytest.skip("the Claude Code subagent definition is not present in this checkout")
    assert statement in _plain(path.read_text(encoding="utf-8"))


def test_the_prompt_is_fingerprinted_so_an_unversioned_edit_is_visible() -> None:
    """A prompt changed without a version bump would otherwise leave no trace."""
    from trading_system.agents.prompts import load_prompt, prompt_fingerprint

    fingerprint = prompt_fingerprint("market_researcher")

    assert len(fingerprint) == 32
    assert fingerprint == prompt_fingerprint("market_researcher"), "stable across calls"
    assert load_prompt("market_researcher"), "and the prompt itself is non-empty"


def test_the_prompt_ships_inside_the_package() -> None:
    """The container installs the package and has no checkout to read from."""
    from importlib.resources import files

    resource = files("trading_system.agents.prompts").joinpath("market_researcher.md")

    assert resource.is_file()


def test_the_prompt_explains_the_a_versus_d_distinction() -> None:
    """Brief section 4: the distinction must exist in the schema and the tests."""
    from trading_system.agents.prompts import load_prompt

    prompt = _plain(load_prompt("market_researcher"))

    assert "a and d are not the same claim" in prompt
    assert "no specific catalyst" in prompt or "needs no specific catalyst" in prompt
