"""Architectural boundaries around risk and allocation (brief section 37.8).

These are the tests that make Milestone 7's central claims structural rather
than stylistic. The claims are:

* **no broker.** Neither engine may reach one, directly or transitively, and
  neither service may construct one. This is a safety property, not only an
  architectural preference: Milestone 2 established that a second uncached round
  trip on one IBKR connection can go unanswered indefinitely, so a risk check
  that fetched its own account state could hang the process at the worst moment.
* **no model.** No agent, no LLM client, no prompt. An AI may not determine a
  quantity, an amount of money or whether a trade is permitted, and the way to
  guarantee that is to make a model unreachable rather than to ask it nicely.
* **no order.** Nothing under ``risk/`` or ``allocation/`` may name an order
  API or carry an order field.
* **no raw data.** The engines read Milestone 6 artifacts and stored snapshots.
  They do not reach a provider, a collector or a raw payload.

Import boundaries are checked by parsing the source, not by grepping: the words
"broker", "agent" and "order" appear in docstrings that explain these very
boundaries, and those must not count as violations of them. Both direct imports
and the whole transitive closure are checked, following package ``__init__``
files, because importing ``a.b.c`` executes ``a.b.__init__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Modules neither engine may reach. ``trading_system.agents`` is the one this
#: milestone turns on: an engine that could consult a model is an engine whose
#: limits a model could argue with.
FORBIDDEN_MODULES = (
    "trading_system.agents",
    "trading_system.broker",
    "trading_system.execution",
    "trading_system.data.providers",
    "trading_system.data.collectors",
    "trading_system.data.cache",
    "trading_system.data.service",
    "trading_system.data.repository",
    "anthropic",
    "ib_async",
)

#: Network and process-control modules. An engine that could open a socket
#: could fetch its own account state, which is exactly what the
#: one-reliable-round-trip constraint from Milestone 2 makes unsafe.
FORBIDDEN_STDLIB = ("socket", "urllib", "http", "subprocess", "requests", "httpx")

#: The pure engines. These must additionally reach no filesystem repository:
#: they are functions of their arguments, and a stored verdict has to be
#: reproducible from those arguments alone.
ENGINE_MODULES = (
    "risk/engine.py",
    "risk/guards.py",
    "risk/exposure.py",
    "risk/limits.py",
    "risk/models.py",
    "allocation/budget_allocator.py",
    "allocation/scorer.py",
    "allocation/models.py",
)

#: Every module in either package. These may reach a repository — the service
#: has to read a contract run from somewhere — but never a broker or a model.
PACKAGE_DIRECTORIES = ("risk", "allocation")


def _imports_of(path: Path) -> set[str]:
    """Every module a file imports at runtime, parsed rather than grepped.

    ``if TYPE_CHECKING:`` bodies are skipped: an annotation-only import creates
    no runtime edge, and counting one would make the deliberate use of the
    lazy-import pattern look like a violation.
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


def _source(repo_root: Path, relative: str) -> Path:
    return repo_root / "src" / "trading_system" / relative


def _package_files(repo_root: Path, package: str) -> list[Path]:
    return sorted((repo_root / "src" / "trading_system" / package).glob("*.py"))


# ---------------------------------------------------------------------------
# No broker, no model, anywhere in either package
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package", PACKAGE_DIRECTORIES)
@pytest.mark.parametrize(
    "forbidden",
    (
        "trading_system.agents",
        "trading_system.broker",
        "trading_system.execution",
        "anthropic",
        "ib_async",
    ),
)
def test_no_module_imports_an_agent_a_broker_or_an_order_path(
    repo_root: Path, package: str, forbidden: str
) -> None:
    offenders = [
        str(path.relative_to(repo_root))
        for path in _package_files(repo_root, package)
        if any(name.startswith(forbidden) for name in _imports_of(path))
    ]
    assert offenders == [], f"{package}/ imports {forbidden}: {offenders}"


@pytest.mark.parametrize("module", ENGINE_MODULES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_engines_cannot_reach_a_broker_or_a_model_transitively(
    repo_root: Path, module: str, forbidden: str
) -> None:
    reachable = _transitive_imports(repo_root, _source(repo_root, module))

    offenders = sorted(name for name in reachable if name.startswith(forbidden))
    assert offenders == [], f"{module} transitively reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("module", ENGINE_MODULES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_engines_cannot_open_a_connection_of_their_own(
    repo_root: Path, module: str, forbidden: str
) -> None:
    reachable = _transitive_imports(repo_root, _source(repo_root, module))

    offenders = sorted(
        name for name in reachable if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"{module} reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("package", PACKAGE_DIRECTORIES)
def test_no_module_names_an_order_api(repo_root: Path, package: str) -> None:
    for path in _package_files(repo_root, package):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "place_order",
            "placeOrder",
            "_submit_order",
            "OrderIntent",
            "cancel_order",
        ):
            assert forbidden not in source, f"{path.name} references {forbidden}"


@pytest.mark.parametrize("package", PACKAGE_DIRECTORIES)
def test_no_module_names_a_model_client(repo_root: Path, package: str) -> None:
    for path in _package_files(repo_root, package):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("LLMClient", "AnthropicLLMClient", "StructuredRequest", "load_prompt"):
            assert forbidden not in source, f"{path.name} references {forbidden}"


# ---------------------------------------------------------------------------
# The engines are pure
# ---------------------------------------------------------------------------
def test_the_risk_engine_takes_only_limits() -> None:
    import inspect

    from trading_system.risk.engine import RiskEngine

    assert set(inspect.signature(RiskEngine.__init__).parameters) == {"self", "limits"}


def test_the_allocation_engine_takes_only_limits_and_a_risk_engine() -> None:
    import inspect

    from trading_system.allocation.budget_allocator import AllocationEngine

    parameters = set(inspect.signature(AllocationEngine.__init__).parameters)

    assert parameters == {"self", "limits", "risk_engine"}


@pytest.mark.parametrize("module", ENGINE_MODULES)
def test_no_engine_reads_a_clock(repo_root: Path, module: str) -> None:
    """The decision instant is passed in, so a stored verdict is reproducible."""
    source = _source(repo_root, module).read_text(encoding="utf-8")

    for forbidden in ("datetime.now(", "date.today(", "utc_now(", "time.time("):
        assert forbidden not in source, f"{module} reads a clock: {forbidden}"


@pytest.mark.parametrize("module", ENGINE_MODULES)
def test_no_engine_touches_the_filesystem(repo_root: Path, module: str) -> None:
    reachable = _transitive_imports(repo_root, _source(repo_root, module))

    assert "trading_system.risk.store" not in reachable
    assert "trading_system.allocation.store" not in reachable


@pytest.mark.parametrize("module", ENGINE_MODULES)
def test_no_engine_uses_a_random_source(repo_root: Path, module: str) -> None:
    reachable = _transitive_imports(repo_root, _source(repo_root, module))

    assert "random" not in reachable
    assert "secrets" not in reachable


# ---------------------------------------------------------------------------
# The services construct no broker and no model client
# ---------------------------------------------------------------------------
def test_the_allocation_service_constructs_no_broker_and_no_model_client() -> None:
    """The composition root itself is the evidence: there is nowhere to call one."""
    import inspect

    from trading_system.allocation.service import AllocationService

    parameters = set(inspect.signature(AllocationService.__init__).parameters)

    assert "llm_client" not in parameters
    assert "broker" not in parameters
    source = inspect.getsource(AllocationService)
    for forbidden in ("build_broker", "AnthropicLLMClient", "_build_client", "place_order"):
        assert forbidden not in source


def test_the_allocation_service_reads_account_state_only_from_a_snapshot() -> None:
    import inspect

    from trading_system.allocation.service import AllocationService

    source = inspect.getsource(AllocationService)

    assert "get_account" not in source, "account state must arrive as a stored snapshot"
    assert "get_positions" not in source


def test_only_the_cli_turns_broker_state_into_an_account_snapshot(repo_root: Path) -> None:
    """The single broker retrieval boundary, asserted by where it lives.

    ``risk/account.py`` is a pure function over two domain models. The broker
    read itself happens in the CLI, which is already the layer allowed to hold
    a broker.
    """
    account_module = _source(repo_root, "risk/account.py")

    assert "get_account" not in account_module.read_text(encoding="utf-8")
    assert all(not name.startswith("trading_system.broker") for name in _imports_of(account_module))


# ---------------------------------------------------------------------------
# No Milestone 7 artifact can express an order
# ---------------------------------------------------------------------------
FORBIDDEN_ORDER_FIELDS = (
    "order_type",
    "side",
    "limit_price",
    "stop_price",
    "time_in_force",
    "broker_order_id",
    "execution_price",
    "filled_quantity",
)


@pytest.mark.parametrize("field", FORBIDDEN_ORDER_FIELDS)
def test_no_milestone_7_artifact_can_carry_an_order_instruction(field: str) -> None:
    """Brief section 42: M7 authorises capital; M8 decides how to execute it."""
    from trading_system.allocation.models import (
        AllocationRunResult,
        CampaignAllocation,
        QuantityCalculation,
    )
    from trading_system.risk.models import (
        AccountSnapshot,
        AllocationCandidate,
        CampaignSnapshot,
        CandidateLeg,
        RiskEvaluation,
    )

    for model in (
        AllocationCandidate,
        CandidateLeg,
        CampaignSnapshot,
        AccountSnapshot,
        RiskEvaluation,
        CampaignAllocation,
        QuantityCalculation,
        AllocationRunResult,
    ):
        assert field not in model.model_fields, f"{model.__name__} exposes {field}"


def test_no_risk_or_allocation_artifact_carries_a_model_rationale() -> None:
    """Nothing an agent wrote may appear on a deterministic verdict."""
    from trading_system.allocation.models import CampaignAllocation
    from trading_system.risk.models import RiskEvaluation

    for model in (RiskEvaluation, CampaignAllocation):
        for field in (
            "rationale",
            "model_name",
            "prompt_version",
            "agent_metadata",
            "raw_response",
        ):
            assert field not in model.model_fields, f"{model.__name__} exposes {field}"


def test_milestone_6_artifacts_still_carry_no_quantity() -> None:
    """The Milestone 6 boundary is preserved: quantity is introduced here, not there."""
    from trading_system.strategies.models import ContractSelectionResult, StrategyDecisionRecord

    for model in (StrategyDecisionRecord, ContractSelectionResult):
        for field in ("quantity", "allocation", "capital_committed", "budget"):
            assert field not in model.model_fields, f"{model.__name__} gained {field}"
