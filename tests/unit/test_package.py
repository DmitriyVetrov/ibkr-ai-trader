"""The package imports cleanly and reports a coherent version."""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path

import pytest

MODULES = [
    "trading_system",
    "trading_system.cli",
    "trading_system.domain.enums",
    "trading_system.domain.events",
    "trading_system.domain.models",
    "trading_system.domain.state_machine",
    "trading_system.infrastructure.clock",
    "trading_system.infrastructure.logging",
    "trading_system.infrastructure.settings",
]


@pytest.mark.unit
@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


@pytest.mark.unit
def test_package_exposes_version() -> None:
    import trading_system

    assert isinstance(trading_system.__version__, str)
    assert trading_system.__version__


@pytest.mark.unit
def test_version_matches_pyproject(repo_root: Path) -> None:
    """A drifting version would mis-stamp every trade record."""
    import trading_system

    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == trading_system.__version__


@pytest.mark.unit
def test_domain_does_not_import_infrastructure() -> None:
    """The domain layer stays pure: no I/O, no settings, no broker."""
    import trading_system.domain.models as models
    import trading_system.domain.state_machine as state_machine

    for module in (models, state_machine):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "trading_system.infrastructure" not in source
        assert "trading_system.broker" not in source


def _imported_modules(path: Path) -> set[str]:
    """Top-level module names actually imported by a file.

    Parsed rather than grepped: the string "ib_async" appears in several
    docstrings that explain the boundary, and those must not count as
    violations of it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    return imported


@pytest.mark.unit
def test_only_the_ibkr_adapter_imports_ib_async(repo_root: Path) -> None:
    """The broker library must not leak past its adapter.

    If any other module imports ib_async, the abstraction has been bypassed and
    application code has become coupled to one broker.
    """
    allowed = repo_root / "src" / "trading_system" / "broker" / "ibkr"
    offenders = [
        str(path.relative_to(repo_root))
        for path in (repo_root / "src").rglob("*.py")
        if "ib_async" in _imported_modules(path) and allowed not in path.parents
    ]
    assert offenders == [], f"ib_async imported outside broker/ibkr/: {offenders}"


@pytest.mark.unit
def test_domain_does_not_import_the_broker_layer(repo_root: Path) -> None:
    """The domain layer stays free of broker implementation details."""
    domain = repo_root / "src" / "trading_system" / "domain"
    for path in domain.rglob("*.py"):
        imported = _imported_modules(path)
        assert "ib_async" not in imported, path
        source = path.read_text(encoding="utf-8")
        assert "from trading_system.broker" not in source, path
        assert "import trading_system.broker" not in source, path


@pytest.mark.unit
def test_ibkr_translation_modules_do_not_import_the_library(repo_root: Path) -> None:
    """Only client.py touches ib_async; the translators stay pure and testable.

    This is what lets the position/order/execution/quote mapping be tested with
    plain fakes, without the library or a gateway.
    """
    adapter = repo_root / "src" / "trading_system" / "broker" / "ibkr"
    for name in ("positions.py", "orders.py", "executions.py", "market_data.py", "conversion.py"):
        assert "ib_async" not in _imported_modules(adapter / name), (
            f"{name} must not import ib_async"
        )
