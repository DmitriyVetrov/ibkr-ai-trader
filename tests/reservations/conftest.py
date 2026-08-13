"""Fixtures for the reservation suite.

Nothing here holds a broker — the reservation stage has none, and a test that
supplied one would be testing something this milestone deliberately cannot do.
Capital moves on the evidence the execution ledger already recorded, so the
fixtures supply execution records and let the lifecycle draw its conclusions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from tests.positions.factories import NOW
from trading_system.allocation.models import AllocationRunResult
from trading_system.allocation.store import (
    AllocationHistoryEntry,
    AllocationRepository,
    SymbolAllocationEntry,
)
from trading_system.execution.models import ExecutionRecord
from trading_system.execution.store import (
    ExecutionHistoryEntry,
    FilesystemExecutionRepository,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import ReservationPolicyConfig, Settings, SystemConfig
from trading_system.reservations.service import ReservationService
from trading_system.reservations.store import FilesystemReservationRepository


class StubAllocationRepository(AllocationRepository):
    """An allocation ledger held in memory, so a test arranges it exactly."""

    def __init__(self, runs: Sequence[AllocationRunResult] = ()) -> None:
        self._runs = list(runs)

    def save(self, result: AllocationRunResult) -> str:
        self._runs.append(result)
        return result.run_id

    def get(self, run_id: str) -> AllocationRunResult | None:
        return next((run for run in self._runs if run.run_id == run_id), None)

    def latest(self) -> AllocationRunResult | None:
        return self._runs[-1] if self._runs else None

    def all_runs(self) -> list[AllocationRunResult]:
        return list(self._runs)

    def history(self, limit: int | None = None) -> list[AllocationHistoryEntry]:
        return []

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolAllocationEntry]:
        return []


class StubExecutionRepository(FilesystemExecutionRepository):
    """A filesystem execution ledger a test can seed directly.

    Subclassed rather than faked: reservations read the *real* execution store
    interface, and a hand-written stand-in would eventually disagree with it in
    exactly the direction that makes a reservation test pass for the wrong
    reason.
    """

    def seed(self, *records: ExecutionRecord) -> None:
        for record in records:
            self.save(record)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, trading_mode="PAPER")


@pytest.fixture
def policy(system_config: SystemConfig) -> ReservationPolicyConfig:
    return system_config.reconciliation.reservations


@pytest.fixture
def reservation_repository(tmp_path: Path) -> FilesystemReservationRepository:
    return FilesystemReservationRepository(tmp_path / "data" / "reservations")


@pytest.fixture
def execution_store(tmp_path: Path) -> StubExecutionRepository:
    return StubExecutionRepository(tmp_path / "data" / "execution")


@pytest.fixture
def make_service(
    settings: Settings,
    system_config: SystemConfig,
    clock: FixedClock,
    tmp_path: Path,
    reservation_repository: FilesystemReservationRepository,
    execution_store: StubExecutionRepository,
) -> Callable[..., ReservationService]:
    def build(
        runs: Sequence[AllocationRunResult] = (),
        **kwargs: object,
    ) -> ReservationService:
        return ReservationService(
            settings=settings,
            config=system_config,
            clock=clock,
            reservation_repository=reservation_repository,
            allocation_repository=StubAllocationRepository(runs),
            execution_repository=execution_store,
            root=tmp_path,
            **kwargs,
        )

    return build


@pytest.fixture
def service(make_service: Callable[..., ReservationService], allocation_run) -> ReservationService:
    """A service over one real Milestone 7 allocation run."""
    return make_service([allocation_run])


__all__ = [
    "ExecutionHistoryEntry",
    "StubAllocationRepository",
    "StubExecutionRepository",
]
