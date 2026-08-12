"""Architectural boundaries around order submission (brief sections 42.11, 64, 65).

Milestone 8's central claim is that *arbitrary code cannot submit an order*.
These tests make it structural rather than stylistic, and there are two halves
to it:

* **Nothing upstream can reach the order path.** Research, strategy, contract
  selection, risk and allocation may not import the broker or the execution
  package, directly or transitively. They are not merely well-behaved — they
  hold no object with a ``place_order`` on it.
* **Execution needs no model.** No agent, no LLM client, no prompt. Execution
  translates an already-approved deterministic artifact into an order; there is
  nothing in that for a model to decide, and an execution engine that could
  consult one would be an order path an LLM could influence.

Import boundaries are checked by parsing the source, not by grepping: the words
"broker", "order" and "agent" appear in the docstrings that explain these very
boundaries. Both direct imports and the whole transitive closure are checked,
following package ``__init__`` files, because importing ``a.b.c`` executes
``a.b.__init__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Packages that must never reach an order path.
UPSTREAM_PACKAGES = ("universe", "research", "strategies", "risk", "allocation")

#: What execution itself must never reach.
FORBIDDEN_FOR_EXECUTION = (
    "trading_system.agents",
    "anthropic",
)

#: Modules that must stay pure: no broker, no repository, no clock.
PURE_MODULES = (
    "execution/order_builder.py",
    "execution/validation.py",
    "execution/fill_tracker.py",
    "execution/purchase_card.py",
    "execution/state_machine.py",
    "execution/models.py",
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


def _source(repo_root: Path, relative: str) -> Path:
    return repo_root / "src" / "trading_system" / relative


def _package_files(repo_root: Path, package: str) -> list[Path]:
    return sorted((repo_root / "src" / "trading_system" / package).glob("*.py"))


# ---------------------------------------------------------------------------
# Upstream cannot submit
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package", UPSTREAM_PACKAGES)
@pytest.mark.parametrize("forbidden", ("trading_system.broker", "trading_system.execution"))
def test_no_upstream_package_imports_the_order_path(
    repo_root: Path, package: str, forbidden: str
) -> None:
    offenders = [
        str(path.relative_to(repo_root))
        for path in _package_files(repo_root, package)
        if any(name.startswith(forbidden) for name in _imports_of(path))
    ]
    assert offenders == [], f"{package}/ imports {forbidden}: {offenders}"


@pytest.mark.parametrize("package", UPSTREAM_PACKAGES)
def test_no_upstream_package_names_a_submission_api(repo_root: Path, package: str) -> None:
    """Not even by string. There is no way to reach one from up there."""
    for path in _package_files(repo_root, package):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "place_order",
            "placeOrder",
            "_submit_order",
            "submit_authorized_order",
            "build_execution_broker",
        ):
            assert forbidden not in source, f"{path.name} references {forbidden}"


@pytest.mark.parametrize(
    "module",
    (
        "research/service.py",
        "strategies/service.py",
        "risk/engine.py",
        "allocation/service.py",
        "allocation/budget_allocator.py",
    ),
)
def test_no_upstream_service_reaches_execution_transitively(repo_root: Path, module: str) -> None:
    reachable = _transitive_imports(repo_root, _source(repo_root, module))

    offenders = sorted(
        name
        for name in reachable
        if name.startswith("trading_system.execution") or name.startswith("trading_system.broker")
    )
    assert offenders == [], f"{module} transitively reaches an order path: {offenders}"


# ---------------------------------------------------------------------------
# Only a deliberately-built broker can submit
# ---------------------------------------------------------------------------
def test_the_ordinary_broker_factory_always_returns_a_read_only_broker() -> None:
    """Every diagnostic, the data layer and every upstream stage get this one."""
    import inspect

    from trading_system.broker import factory

    source = inspect.getsource(factory.build_broker)
    assert "read_only=True" in source
    assert "read_only=False" not in source


def test_only_the_execution_service_calls_the_writable_broker_factory(repo_root: Path) -> None:
    """Parsed, not grepped: the name appears in docstrings that explain the rule."""
    callers = set()
    for path in (repo_root / "src" / "trading_system").rglob("*.py"):
        if path.name == "factory.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported = (
                {alias.name for alias in node.names} if isinstance(node, ast.ImportFrom) else set()
            )
            if "build_execution_broker" in imported:
                callers.add(str(path.relative_to(repo_root / "src" / "trading_system")))

    assert callers == {"execution/service.py"}, (
        f"only the execution service may obtain a writable broker, but {callers} do"
    )


def test_a_writable_ibkr_connection_requires_paper() -> None:
    from trading_system.broker.base import BrokerConfigurationError
    from trading_system.broker.ibkr import IBKRBroker
    from trading_system.domain.enums import TradingMode

    with pytest.raises(BrokerConfigurationError, match="TRADING_MODE=PAPER"):
        IBKRBroker(
            host="127.0.0.1",
            port=4002,
            client_id=1,
            trading_mode=TradingMode.DRY_RUN,
            read_only=False,
        )


def test_live_is_refused_by_the_adapter_outright() -> None:
    from trading_system.broker.base import BrokerConfigurationError
    from trading_system.broker.ibkr import IBKRBroker
    from trading_system.domain.enums import TradingMode

    with pytest.raises(BrokerConfigurationError, match="LIVE"):
        IBKRBroker(host="127.0.0.1", port=4001, client_id=1, trading_mode=TradingMode.LIVE)


def test_live_is_refused_by_the_execution_factory() -> None:
    """One of three independent refusals for the one irreversible action."""
    from trading_system.broker.base import BrokerConfigurationError
    from trading_system.broker.factory import build_execution_broker
    from trading_system.infrastructure.settings import Settings

    settings = Settings(
        trading_mode="LIVE",
        live_trading_confirmed=True,
        live_readiness_checklist_signed_off=True,
    )
    with pytest.raises(BrokerConfigurationError, match="LIVE"):
        build_execution_broker(settings)


def test_live_is_refused_by_configuration(system_config) -> None:
    from pydantic import ValidationError

    from trading_system.infrastructure.settings import ExecutionConfig

    payload = system_config.execution.model_dump() | {"allow_live": True}
    with pytest.raises(ValidationError, match="Milestone 12"):
        ExecutionConfig.model_validate(payload)


def test_the_read_only_setting_still_binds_the_execution_factory() -> None:
    """IBKR_READ_ONLY=true is the shipped default and refuses execution."""
    from trading_system.broker.base import BrokerConfigurationError
    from trading_system.broker.factory import build_execution_broker
    from trading_system.infrastructure.settings import BrokerBackend, Settings

    settings = Settings(trading_mode="PAPER", ibkr_read_only=True)
    with pytest.raises(BrokerConfigurationError, match="IBKR_READ_ONLY"):
        build_execution_broker(settings, backend=BrokerBackend.IBKR)


def test_place_order_is_final_and_cannot_be_overridden() -> None:
    """The read-only guard and the counter must not be bypassable."""
    import inspect

    from trading_system.broker.base import Broker

    source = inspect.getsource(Broker)
    assert "@final\n    def place_order" in source
    assert "@final\n    def cancel_order" in source


def test_the_submitted_counter_increments_before_the_submission() -> None:
    """So an ambiguous timeout cannot report zero submitted orders.

    Counting only successes would let exactly that case say "nothing was
    sent" — the one circumstance where a caller must not believe it.
    """
    import inspect

    from trading_system.broker.base import Broker

    source = inspect.getsource(Broker.place_order)
    increment = source.index("_orders_submitted += 1")
    submit = source.index("self._submit_order(intent)")
    assert increment < submit


# ---------------------------------------------------------------------------
# Execution needs no model
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("forbidden", FORBIDDEN_FOR_EXECUTION)
def test_execution_never_reaches_a_model(repo_root: Path, forbidden: str) -> None:
    """Brief section 65: the execution engine needs no LLM."""
    for path in _package_files(repo_root, "execution"):
        reachable = _transitive_imports(repo_root, path)
        offenders = sorted(name for name in reachable if name.startswith(forbidden))
        assert offenders == [], f"{path.name} reaches {forbidden}: {offenders}"


def test_no_execution_module_names_a_model_client(repo_root: Path) -> None:
    for path in _package_files(repo_root, "execution"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("LLMClient", "AnthropicLLMClient", "StructuredRequest", "load_prompt"):
            assert forbidden not in source, f"{path.name} references {forbidden}"


def test_the_execution_service_takes_no_model_client() -> None:
    import inspect

    from trading_system.execution.service import ExecutionService

    parameters = set(inspect.signature(ExecutionService.__init__).parameters)
    assert "llm_client" not in parameters
    assert "agent" not in parameters


# ---------------------------------------------------------------------------
# ib_async stays inside the adapter
# ---------------------------------------------------------------------------
def test_no_execution_module_instantiates_ib_async(repo_root: Path) -> None:
    """Brief 42.11. Execution depends on the broker interface, never the library.

    Reaching ``broker.factory`` transitively is expected and correct — that is
    how an order gets sent. What must not happen is execution business logic
    naming the IBKR library, or reaching into the adapter package to build one
    itself.
    """
    for path in _package_files(repo_root, "execution"):
        direct = _imports_of(path)
        assert "ib_async" not in direct, f"{path.name} imports ib_async"
        assert not any(name.startswith("trading_system.broker.ibkr") for name in direct), (
            f"{path.name} reaches into the IBKR adapter instead of the broker interface"
        )


def test_the_execution_service_depends_only_on_the_broker_interface(repo_root: Path) -> None:
    broker_imports = {
        name
        for path in _package_files(repo_root, "execution")
        for name in _imports_of(path)
        if name.startswith("trading_system.broker")
    }
    assert broker_imports <= {"trading_system.broker.base", "trading_system.broker.factory"}


def test_only_the_ibkr_client_imports_ib_async(repo_root: Path) -> None:
    offenders = set()
    for path in (repo_root / "src" / "trading_system").rglob("*.py"):
        if "ib_async" in _imports_of(path):
            offenders.add(str(path.relative_to(repo_root / "src" / "trading_system")))

    assert offenders == {"broker/ibkr/client.py"}


def test_the_ibkr_translation_modules_stay_pure(repo_root: Path) -> None:
    """Which is what lets them be tested exhaustively without a gateway.

    Parsed rather than grepped: each of these modules has a docstring saying it
    imports nothing from ``ib_async``, and that sentence must not count as a
    violation of itself.
    """
    for name in ("orders.py", "order_translation.py", "positions.py", "executions.py"):
        path = repo_root / "src" / "trading_system" / "broker" / "ibkr" / name
        assert "ib_async" not in _imports_of(path), f"{name} imports ib_async"


# ---------------------------------------------------------------------------
# The pure layers stay pure
# ---------------------------------------------------------------------------
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
    ("execution/order_builder.py", "execution/validation.py", "execution/purchase_card.py"),
)
def test_the_pure_modules_read_no_clock(repo_root: Path, module: str) -> None:
    """The instant is injected, so a validation is reproducible in a test."""
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
def test_naming_an_execution_type_does_not_import_a_broker(repo_root: Path) -> None:
    """A package ``__init__`` is part of every importer's graph.

    An eager re-export of ``service`` or ``execution_engine`` would put a
    *writable broker* into the import graph of anything that merely names an
    execution type.
    """
    reachable = _transitive_imports(repo_root, _source(repo_root, "execution/__init__.py"))

    offenders = sorted(name for name in reachable if name.startswith("trading_system.broker"))
    assert offenders == [], f"execution/__init__.py reaches a broker: {offenders}"


def test_the_execution_package_defers_its_service() -> None:
    """The lazy accessor is load-bearing; do not tidy it away."""
    import trading_system.execution as package

    assert "_LAZY" in vars(package)
    assert package._LAZY["ExecutionService"] == "service"
    assert package._LAZY["ExecutionEngine"] == "execution_engine"


def test_the_deferred_members_still_resolve() -> None:
    from trading_system.execution import ExecutionEngine, ExecutionService

    assert ExecutionService.__name__ == "ExecutionService"
    assert ExecutionEngine.__name__ == "ExecutionEngine"
