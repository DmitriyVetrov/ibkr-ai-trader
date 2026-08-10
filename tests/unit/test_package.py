"""The package imports cleanly and reports a coherent version."""

from __future__ import annotations

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
