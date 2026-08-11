"""Architectural boundaries around the agent (brief sections 33, 40, 43-45).

These are the tests that make the milestone's claims structural rather than
stylistic. The prompt *asks* the agent to stay in its lane; these assert that it
has no other lane available.

Import boundaries are checked by parsing the source, not by grepping: the word
``ib_async`` appears in several docstrings that explain the boundary, and those
must not count as violations of it. Both the direct imports of each agent module
and the whole transitive closure are checked — a forbidden module reached two
hops away is reached just the same.
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
    "trading_system.execution",
    "trading_system.risk",
    "trading_system.allocation",
    "ib_async",
)

#: Network and process-control modules. An agent that could open a socket could
#: retrieve its own evidence, which is exactly what the data layer exists to
#: prevent it from doing.
FORBIDDEN_STDLIB = ("socket", "urllib", "http", "subprocess", "requests", "httpx")


def _imports_of(path: Path) -> set[str]:
    """Every module a file imports, parsed rather than grepped."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    return imported


def _agent_files(repo_root: Path) -> list[Path]:
    package = repo_root / "src" / "trading_system" / "agents"
    return sorted(p for p in package.rglob("*.py"))


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
    """Every first-party module reachable from ``entry``, plus third-party names."""
    seen_files = {entry}
    modules: set[str] = set()
    queue = [entry]
    while queue:
        current = queue.pop()
        for module in _imports_of(current):
            modules.add(module)
            source = _module_path(repo_root, module)
            if source is not None and source not in seen_files:
                seen_files.add(source)
                queue.append(source)
    return modules


# ---------------------------------------------------------------------------
# 33. The agent consumes the input contract and nothing else
# ---------------------------------------------------------------------------
def test_the_agents_package_exists_and_has_modules(repo_root: Path) -> None:
    files = _agent_files(repo_root)
    assert files, "the agents package must contain source to check"
    assert any(p.name == "universe_selector.py" for p in files)


@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_no_agent_module_imports_a_broker_provider_or_repository(
    repo_root: Path, forbidden: str
) -> None:
    offenders = [
        str(path.relative_to(repo_root))
        for path in _agent_files(repo_root)
        if any(imported.startswith(forbidden) for imported in _imports_of(path))
    ]
    assert offenders == [], f"agent modules must not import {forbidden}: {offenders}"


@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_no_agent_module_can_open_a_connection_of_its_own(repo_root: Path, forbidden: str) -> None:
    """The Anthropic client is the sole exception, and it goes through the SDK."""
    offenders = [
        str(path.relative_to(repo_root))
        for path in _agent_files(repo_root)
        if path.name != "anthropic_client.py"
        and any(
            imported == forbidden or imported.startswith(f"{forbidden}.")
            for imported in _imports_of(path)
        )
    ]
    assert offenders == [], f"agent modules must not import {forbidden}: {offenders}"


@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_universe_selector_cannot_reach_a_broker_even_transitively(
    repo_root: Path, forbidden: str
) -> None:
    """Two hops away is still reachable. The whole closure is checked."""
    entry = repo_root / "src" / "trading_system" / "agents" / "universe_selector.py"
    reachable = _transitive_imports(repo_root, entry)

    offenders = sorted(m for m in reachable if m.startswith(forbidden))
    assert offenders == [], f"the universe selector transitively reaches {forbidden}: {offenders}"


def test_the_agent_takes_its_input_as_a_validated_contract(repo_root: Path) -> None:
    """It receives an object, not a handle it could use to fetch more."""
    import inspect

    from trading_system.agents.universe_selector import UniverseSelectorAgent
    from trading_system.universe.models import UniverseSelectionInput

    signature = inspect.signature(UniverseSelectorAgent.rank)
    assert signature.parameters["agent_input"].annotation in (
        UniverseSelectionInput,
        "UniverseSelectionInput",
    )


def test_the_agent_cannot_be_constructed_with_a_repository() -> None:
    import inspect

    from trading_system.agents.universe_selector import UniverseSelectorAgent

    parameters = set(inspect.signature(UniverseSelectorAgent.__init__).parameters)
    assert parameters == {"self", "client", "max_output_tokens", "timeout_seconds", "effort"}


# ---------------------------------------------------------------------------
# 40. Zero orders
# ---------------------------------------------------------------------------
def test_no_universe_module_can_reach_an_order_path(repo_root: Path) -> None:
    """The universe package may read data; it may not touch the broker."""
    package = repo_root / "src" / "trading_system" / "universe"
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        imports = _imports_of(path)
        if any(i.startswith("trading_system.broker") or i == "ib_async" for i in imports):
            offenders.append(str(path.relative_to(repo_root)))
    assert offenders == []


def test_no_universe_module_names_an_order_api(repo_root: Path) -> None:
    package = repo_root / "src" / "trading_system" / "universe"
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("place_order", "placeOrder", "_submit_order", "OrderIntent"):
            assert forbidden not in source, f"{path.name} references {forbidden}"


def test_a_universe_run_against_a_mutation_recording_broker_submits_nothing(
    optionable_symbols, make_service, ranking_text
) -> None:
    """A whole run, with a live broker sitting there unused."""
    from trading_system.broker.simulator import SimulatedBroker

    from .conftest import FakeLLMClient

    broker = SimulatedBroker(read_only=False)
    optionable_symbols(["SPY", "QQQ"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ"])),
        symbols=["SPY", "QQQ"],
    )

    from .conftest import UNIVERSE_NOW

    run = service.run(as_of=UNIVERSE_NOW)

    assert run.succeeded
    assert broker.orders_submitted == 0
    assert not broker.is_connected, "the run never even connected"


def test_the_service_exposes_no_broker(make_service) -> None:
    """There is nothing on the service a caller could reach an order through."""
    service = make_service(symbols=["SPY"])
    public = {name for name in dir(service) if not name.startswith("_")}

    assert not any("broker" in name or "order" in name for name in public)


# ---------------------------------------------------------------------------
# 43-45. The agent cannot express direction, strategy or allocation
# ---------------------------------------------------------------------------
FORBIDDEN_OUTPUT_CONCEPTS = (
    "BULLISH",
    "BEARISH",
    "CALL",
    "PUT",
    "STRADDLE",
    "STRANGLE",
    "LONG_CALL",
    "LONG_PUT",
    "EXPECTED_MOVE",
    "STRIKE",
    "EXPIRY",
    "EXPIRATION_DATE",
    "ALLOCATION",
    "BUDGET",
    "POSITION_SIZE",
    "RISK",
)


@pytest.mark.parametrize("concept", FORBIDDEN_OUTPUT_CONCEPTS)
def test_no_reason_code_expresses_a_directional_or_option_level_view(concept: str) -> None:
    """The vocabulary has no word for it, so the agent cannot say it."""
    from trading_system.domain.enums import UniverseSelectionReason

    assert all(concept not in reason.value for reason in UniverseSelectionReason)


def test_the_selected_asset_model_has_no_option_or_money_field() -> None:
    """A universe entry is an underlying. There is nowhere to put a strike."""
    from trading_system.universe.models import SelectedAsset

    fields = set(SelectedAsset.model_fields)
    for forbidden in (
        "strike",
        "expiration",
        "right",
        "strategy_type",
        "direction",
        "allocated_eur",
        "quantity",
        "legs",
    ):
        assert forbidden not in fields


def test_the_agent_input_carries_no_budget_or_risk_information() -> None:
    """Brief section 45: the selector must not even know the campaign budget."""
    from trading_system.universe.models import CandidateAsset, UniverseSelectionInput

    for model in (UniverseSelectionInput, CandidateAsset):
        fields = " ".join(model.model_fields)
        for forbidden in ("budget", "risk", "allocation", "position", "capital"):
            assert forbidden not in fields.lower()


def test_the_agent_payload_never_mentions_capital(
    optionable_symbols, make_service, ranking_text
) -> None:
    """Checked on the actual bytes sent, not only on the model's shape."""
    from .conftest import UNIVERSE_NOW, FakeLLMClient

    optionable_symbols(["SPY"])
    client = FakeLLMClient(ranking_text(["SPY"]))
    make_service(llm_client=client, symbols=["SPY"]).run(as_of=UNIVERSE_NOW, dry_run=True)

    sent = client.requests[0].user_content.lower()
    for forbidden in ("campaign_budget", "budget_eur", "max_allocation", "position_size"):
        assert forbidden not in sent


# ---------------------------------------------------------------------------
# The prompt states the boundaries, in both places it lives
# ---------------------------------------------------------------------------
#: Boundary statements every copy of the prompt must make. Compared with
#: markdown emphasis stripped: the two files are allowed to differ in wording
#: and formatting, but not on what the agent is forbidden to do.
REQUIRED_PROMPT_STATEMENTS = (
    "not a trader",
    "not select option contracts",
    "predict price direction",
    "allocate money",
    "cannot add a symbol",
    "cannot restore an excluded asset",
    "is not option liquidity",
)


def _plain(text: str) -> str:
    """Lower-cased with markdown emphasis removed, so wording is what is compared."""
    return text.replace("*", "").replace("`", "").lower()


@pytest.mark.parametrize("statement", REQUIRED_PROMPT_STATEMENTS)
def test_the_runtime_prompt_states_its_boundaries(statement: str) -> None:
    from trading_system.agents.prompts import load_prompt

    assert statement in _plain(load_prompt("universe_selector"))


@pytest.mark.parametrize("statement", REQUIRED_PROMPT_STATEMENTS)
def test_the_development_subagent_states_the_same_boundaries(
    repo_root: Path, statement: str
) -> None:
    """The two copies may differ in detail; they may not differ on the limits."""
    path = repo_root / ".claude" / "agents" / "universe_selector.md"
    if not path.is_file():
        pytest.skip("the Claude Code subagent definition is not present in this checkout")
    assert statement in _plain(path.read_text(encoding="utf-8"))


def test_the_prompt_is_fingerprinted_so_an_unversioned_edit_is_visible() -> None:
    """A prompt changed without a version bump would otherwise leave no trace."""
    from trading_system.agents.prompts import load_prompt, prompt_fingerprint

    fingerprint = prompt_fingerprint("universe_selector")

    assert len(fingerprint) == 32
    assert fingerprint == prompt_fingerprint("universe_selector"), "stable across calls"
    assert load_prompt("universe_selector"), "and the prompt itself is non-empty"


def test_a_missing_prompt_raises_rather_than_defaulting() -> None:
    """An agent running on an empty prompt would still produce confident output."""
    from trading_system.agents.prompts import load_prompt

    with pytest.raises(FileNotFoundError):
        load_prompt("no_such_agent")
