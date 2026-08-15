"""Architectural boundaries around the orphan-cleanup path.

This operation adds the second *authorisation* that can reach a broker, and
these tests make sure it did not add a second *order path*. Both halves matter:

* **The cleanup package holds no broker.** No writable factory, no ``ib_async``,
  no submission API, no socket — directly or transitively. An order exists only
  because ``ExecutionService.submit_cleanup`` made one.
* **The pure half stays pure.** ``targets.py`` and ``gates.py`` are functions of
  captured state, so a stored target list and a stored gate verdict can be
  re-derived and checked.

Import boundaries are checked by *parsing*, not grepping: the words "broker",
"order" and "submit" appear in the docstrings that explain these very rules.
Both direct imports and the whole transitive closure are checked, following
package ``__init__`` files, because importing ``a.b.c`` executes ``a.b.__init__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: What *no* cleanup module may reach, composition root included. Whether to
#: close a pre-existing holding is a safety decision an operator makes, and a
#: model anywhere in this graph would be a model with an order path.
FORBIDDEN_FOR_CLEANUP = (
    "trading_system.agents",
    "anthropic",
)

#: ``service.py`` is the single module allowed to reach the broker library, and
#: only transitively: it calls ``ExecutionService.submit_cleanup``, which owns
#: the one writable factory in the system. Everything else here — the models,
#: the selection, the gates, the store, the renderer — must not, so a stored
#: artifact can be read and a target list re-derived without a gateway anywhere
#: in the picture.
BROKER_LIBRARY_EXEMPT = ("cleanup/service.py",)

#: Modules that must stay pure: no broker, no repository, no clock, no socket.
PURE_MODULES = (
    "cleanup/targets.py",
    "cleanup/gates.py",
    "cleanup/models.py",
)


def _imports_of(path: Path) -> set[str]:
    """Every module a file imports at runtime, parsed rather than grepped.

    ``if TYPE_CHECKING:`` bodies are skipped: an annotation-only import creates
    no runtime edge, and counting one would make the deliberate lazy-import
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


def _package_files(repo_root: Path, package: str) -> list[Path]:
    return sorted((repo_root / "src" / "trading_system" / package).rglob("*.py"))


def _module_path(repo_root: Path, module: str) -> Path | None:
    if not module.startswith("trading_system"):
        return None
    relative = Path(*module.split("."))
    source = repo_root / "src" / relative.with_suffix(".py")
    if source.exists():
        return source
    package = repo_root / "src" / relative / "__init__.py"
    return package if package.exists() else None


def _transitive_imports(repo_root: Path, start: Path) -> set[str]:
    """Every module reachable from ``start``, following package inits.

    A package ``__init__`` is part of the graph because Python executes it:
    ``import trading_system.cleanup.models`` runs ``cleanup/__init__.py``.
    """
    seen: set[str] = set()
    queue = [start]
    visited_paths = {start}
    while queue:
        path = queue.pop()
        for name in _imports_of(path):
            seen.add(name)
            # The parent packages are executed too.
            parts = name.split(".")
            for depth in range(1, len(parts)):
                seen.add(".".join(parts[:depth]))
            for candidate in (name, *(".".join(parts[:d]) for d in range(1, len(parts)))):
                resolved = _module_path(repo_root, candidate)
                if resolved is not None and resolved not in visited_paths:
                    visited_paths.add(resolved)
                    queue.append(resolved)
    return seen


# ---------------------------------------------------------------------------
# One order path, and it is not here
# ---------------------------------------------------------------------------
def test_only_the_execution_service_calls_the_writable_broker_factory(repo_root: Path) -> None:
    """Restated a third time, because the cleanup path adds an authorisation.

    ``tests/execution`` and ``tests/positions`` both assert this. It is
    repeated here because this is the milestone most likely to break it: the
    obvious way to build a cleanup is to give it a broker of its own.
    """
    callers = set()
    for path in (repo_root / "src" / "trading_system").rglob("*.py"):
        if path.name == "factory.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "build_execution_broker" in {
                alias.name for alias in node.names
            }:
                callers.add(str(path.relative_to(repo_root / "src" / "trading_system")))

    assert callers == {"execution/service.py"}, (
        f"only the execution service may obtain a writable broker, but {callers} do"
    )


def test_no_cleanup_module_imports_a_broker_factory(repo_root: Path) -> None:
    for path in _package_files(repo_root, "cleanup"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = {alias.name for alias in node.names}
                assert "build_execution_broker" not in names, path
                assert "build_broker" not in names, path


def test_no_cleanup_module_names_a_submission_api(repo_root: Path) -> None:
    """Matched as *call syntax*, not as a substring.

    A grep for ``place_order`` would flag ``can_submit_orders`` and every
    docstring on this page; matching ``.place_order(`` matches an actual call.
    """
    for path in _package_files(repo_root, "cleanup"):
        source = path.read_text(encoding="utf-8")
        assert ".place_order(" not in source, path
        assert "._submit_order(" not in source, path
        assert ".cancel_order(" not in source, path


def test_the_cleanup_service_takes_no_broker_parameter() -> None:
    """It orchestrates services that hold their own; it never holds one."""
    import inspect

    from trading_system.cleanup.service import CleanupService

    parameters = set(inspect.signature(CleanupService.__init__).parameters)
    assert "broker" not in parameters
    assert "broker_factory" not in parameters

    run = set(inspect.signature(CleanupService.run).parameters)
    assert "broker" not in run


def test_no_cleanup_module_instantiates_a_second_broker_client(repo_root: Path) -> None:
    for path in _package_files(repo_root, "cleanup"):
        source = path.read_text(encoding="utf-8")
        assert "IBKRBroker(" not in source, path
        assert "SimulatedBroker(" not in source, path
        assert "import ib_async" not in source, path


@pytest.mark.parametrize("forbidden", FORBIDDEN_FOR_CLEANUP)
def test_cleanup_never_reaches_a_model(repo_root: Path, forbidden: str) -> None:
    for path in _package_files(repo_root, "cleanup"):
        reachable = _transitive_imports(repo_root, path)
        assert forbidden not in reachable, f"{path} transitively imports {forbidden}"


def test_only_the_cleanup_service_reaches_the_broker_library(repo_root: Path) -> None:
    """And it reaches it only through the one module that may submit."""
    reaching = {
        str(path.relative_to(repo_root / "src" / "trading_system"))
        for path in _package_files(repo_root, "cleanup")
        if "ib_async" in _transitive_imports(repo_root, path)
    }
    assert reaching <= set(BROKER_LIBRARY_EXEMPT), (
        f"{sorted(reaching - set(BROKER_LIBRARY_EXEMPT))} reach the broker library; only "
        f"{BROKER_LIBRARY_EXEMPT} may, and only through ExecutionService"
    )


def test_no_cleanup_module_names_a_model_client(repo_root: Path) -> None:
    for path in _package_files(repo_root, "cleanup"):
        source = path.read_text(encoding="utf-8")
        assert "LLMClient" not in source, path
        assert "prompt" not in source.lower().replace("prompted", ""), path


# ---------------------------------------------------------------------------
# The pure half stays pure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", PURE_MODULES)
def test_the_pure_modules_reach_no_broker(repo_root: Path, module: str) -> None:
    path = repo_root / "src" / "trading_system" / module
    reachable = _transitive_imports(repo_root, path)
    assert not any(name.startswith("trading_system.broker") for name in reachable), module


@pytest.mark.parametrize("module", PURE_MODULES)
@pytest.mark.parametrize("forbidden", ["socket", "urllib", "http", "requests"])
def test_the_pure_modules_open_no_connection(repo_root: Path, module: str, forbidden: str) -> None:
    path = repo_root / "src" / "trading_system" / module
    assert forbidden not in _transitive_imports(repo_root, path), module


@pytest.mark.parametrize("module", ("cleanup/targets.py", "cleanup/gates.py"))
def test_the_pure_modules_read_no_clock(repo_root: Path, module: str) -> None:
    """The instant is passed in, so a stored verdict can be re-derived."""
    path = repo_root / "src" / "trading_system" / module
    source = path.read_text(encoding="utf-8")
    assert "datetime.now" not in source, module
    assert "SystemClock" not in source, module
    assert ".now()" not in source, module


@pytest.mark.parametrize("module", ("cleanup/targets.py", "cleanup/gates.py"))
def test_the_pure_modules_use_no_random_source(repo_root: Path, module: str) -> None:
    path = repo_root / "src" / "trading_system" / module
    assert "random" not in _transitive_imports(repo_root, path), module


def test_the_pure_modules_touch_no_repository(repo_root: Path) -> None:
    for module in ("cleanup/targets.py", "cleanup/gates.py"):
        path = repo_root / "src" / "trading_system" / module
        source = path.read_text(encoding="utf-8")
        assert "Repository" not in source, module
        assert "open(" not in source, module


# ---------------------------------------------------------------------------
# Reconciliation is unchanged
# ---------------------------------------------------------------------------
def test_reconciliation_still_names_no_submission_api(repo_root: Path) -> None:
    """The comparison stays read-only; the cleanup lives in its own package."""
    for path in _package_files(repo_root, "reconciliation"):
        source = path.read_text(encoding="utf-8")
        assert ".place_order(" not in source, path
        assert "build_execution_broker" not in source, path


def test_reconciliation_does_not_import_the_cleanup_package(repo_root: Path) -> None:
    """The dependency points one way: cleanup reads reconciliation, never back."""
    for path in _package_files(repo_root, "reconciliation"):
        reachable = _transitive_imports(repo_root, path)
        assert "trading_system.cleanup" not in reachable, path


@pytest.mark.parametrize("package", ("universe", "research", "strategies", "risk", "allocation"))
def test_upstream_packages_never_reach_the_cleanup_path(repo_root: Path, package: str) -> None:
    for path in _package_files(repo_root, package):
        reachable = _transitive_imports(repo_root, path)
        assert not any(name.startswith("trading_system.cleanup") for name in reachable), path


# ---------------------------------------------------------------------------
# The deferred service
# ---------------------------------------------------------------------------
def test_the_cleanup_package_defers_its_service() -> None:
    """An eager re-export would put a writable-broker-capable service in the
    import graph of anything that merely names a cleanup type — including the
    execution service, which type-checks against ``CleanupTarget``."""
    import trading_system.cleanup as package

    tree = ast.parse(Path(package.__file__).read_text(encoding="utf-8"))
    eager = _imports_of(Path(package.__file__))
    assert "trading_system.cleanup.service" not in eager
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "__getattr__" for node in ast.walk(tree)
    )


def test_the_deferred_members_still_resolve() -> None:
    from trading_system.cleanup import CleanupService, evaluate_run_gates, select_targets

    assert CleanupService.__name__ == "CleanupService"
    assert callable(select_targets)
    assert callable(evaluate_run_gates)


def test_naming_a_cleanup_type_does_not_import_a_broker(repo_root: Path) -> None:
    path = repo_root / "src" / "trading_system" / "cleanup" / "models.py"
    reachable = _transitive_imports(repo_root, path)
    assert "trading_system.execution.service" not in reachable
    assert not any(name.startswith("trading_system.broker") for name in reachable)
