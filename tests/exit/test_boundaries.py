"""Architectural boundaries around the exit subsystem.

These are the tests that make the milestone's central claims structural rather
than stylistic. The documentation *says* exit management consults no model and
holds no broker; these assert that it has no other option available — no client
to call, no writable constructor to reach, and no import through which either
could arrive.

Import boundaries are checked by parsing the source, not by grepping: the words
"broker", "model" and "order" appear throughout the docstrings that explain the
boundary, and those must not count as violations of it. Both direct imports and
the whole transitive closure are checked, following package ``__init__`` files,
because importing ``a.b.c`` executes ``a.b.__init__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Modules the exit subsystem must never reach.
#:
#: ``trading_system.agents`` is the one this milestone turns on: whether to
#: sell an option is a safety decision, and a deterministic engine that can be
#: replayed is worth more than a persuasive one that cannot.
#: ``trading_system.broker`` is the other: an exit order exists only because
#: Milestone 8's execution service made one.
FORBIDDEN_MODULES = (
    "trading_system.agents",
    "trading_system.broker",
    "trading_system.data.providers",
    "trading_system.data.collectors",
    "trading_system.universe",
    "anthropic",
    "ib_async",
)

FORBIDDEN_STDLIB = ("socket", "urllib", "http", "subprocess", "requests", "httpx")

#: Every module in the package except the composition root, which is *allowed*
#: to reach Milestone 8's execution service — that is what it is for — and does
#: so lazily, inside methods, so the models themselves stay clean.
PURE_MODULES = (
    "models.py",
    "lifecycle.py",
    "expiration.py",
    "trailing.py",
    "thesis.py",
    "policies.py",
    "engine.py",
    "validation.py",
    "valuation.py",
    "store.py",
    "report.py",
    "__init__.py",
)


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


def _exit_package(repo_root: Path) -> Path:
    return repo_root / "src" / "trading_system" / "exit"


def _exit_module(repo_root: Path, name: str) -> Path:
    return _exit_package(repo_root) / name


# ---------------------------------------------------------------------------
# The package exists and is shaped as documented
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", PURE_MODULES)
def test_every_documented_module_exists(repo_root: Path, name: str) -> None:
    assert _exit_module(repo_root, name).is_file()


# ---------------------------------------------------------------------------
# No model, anywhere
# ---------------------------------------------------------------------------
def test_no_exit_module_imports_an_agent_or_an_llm_client(repo_root: Path) -> None:
    """The whole package, service included. Milestone 10 introduces no agent."""
    package = _exit_package(repo_root)
    offenders: dict[str, list[str]] = {}
    for source in sorted(package.glob("*.py")):
        reachable = _transitive_imports(repo_root, source)
        found = sorted(
            module
            for module in reachable
            if module.startswith(("trading_system.agents", "anthropic"))
        )
        if found:
            offenders[source.name] = found

    assert offenders == {}, f"exit modules reach an agent or an LLM client: {offenders}"


def test_no_exit_module_names_a_model_a_prompt_or_a_client(repo_root: Path) -> None:
    """A parameter is how a model would arrive; there is none.

    Checked on the source's identifiers rather than on prose, so the docstrings
    explaining why there is no model do not count as evidence that there is.
    """
    package = _exit_package(repo_root)
    offenders: dict[str, list[str]] = {}
    for source in sorted(package.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {
            node.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.arg)
            and node.arg in {"llm", "client", "agent", "prompt", "model_id"}
        }
        if names:
            offenders[source.name] = sorted(names)

    assert offenders == {}, f"exit modules accept a model-shaped parameter: {offenders}"


# ---------------------------------------------------------------------------
# No broker, anywhere in the pure half
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", PURE_MODULES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_MODULES)
def test_the_pure_modules_cannot_reach_a_broker_transitively(
    repo_root: Path, name: str, forbidden: str
) -> None:
    reachable = _transitive_imports(repo_root, _exit_module(repo_root, name))

    offenders = sorted(module for module in reachable if module.startswith(forbidden))
    assert offenders == [], f"exit/{name} transitively reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("name", PURE_MODULES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_pure_modules_cannot_open_a_connection_of_their_own(
    repo_root: Path, name: str, forbidden: str
) -> None:
    reachable = _transitive_imports(repo_root, _exit_module(repo_root, name))

    offenders = sorted(
        module for module in reachable if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"exit/{name} transitively reaches {forbidden}: {offenders}"


def test_the_service_does_not_import_a_broker_directly(repo_root: Path) -> None:
    """The composition root reaches Milestone 8's *service*, never a broker.

    Every path to an order goes through ``ExecutionService.submit_exit``, which
    owns both of the switches and the idempotency check.
    """
    direct = _imports_of(_exit_module(repo_root, "service.py"))

    offenders = sorted(
        module for module in direct if module.startswith(("trading_system.broker", "ib_async"))
    )
    assert offenders == []


def test_the_service_constructs_no_writable_broker(repo_root: Path) -> None:
    """``build_execution_broker`` is the one writable constructor in the system.

    Naming it anywhere in this package would be a second path to an order.
    """
    package = _exit_package(repo_root)
    offenders = [
        source.name
        for source in sorted(package.glob("*.py"))
        if "build_execution_broker" in source.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_the_package_init_defers_everything_that_touches_a_repository(
    repo_root: Path,
) -> None:
    """Importing ``exit.models`` executes ``exit/__init__``, so anything eager
    there lands in the import graph of every module that merely names an exit
    type — including Milestone 8's execution service."""
    reachable = _transitive_imports(repo_root, _exit_module(repo_root, "__init__.py"))

    assert not any(module.startswith("trading_system.broker") for module in reachable)
    assert not any(module.startswith("trading_system.execution") for module in reachable)


# ---------------------------------------------------------------------------
# No sizing, no allocation, no money decisions
# ---------------------------------------------------------------------------
def test_no_exit_module_imports_the_allocation_or_risk_engines(repo_root: Path) -> None:
    """Milestone 10 decides no money. It reads Milestone 7's *declared* basis
    from the strategy structure and computes no limit of its own."""
    package = _exit_package(repo_root)
    offenders: dict[str, list[str]] = {}
    for source in sorted(package.glob("*.py")):
        direct = _imports_of(source)
        found = sorted(
            module
            for module in direct
            if module.startswith(("trading_system.allocation", "trading_system.risk"))
        )
        if found:
            offenders[source.name] = found

    assert offenders == {}, f"exit modules reach a sizing engine: {offenders}"


def test_the_exit_package_defines_no_independent_leg_path(repo_root: Path) -> None:
    """A structure exits whole. There is no code that closes one leg."""
    package = _exit_package(repo_root)
    for source in sorted(package.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            assert "allow_independent_leg_exit=True" not in stripped.replace(" ", "")


# ---------------------------------------------------------------------------
# The M8 seam
# ---------------------------------------------------------------------------
def test_submit_exit_is_called_from_the_composition_root_and_nowhere_else(
    repo_root: Path,
) -> None:
    """Called once, in the composition root.

    Parsed rather than grepped: the name appears in the docstrings that explain
    the seam, and a mention is not a call.
    """
    package = _exit_package(repo_root)
    callers: list[str] = []
    for source in sorted(package.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        called = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit_exit"
            for node in ast.walk(tree)
        )
        if called:
            callers.append(source.name)

    assert callers == ["service.py"]


def test_the_execution_service_owns_the_order_building(repo_root: Path) -> None:
    """There is no second order builder: the exit intent is built in
    Milestone 8's own module, beside the entry one."""
    package = _exit_package(repo_root)
    offenders = [
        source.name
        for source in sorted(package.glob("*.py"))
        if "OrderIntent(" in source.read_text(encoding="utf-8")
    ]

    assert offenders == []
