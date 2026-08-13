"""Fixtures for the reconciliation suite.

The engine is a pure function of its arguments, so most tests here build
:class:`ReconciliationInputs` directly and never touch a broker at all. The
service tests use the in-process simulator through a temporary store, and every
one of them asserts the broker's own submitted-order counter is still zero.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from tests.positions.factories import ACCOUNT, MASKED, NOW
from tests.reservations.conftest import StubAllocationRepository, StubExecutionRepository
from trading_system.allocation.models import AllocationRunResult
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import BrokerReadStatus, TradingMode
from trading_system.domain.models import BrokerPosition
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import ReconciliationConfig, Settings, SystemConfig
from trading_system.positions.models import BrokerPositionSnapshot
from trading_system.positions.service import PositionService
from trading_system.positions.snapshot import build_position_snapshot, unavailable_snapshot
from trading_system.reconciliation.engine import ReconciliationEngine, ReconciliationInputs
from trading_system.reconciliation.service import ReconciliationService
from trading_system.reservations.service import ReservationService


class WiredReconciliation(ReconciliationService):
    """A reconciliation service that remembers what a test wired it to.

    The broker and the execution ledger are declared here rather than attached
    dynamically, so a test can seed one and assert against the other without
    reaching into private attributes.
    """

    broker: SimulatedBroker
    executions: StubExecutionRepository


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, trading_mode="PAPER")


@pytest.fixture
def policy(system_config: SystemConfig) -> ReconciliationConfig:
    return system_config.reconciliation


@pytest.fixture
def engine(policy: ReconciliationConfig) -> ReconciliationEngine:
    return ReconciliationEngine(policy)


@pytest.fixture
def snapshot_of() -> Callable[..., BrokerPositionSnapshot]:
    """Build a broker snapshot from a list of broker positions."""

    def build(positions: Sequence[BrokerPosition] = ()) -> BrokerPositionSnapshot:
        return build_position_snapshot(
            list(positions),
            broker="SIMULATOR",
            account_id=ACCOUNT,
            trading_mode=TradingMode.PAPER,
            as_of=NOW,
            observed_at=NOW,
        )

    return build


@pytest.fixture
def unreadable_snapshot() -> BrokerPositionSnapshot:
    return unavailable_snapshot(
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
        status=BrokerReadStatus.UNAVAILABLE,
        detail="the gateway refused the connection",
    )


@pytest.fixture
def inputs_for(snapshot_of) -> Callable[..., ReconciliationInputs]:
    """Assemble engine inputs with sensible, readable defaults."""

    def build(**kwargs: object) -> ReconciliationInputs:
        defaults: dict[str, object] = {
            "campaign_id": "campaign-001",
            "broker": "SIMULATOR",
            "account_reference": MASKED,
            "trading_mode": TradingMode.PAPER,
            "as_of": NOW,
            "observed_at": NOW,
            "snapshot": snapshot_of([]),
            "account_read": BrokerReadStatus.OK,
            "orders_read": BrokerReadStatus.EMPTY,
            "fills_read": BrokerReadStatus.EMPTY,
            "config_version": "test",
        }
        return ReconciliationInputs(**(defaults | kwargs))  # type: ignore[arg-type]

    return build


@pytest.fixture
def make_service(
    settings: Settings,
    system_config: SystemConfig,
    clock: FixedClock,
    tmp_path: Path,
    allocation_run,
) -> Callable[..., WiredReconciliation]:
    """A reconciliation service over a temporary store and an in-process broker."""

    def build(
        state: SimulatedBrokerState | None = None,
        *,
        broker: SimulatedBroker | None = None,
        runs: Sequence[AllocationRunResult] | None = None,
    ) -> WiredReconciliation:
        connection = broker or SimulatedBroker(
            state
            if state is not None
            else SimulatedBrokerState(account_id=ACCOUNT, currency="EUR"),
            clock=clock,
            trading_mode=TradingMode.PAPER,
            read_only=True,
        )
        executions = StubExecutionRepository(tmp_path / "data" / "execution")
        positions = PositionService(
            settings=settings,
            config=system_config,
            clock=clock,
            execution_repository=executions,
            broker_factory=lambda *a, **k: connection,
            root=tmp_path,
        )
        reservations = ReservationService(
            settings=settings,
            config=system_config,
            clock=clock,
            allocation_repository=StubAllocationRepository(
                list(runs) if runs is not None else [allocation_run]
            ),
            execution_repository=executions,
            root=tmp_path,
        )
        service = WiredReconciliation(
            settings=settings,
            config=system_config,
            clock=clock,
            position_service=positions,
            reservation_service=reservations,
            execution_repository=executions,
            root=tmp_path,
        )
        # Handed back on the service so a test can seed the ledgers and assert
        # against the same broker the run will use.
        service.broker = connection
        service.executions = executions
        return service

    return build


@pytest.fixture
def service(make_service: Callable[..., WiredReconciliation]) -> WiredReconciliation:
    return make_service()
