"""Architectural boundaries around operations and telemetry.

These are the tests that make Milestone 11's central claims structural rather
than stylistic. The documentation *says* the scheduler holds no broker, that an
alert cannot trade, and that telemetry cannot reach a trading decision; these
assert that none of them has the option.

Import boundaries are checked by parsing the source, not by grepping: the words
"broker", "order" and "trade" appear throughout the docstrings that explain the
boundary, and those must not count as violations of it. Both direct imports and
the whole transitive closure are checked, following package ``__init__`` files,
because importing ``a.b.c`` executes ``a.b.__init__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Modules the *alerting* path must never reach. An alert reports; if it could
#: reach an execution service it could, eventually, be made to act.
ALERT_FORBIDDEN = (
    "trading_system.agents",
    "trading_system.broker",
    "trading_system.execution.service",
    "trading_system.exit.service",
    "anthropic",
    "ib_async",
)

#: Network and process control. The alert *rules* must be a pure function of
#: captured facts; delivery is a separate module, and that separation is what
#: this list enforces.
FORBIDDEN_STDLIB = ("socket", "urllib", "http", "subprocess", "requests", "httpx")

#: The pure half of the operations package: rules and shapes, no I/O.
PURE_OPERATIONS = ("models.py", "cron.py", "alerts.py", "health.py")

#: Every module in the observability package that trading code may import.
#: These must be reachable from an agent, a risk engine and an exit engine
#: without dragging the OpenTelemetry SDK — whose exporter imports sockets —
#: into their graphs.
TELEMETRY_SAFE_MODULES = (
    "attributes.py",
    "privacy.py",
    "provider.py",
    "tracing.py",
    "metrics.py",
    "instrument.py",
    "llm.py",
    "logging.py",
    "__init__.py",
)


def _imports_of(path: Path) -> set[str]:
    """Every module a file imports at runtime, parsed rather than grepped.

    ``if TYPE_CHECKING:`` bodies are skipped because they genuinely never
    execute — an annotation-only import creates no runtime edge, and counting
    one would make the deliberate use of the lazy-import pattern look like a
    violation.
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


def _package(repo_root: Path, name: str) -> Path:
    return repo_root / "src" / "trading_system" / name


def _files(repo_root: Path, package: str) -> list[Path]:
    return sorted(_package(repo_root, package).glob("*.py"))


# ---------------------------------------------------------------------------
# The packages are shaped as documented
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", PURE_OPERATIONS)
def test_every_documented_operations_module_exists(repo_root: Path, name: str) -> None:
    assert (_package(repo_root, "operations") / name).is_file()


@pytest.mark.parametrize("name", TELEMETRY_SAFE_MODULES)
def test_every_documented_observability_module_exists(repo_root: Path, name: str) -> None:
    assert (_package(repo_root, "observability") / name).is_file()


# ---------------------------------------------------------------------------
# An alert cannot trade
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("forbidden", ALERT_FORBIDDEN)
def test_the_alert_rules_cannot_reach_an_order_path(repo_root: Path, forbidden: str) -> None:
    """Safety is enforced by the domain. Alerting is how a person finds out."""
    reachable = _transitive_imports(repo_root, _package(repo_root, "operations") / "alerts.py")

    offenders = sorted(name for name in reachable if name.startswith(forbidden))
    assert offenders == [], f"operations/alerts.py reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_alert_rules_cannot_open_a_connection(repo_root: Path, forbidden: str) -> None:
    """Deciding an alert and delivering one are separate modules, and this is
    what keeps them separate: the rules are a pure function of captured facts."""
    reachable = _transitive_imports(repo_root, _package(repo_root, "operations") / "alerts.py")

    offenders = sorted(
        name for name in reachable if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"operations/alerts.py reaches {forbidden}: {offenders}"


#: Order APIs, matched as *calls* rather than as substrings.
#:
#: The substring form would flag ``can_submit_orders`` — a field whose whole
#: purpose is to say which job could reach one — which is the same mistake the
#: other boundary suites avoid by parsing rather than grepping. What must not
#: appear is an invocation.
ORDER_CALLS = (".place_order(", ".placeOrder(", "._submit_order(", ".cancel_order(")


@pytest.mark.parametrize("call", ORDER_CALLS)
def test_no_operations_module_calls_an_order_api(repo_root: Path, call: str) -> None:
    for path in _files(repo_root, "operations"):
        source = path.read_text(encoding="utf-8")
        assert call not in source, f"{path.name} calls {call}"


def test_no_operations_module_names_a_model_client(repo_root: Path) -> None:
    """The scheduler orchestrates deterministic services. No operational
    decision is made by a model."""
    for path in _files(repo_root, "operations"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("LLMClient", "AnthropicLLMClient", "StructuredRequest", "load_prompt"):
            assert forbidden not in source, f"{path.name} references {forbidden}"


# ---------------------------------------------------------------------------
# The scheduler holds no broker
# ---------------------------------------------------------------------------
def test_the_scheduler_does_not_import_a_broker(repo_root: Path) -> None:
    """Services open their own short-lived read-only connections. A scheduler
    holding a persistent connection and polling through it is exactly the shape
    Milestone 2's one-reliable-round-trip constraint forbids."""
    direct = _imports_of(_package(repo_root, "operations") / "scheduler.py")

    assert not [name for name in direct if name.startswith("trading_system.broker")]


def test_only_the_health_check_can_construct_a_broker(repo_root: Path) -> None:
    """And only the read-only one, and only when explicitly asked to probe."""
    constructors = {
        path.name
        for path in _files(repo_root, "operations")
        if "build_broker" in path.read_text(encoding="utf-8")
    }

    assert constructors == {"service.py"}


def test_no_operations_module_reaches_the_writable_broker_factory(repo_root: Path) -> None:
    """``build_execution_broker`` is the only writable constructor in the
    system, and the execution service is its only caller."""
    for path in _files(repo_root, "operations"):
        assert "build_execution_broker" not in path.read_text(encoding="utf-8"), path.name


# ---------------------------------------------------------------------------
# Telemetry cannot reach a trading decision
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", TELEMETRY_SAFE_MODULES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_instrumented_telemetry_path_opens_no_socket(
    repo_root: Path, name: str, forbidden: str
) -> None:
    """The load-bearing claim of the package layout.

    The research agent, the exit engine, the risk engine and the strategy
    selector all have boundary tests forbidding sockets in their import graphs,
    and every one of them now calls into this package. The OTLP exporter
    imports ``socket``, ``urllib`` and ``http`` — so the SDK is confined to
    ``otel.py``, which nothing here may reach.
    """
    reachable = _transitive_imports(repo_root, _package(repo_root, "observability") / name)

    offenders = sorted(
        module for module in reachable if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"observability/{name} reaches {forbidden}: {offenders}"


@pytest.mark.parametrize("name", TELEMETRY_SAFE_MODULES)
def test_the_instrumented_telemetry_path_never_reaches_the_sdk(repo_root: Path, name: str) -> None:
    reachable = _transitive_imports(repo_root, _package(repo_root, "observability") / name)

    assert not [module for module in reachable if module.startswith("opentelemetry")]
    assert "trading_system.observability.otel" not in reachable


def test_only_two_modules_name_the_sdk(repo_root: Path) -> None:
    """``otel.py`` imports it; ``runtime.py`` imports ``otel``. Nothing else."""
    naming = {
        path.name
        for path in _files(repo_root, "observability")
        if "opentelemetry" in path.read_text(encoding="utf-8")
    }

    assert naming == {"otel.py"}


@pytest.mark.parametrize(
    "name", ("attributes.py", "privacy.py", "provider.py", "tracing.py", "metrics.py")
)
def test_the_telemetry_core_imports_no_trading_package(repo_root: Path, name: str) -> None:
    """Telemetry observes the trading system; it does not know what a trade is.

    A dependency the other way would let a telemetry module read a domain
    value — and the moment it can read one, somebody can make a decision from
    it.
    """
    reachable = _transitive_imports(repo_root, _package(repo_root, "observability") / name)

    trading = sorted(
        module
        for module in reachable
        if module.startswith("trading_system.")
        and not module.startswith("trading_system.observability")
    )
    assert trading == [], f"observability/{name} reaches {trading}"


@pytest.mark.parametrize("call", ORDER_CALLS)
def test_no_telemetry_module_calls_an_order_api(repo_root: Path, call: str) -> None:
    for path in _files(repo_root, "observability"):
        assert call not in path.read_text(encoding="utf-8"), f"{path.name} calls {call}"


# ---------------------------------------------------------------------------
# The profit-and-loss package
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "forbidden",
    (
        "trading_system.agents",
        "trading_system.broker",
        "anthropic",
        "ib_async",
    ),
)
def test_the_pnl_package_reaches_no_broker_and_no_model(repo_root: Path, forbidden: str) -> None:
    """What a trade made is arithmetic over confirmed fills. Whether the
    position is closed is read from Milestone 10's lifecycle, which read it
    from Milestone 9's snapshot, which read it from a read-only connection."""
    for path in _files(repo_root, "pnl"):
        offenders = sorted(module for module in _imports_of(path) if module.startswith(forbidden))
        assert offenders == [], f"pnl/{path.name} imports {forbidden}: {offenders}"


@pytest.mark.parametrize("name", ("models.py", "calculator.py", "settlement.py"))
@pytest.mark.parametrize("forbidden", FORBIDDEN_STDLIB)
def test_the_pure_pnl_modules_open_no_connection(
    repo_root: Path, name: str, forbidden: str
) -> None:
    """A stored realised result has to be reproducible from the stored fills,
    which means the thing that computes it can have no other input."""
    reachable = _transitive_imports(repo_root, _package(repo_root, "pnl") / name)

    offenders = sorted(
        module for module in reachable if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert offenders == [], f"pnl/{name} reaches {forbidden}: {offenders}"


def test_the_campaign_state_reader_reaches_no_broker(repo_root: Path) -> None:
    """It is imported by ``allocation/``, whose own boundary test forbids a
    broker, a provider and a data repository in its graph."""
    reachable = _transitive_imports(repo_root, _package(repo_root, "pnl") / "campaign_state.py")

    for forbidden in ("trading_system.broker", "trading_system.positions.service"):
        assert not [module for module in reachable if module.startswith(forbidden)]


def test_there_is_no_second_capital_ledger(repo_root: Path) -> None:
    """Capital moves as an appended event on the Milestone 9 reservation.

    A store that also kept balances would be a second copy of the truth, and
    when two copies disagree there is no way to tell which is wrong.
    """
    store = (_package(repo_root, "pnl") / "store.py").read_text(encoding="utf-8")

    for forbidden in ("class Reservation", "def consume", "def release", "committed_total"):
        assert forbidden not in store
