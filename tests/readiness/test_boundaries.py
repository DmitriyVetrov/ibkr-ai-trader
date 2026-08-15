"""Architectural boundaries the readiness core must not cross (brief section 37).

The claims, in the order they matter:

* the **evaluator** reaches no broker, no LLM, no Docker client, no socket and
  no order path — it is a pure function and the import graph proves it;
* the **sign-off** reaches nothing that could enable trading;
* only the **paper gate** can reach a writable broker, and nothing else imports
  it — including the service that runs every other check.

Every test walks the *transitive* import graph, through ``__init__`` files,
because Python executes those. ``research/__init__.py`` and
``data/__init__.py`` both record why: an eager re-export pulls a whole
subsystem into the graph of anything that merely names a type, invisibly to a
test that reads only direct imports.

``if TYPE_CHECKING:`` bodies are skipped — they never execute.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"
PACKAGE = "trading_system"

#: Modules that must never appear in the readiness core's transitive graph.
FORBIDDEN_MODULES = {
    "socket",
    "urllib",
    "http",
    "requests",
    "httpx",
    "docker",
    "anthropic",
    "ib_async",
    "subprocess",
}

#: Trading-system packages the evaluator must not be able to reach.
FORBIDDEN_PACKAGES = {
    "trading_system.broker",
    "trading_system.execution",
    "trading_system.agents",
    "trading_system.readiness.collectors",
    "trading_system.readiness.observability_probe",
    "trading_system.readiness.paper_gate",
    "trading_system.readiness.telemetry_emission",
    "trading_system.readiness.service",
}


def _module_path(module: str) -> Path | None:
    relative = Path(*module.split("."))
    for candidate in (SRC / relative.with_suffix(".py"), SRC / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _direct_imports(path: Path) -> set[str]:
    """Every module this file imports, skipping ``if TYPE_CHECKING:`` bodies."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found: set[str] = set()

        def visit_If(self, node: ast.If) -> None:
            test = node.test
            is_type_checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if is_type_checking:
                for child in node.orelse:
                    self.visit(child)
                return
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self.found.add(alias.name)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module and node.level == 0:
                self.found.add(node.module)
                for alias in node.names:
                    self.found.add(f"{node.module}.{alias.name}")

    visitor = Visitor()
    visitor.visit(tree)
    return visitor.found


def closure(entry: str) -> set[str]:
    """Every module reachable from ``entry``, including through packages.

    Package ``__init__`` files are visited for every module walked through,
    because importing ``a.b.c`` executes ``a/__init__.py`` and
    ``a/b/__init__.py``.
    """
    seen: set[str] = set()
    queue = [entry]
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)

        parts = module.split(".")
        for depth in range(1, len(parts)):
            parent = ".".join(parts[:depth])
            if parent.startswith(PACKAGE) and parent not in seen:
                queue.append(parent)

        path = _module_path(module)
        if path is None:
            continue
        for imported in _direct_imports(path):
            if imported not in seen:
                queue.append(imported)
    return seen


def _violations(entry: str, forbidden: set[str]) -> set[str]:
    reached = closure(entry)
    found = set()
    for module in reached:
        root = module.split(".")[0]
        if root in forbidden:
            found.add(module)
        for package in forbidden:
            if module == package or module.startswith(package + "."):
                found.add(module)
    return found


# ---------------------------------------------------------------------------
# The evaluator is pure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "module",
    [
        "trading_system.readiness.evaluator",
        "trading_system.readiness.criteria",
        "trading_system.readiness.evidence",
        "trading_system.readiness.models",
        "trading_system.readiness.policy",
    ],
)
def test_the_readiness_core_reaches_no_infrastructure(module: str) -> None:
    """No socket, no HTTP, no Docker, no subprocess, no broker library.

    ``subprocess`` is in the forbidden set for the same reason ``socket`` is:
    a criterion predicate that could shell out would stop being a pure
    function of its evidence, and the stored assessment would stop being
    reproducible.
    """
    violations = _violations(module, FORBIDDEN_MODULES)
    assert not violations, f"{module} transitively imports {sorted(violations)}"


@pytest.mark.parametrize(
    "module",
    [
        "trading_system.readiness.evaluator",
        "trading_system.readiness.criteria",
        "trading_system.readiness.models",
        "trading_system.readiness.policy",
    ],
)
def test_the_readiness_core_reaches_no_broker_or_order_path(module: str) -> None:
    violations = _violations(module, FORBIDDEN_PACKAGES)
    assert not violations, f"{module} transitively imports {sorted(violations)}"


def test_the_evaluator_cannot_reach_the_paper_gate() -> None:
    """The one module that can send an order is not in the evaluator's graph."""
    reached = closure("trading_system.readiness.evaluator")
    assert "trading_system.readiness.paper_gate" not in reached


def test_the_readiness_service_does_not_import_the_paper_gate() -> None:
    """Brief section 2: no path from "readiness is fine" to an order.

    The service is what ``readiness check`` runs. Importing the paper gate
    would put a writable broker constructor one attribute lookup away from
    every readiness run.
    """
    reached = closure("trading_system.readiness.service")
    assert "trading_system.readiness.paper_gate" not in reached


def test_the_readiness_package_init_stays_light() -> None:
    """``readiness/__init__.py`` defers anything that touches infrastructure.

    Same reason ``execution/__init__.py`` defers its service: an eager
    re-export would put a broker in the import graph of anything that merely
    names a readiness type.
    """
    violations = _violations("trading_system.readiness", FORBIDDEN_MODULES)
    assert not violations, f"readiness/__init__.py transitively imports {sorted(violations)}"


# ---------------------------------------------------------------------------
# Sign-off enables nothing
# ---------------------------------------------------------------------------
def test_the_signoff_module_reaches_no_broker_and_no_execution() -> None:
    violations = _violations(
        "trading_system.readiness.signoff",
        {"trading_system.broker", "trading_system.execution", "ib_async"},
    )
    assert not violations, f"signoff transitively imports {sorted(violations)}"


def test_the_signoff_module_never_writes_the_environment() -> None:
    """Signing must not be able to set a live guard.

    Parsed rather than grepped: a comment mentioning ``os.environ`` is not a
    mutation, and a test that could not tell the difference would eventually be
    loosened by somebody who wrote the comment.
    """
    path = SRC / "trading_system" / "readiness" / "signoff.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            assert name not in {"setenv", "putenv"}, "signoff sets an environment variable"
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            value = node.value
            rendered = ast.unparse(value)
            assert "environ" not in rendered, "signoff assigns into os.environ"


def test_the_signoff_module_writes_no_configuration_file() -> None:
    """A sign-off records a decision; it does not edit ``config/``."""
    source = (SRC / "trading_system" / "readiness" / "signoff.py").read_text(encoding="utf-8")
    for forbidden in ("write_text(", "open(", "yaml.dump", "yaml.safe_dump"):
        assert forbidden not in source, f"signoff calls {forbidden}"


# ---------------------------------------------------------------------------
# Nothing in readiness can submit an order
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "module",
    [
        "trading_system.readiness.evaluator",
        "trading_system.readiness.criteria",
        "trading_system.readiness.service",
        "trading_system.readiness.collectors",
        "trading_system.readiness.store",
        "trading_system.readiness.report",
        "trading_system.readiness.signoff",
    ],
)
def test_no_readiness_module_but_the_paper_gate_calls_place_order(module: str) -> None:
    """Matched on *call syntax*, not on a substring.

    ``tests/operations/`` records why: a boundary test that greps for
    ``_submit_order`` flags ``can_submit_orders``, a field whose entire purpose
    is to say which job could reach an order path.
    """
    path = _module_path(module)
    assert path is not None
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            attribute = getattr(node.func, "attr", None)
            assert attribute not in {
                "place_order",
                "_submit_order",
                "submit",
                "cancel_order",
            }, f"{module} calls {attribute}()"


def _imports_name(path: Path, name: str) -> bool:
    """Whether this module actually imports ``name``.

    Parsed, not grepped. Both of these tests failed on their first run against
    a *docstring* that named ``build_execution_broker`` while explaining that
    the module does not import it — the same mistake the operations suite
    records about greping for ``_submit_order`` and flagging
    ``can_submit_orders``. A prose mention is not a dependency.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == name for alias in node.names):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name.endswith(f".{name}") for alias in node.names
        ):
            return True
    return False


def test_no_readiness_module_imports_a_writable_broker_constructor() -> None:
    """Not one — not even the paper gate.

    Milestone 8's invariant is that ``build_execution_broker`` has exactly one
    caller, ``execution/service.py``, and two boundary suites assert it. The
    first shape of ``paper_gate.py`` submitted its own controlled order and so
    became a second caller; brief section 2 forbids weakening an existing gate,
    so the gate now checks authorisations and points at the audited path
    (``tests/integration/test_paper_execution.py``) instead of opening a
    connection of its own.
    """
    readiness = SRC / "trading_system" / "readiness"
    importers = sorted(
        path.name
        for path in readiness.glob("*.py")
        if _imports_name(path, "build_execution_broker")
    )
    assert importers == [], f"writable broker imported by {importers}"


def test_the_paper_gate_opens_no_connection_at_all() -> None:
    """It authorises; it does not act."""
    gate = SRC / "trading_system" / "readiness" / "paper_gate.py"
    assert not _imports_name(gate, "build_execution_broker")
    assert not _imports_name(gate, "build_broker")


def test_the_collectors_never_build_a_writable_broker() -> None:
    """Collectors read. ``build_broker`` is read-only whatever the settings say."""
    collectors = SRC / "trading_system" / "readiness" / "collectors.py"
    assert not _imports_name(collectors, "build_execution_broker")
    assert _imports_name(collectors, "build_broker")
