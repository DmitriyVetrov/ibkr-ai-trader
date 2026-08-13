"""Fixtures for the position suite.

Nothing here reaches a real broker. The one broker used is the in-process
simulator, constructed read-only, and several tests assert its own
submitted-order counter is still zero afterwards — the same evidence the
production code records on every snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from tests.positions.factories import ACCOUNT, NOW, broker_account
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.positions.service import PositionService
from trading_system.positions.store import (
    FilesystemFillRepository,
    FilesystemPositionRepository,
)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, trading_mode="PAPER")


@pytest.fixture
def empty_state() -> SimulatedBrokerState:
    """A simulator holding nothing, so a test controls exactly what is there.

    The shipped default fixture is deliberately non-empty, which is right for
    the broker suite and wrong here: a position test that inherited scenery
    would be asserting against someone else's portfolio.
    """
    return SimulatedBrokerState(
        account_id=ACCOUNT,
        currency="EUR",
        positions=[],
        open_orders=[],
        executions=[],
    )


@pytest.fixture
def broker(empty_state: SimulatedBrokerState, clock: FixedClock) -> SimulatedBroker:
    connection = SimulatedBroker(
        empty_state, clock=clock, trading_mode=TradingMode.PAPER, read_only=True
    )
    connection.connect()
    return connection


@pytest.fixture
def position_repository(tmp_path: Path) -> FilesystemPositionRepository:
    return FilesystemPositionRepository(tmp_path / "data" / "positions")


@pytest.fixture
def fill_repository(tmp_path: Path) -> FilesystemFillRepository:
    return FilesystemFillRepository(tmp_path / "data" / "fills")


@pytest.fixture
def make_service(
    settings: Settings,
    system_config: SystemConfig,
    clock: FixedClock,
    tmp_path: Path,
) -> Callable[..., PositionService]:
    """Build a position service wired to a temporary store and a given broker."""

    def build(broker: SimulatedBroker | None = None) -> PositionService:
        return PositionService(
            settings=settings,
            config=system_config,
            clock=clock,
            broker_factory=(lambda *a, **k: broker) if broker is not None else None,
            root=tmp_path,
        )

    return build


@pytest.fixture
def service(
    make_service: Callable[..., PositionService], broker: SimulatedBroker
) -> PositionService:
    return make_service(broker)


@pytest.fixture
def account():
    return broker_account()
