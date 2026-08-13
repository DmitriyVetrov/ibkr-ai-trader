"""Architectural boundaries around the Milestone 9 packages (brief section 81).

The central claim: **positions, reservations and reconciliation cannot submit
an order.** These tests make it structural rather than stylistic, by parsing
the import graph rather than grepping — the words "broker", "order" and
"execution" appear throughout the docstrings that explain these very
boundaries.

Four boundaries:

* no Milestone 9 package reaches the writable broker constructor, directly or
  transitively, and none of them names a submission API;
* ``reservations`` holds no broker at all: capital moves on the evidence the
  execution ledger already recorded;
* nothing here reaches a model — reconciliation is arithmetic over two ledgers;
* ``ib_async`` stays inside the adapter, and the position repository never
  touches it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: The three packages this milestone adds.
M9_PACKAGES = ("positions", "reservations", "reconciliation")

#: Names that would mean an order could leave the process.
SUBMISSION_APIS = (
    "place_order",
    "placeOrder",
    "_submit_order",
    "build_execution_broker",
    "cancel_order",
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


def _package_files(repo_root: Path, package: str) -> list[Path]:
    return sorted((repo_root / "src" / "trading_system" / package).glob("*.py"))


def _source(repo_root: Path, relative: str) -> Path:
    return repo_root / "src" / "trading_system" / relative


# ---------------------------------------------------------------------------
# Nothing here can submit an order
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package", M9_PACKAGES)
def test_no_milestone_9_module_names_a_submission_api(repo_root: Path, package: str) -> None:
    """Not even by string. There is no way to reach one from here."""
    for path in _package_files(repo_root, package):
        source = path.read_text(encoding="utf-8")
        for forbidden in SUBMISSION_APIS:
            assert forbidden not in source, f"{package}/{path.name} references {forbidden}"


@pytest.mark.parametrize("package", M9_PACKAGES)
def test_no_milestone_9_module_reaches_the_writable_broker_factory(
    repo_root: Path, package: str
) -> None:
    for path in _package_files(repo_root, package):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "build_execution_broker" not in {alias.name for alias in node.names}


def test_only_the_execution_service_calls_the_writable_broker_factory(repo_root: Path) -> None:
    """Restated here because Milestone 9 adds three packages that hold brokers."""
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
    assert callers == {"execution/service.py"}


def test_the_position_service_depends_only_on_the_broker_interface(repo_root: Path) -> None:
    broker_imports = {
        name
        for path in _package_files(repo_root, "positions")
        for name in _imports_of(path)
        if name.startswith("trading_system.broker")
    }
    assert broker_imports <= {"trading_system.broker.base", "trading_system.broker.factory"}


# ---------------------------------------------------------------------------
# Reservations hold no broker at all
# ---------------------------------------------------------------------------
def test_reservations_reach_no_broker(repo_root: Path) -> None:
    """Capital moves on evidence already recorded, never on a fresh question."""
    for path in _package_files(repo_root, "reservations"):
        reachable = _transitive_imports(repo_root, path)
        offenders = sorted(name for name in reachable if name.startswith("trading_system.broker"))
        assert offenders == [], f"reservations/{path.name} reaches a broker: {offenders}"


def test_the_reservation_service_takes_no_broker_parameter() -> None:
    import inspect

    from trading_system.reservations.service import ReservationService

    parameters = set(inspect.signature(ReservationService.__init__).parameters)
    assert "broker" not in parameters
    assert "broker_factory" not in parameters


# ---------------------------------------------------------------------------
# Nothing here consults a model
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package", M9_PACKAGES)
@pytest.mark.parametrize("forbidden", ("trading_system.agents", "anthropic"))
def test_no_milestone_9_module_reaches_a_model(
    repo_root: Path, package: str, forbidden: str
) -> None:
    for path in _package_files(repo_root, package):
        reachable = _transitive_imports(repo_root, path)
        offenders = sorted(name for name in reachable if name.startswith(forbidden))
        assert offenders == [], f"{package}/{path.name} reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("package", M9_PACKAGES)
def test_no_milestone_9_module_names_a_model_client(repo_root: Path, package: str) -> None:
    for path in _package_files(repo_root, package):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("LLMClient", "AnthropicLLMClient", "StructuredRequest", "load_prompt"):
            assert forbidden not in source, f"{package}/{path.name} references {forbidden}"


@pytest.mark.parametrize(
    "service",
    (
        "positions.service.PositionService",
        "reservations.service.ReservationService",
        "reconciliation.service.ReconciliationService",
    ),
)
def test_no_milestone_9_service_takes_a_model_client(service: str) -> None:
    import importlib
    import inspect

    module_name, class_name = service.rsplit(".", 1)
    module = importlib.import_module(f"trading_system.{module_name}")
    parameters = set(inspect.signature(getattr(module, class_name).__init__).parameters)
    assert "llm_client" not in parameters
    assert "agent" not in parameters


# ---------------------------------------------------------------------------
# ib_async stays inside the adapter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package", M9_PACKAGES)
def test_no_milestone_9_module_instantiates_ib_async(repo_root: Path, package: str) -> None:
    for path in _package_files(repo_root, package):
        direct = _imports_of(path)
        assert "ib_async" not in direct, f"{package}/{path.name} imports ib_async"
        assert not any(name.startswith("trading_system.broker.ibkr") for name in direct), (
            f"{package}/{path.name} reaches into the IBKR adapter instead of the interface"
        )


def test_the_position_repository_never_touches_the_broker_at_all(repo_root: Path) -> None:
    """Storage is storage. The service holds the connection; the store does not."""
    reachable = _transitive_imports(repo_root, _source(repo_root, "positions/store.py"))
    offenders = sorted(name for name in reachable if name.startswith("trading_system.broker"))
    assert offenders == []


# ---------------------------------------------------------------------------
# The pure layers stay pure
# ---------------------------------------------------------------------------
PURE_MODULES = (
    "positions/models.py",
    "positions/snapshot.py",
    "positions/fills.py",
    "positions/expected.py",
    "reservations/models.py",
    "reservations/lifecycle.py",
    "reconciliation/models.py",
    "reconciliation/engine.py",
    "reconciliation/positions.py",
    "reconciliation/orders.py",
    "reconciliation/fills.py",
    "reconciliation/unknown.py",
    "reconciliation/reservations.py",
)


@pytest.mark.parametrize("module", PURE_MODULES)
def test_the_pure_modules_reach_no_broker(repo_root: Path, module: str) -> None:
    reachable = _transitive_imports(repo_root, _source(repo_root, module))
    offenders = sorted(name for name in reachable if name.startswith("trading_system.broker"))
    assert offenders == [], f"{module} reaches a broker: {offenders}"


@pytest.mark.parametrize("module", PURE_MODULES)
@pytest.mark.parametrize("forbidden", ("socket", "urllib", "http", "subprocess", "requests"))
def test_the_pure_modules_open_no_connection(repo_root: Path, module: str, forbidden: str) -> None:
    reachable = _transitive_imports(repo_root, _source(repo_root, module))
    offenders = sorted(
        name for name in reachable if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"{module} reaches {forbidden}: {offenders}"


@pytest.mark.parametrize(
    "module",
    (
        "reconciliation/engine.py",
        "reconciliation/positions.py",
        "reconciliation/orders.py",
        "reconciliation/unknown.py",
        "reservations/lifecycle.py",
    ),
)
def test_the_deterministic_modules_read_no_clock(repo_root: Path, module: str) -> None:
    """The instant is injected, so a stored comparison is reproducible."""
    source = _source(repo_root, module).read_text(encoding="utf-8")
    for forbidden in ("datetime.now(", "date.today(", "utc_now(", "time.time("):
        assert forbidden not in source, f"{module} reads a clock: {forbidden}"


@pytest.mark.parametrize("module", PURE_MODULES)
def test_the_pure_modules_use_no_random_source(repo_root: Path, module: str) -> None:
    reachable = _transitive_imports(repo_root, _source(repo_root, module))
    assert "random" not in reachable
    assert "secrets" not in reachable


# ---------------------------------------------------------------------------
# The package __init__ does not drag a broker in
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package", M9_PACKAGES)
def test_naming_a_milestone_9_type_does_not_import_a_broker(repo_root: Path, package: str) -> None:
    """A package ``__init__`` is part of every importer's graph."""
    reachable = _transitive_imports(repo_root, _source(repo_root, f"{package}/__init__.py"))
    offenders = sorted(name for name in reachable if name.startswith("trading_system.broker"))
    assert offenders == [], f"{package}/__init__.py reaches a broker: {offenders}"


@pytest.mark.parametrize(
    ("package", "member", "module"),
    (
        ("positions", "PositionService", "service"),
        ("reservations", "ReservationService", "service"),
        ("reconciliation", "ReconciliationService", "service"),
    ),
)
def test_each_package_defers_its_service(package: str, member: str, module: str) -> None:
    """The lazy accessor is load-bearing; do not tidy it away."""
    import importlib

    imported = importlib.import_module(f"trading_system.{package}")
    assert imported._LAZY[member] == module


def test_the_deferred_members_still_resolve() -> None:
    from trading_system.positions import PositionService
    from trading_system.reconciliation import ReconciliationService
    from trading_system.reservations import ReservationService

    assert PositionService.__name__ == "PositionService"
    assert ReservationService.__name__ == "ReservationService"
    assert ReconciliationService.__name__ == "ReconciliationService"


# ---------------------------------------------------------------------------
# Dependency direction
# ---------------------------------------------------------------------------
def test_upstream_packages_never_import_milestone_9(repo_root: Path) -> None:
    """Research, strategy, risk and allocation know nothing about positions.

    The dependency runs one way: reconciliation reads their ledgers, never the
    reverse. A risk engine that imported the position ledger could no longer be
    a pure function of its arguments.
    """
    upstream = ("universe", "research", "strategies", "risk", "allocation", "execution")
    for package in upstream:
        for path in _package_files(repo_root, package):
            offenders = sorted(
                name
                for name in _imports_of(path)
                if any(name.startswith(f"trading_system.{m9}") for m9 in M9_PACKAGES)
            )
            assert offenders == [], f"{package}/{path.name} imports Milestone 9: {offenders}"
