"""Fixtures for the orphan-cleanup suite.

Everything runs in process. The broker is the simulator, the stores are under
``tmp_path``, and the one thing every fixture here refuses to build is a
*writable* broker for anything but the explicitly authorised path — because
"a review cannot place an order" is the claim most of these tests exist to
check, and a fixture that handed one out would make the claim untestable.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.positions.factories import ACCOUNT, MASKED, option_position
from tests.reservations.conftest import StubAllocationRepository, StubExecutionRepository
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.cleanup.models import CleanupTarget
from trading_system.cleanup.service import CleanupService
from trading_system.cleanup.store import FilesystemCleanupRepository
from trading_system.domain.enums import (
    OptionRight,
    ReconciliationFindingType,
    SecurityType,
    TradingMode,
)
from trading_system.domain.models import BrokerPosition
from trading_system.execution.service import ExecutionService
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.positions.service import PositionService
from trading_system.reconciliation.service import ReconciliationService
from trading_system.reservations.service import ReservationService

#: A Monday inside the regular NYSE session, so the market-session gate does not
#: fire for reasons unrelated to what a test is about.
NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

ORPHAN_CALL_ID = 848575117
ORPHAN_PUT_ID = 848575500
ORPHAN_CALL_KEY = f"cid:{ORPHAN_CALL_ID}"
ORPHAN_PUT_KEY = f"cid:{ORPHAN_PUT_ID}"


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def settings() -> Settings:
    """PAPER, naming the account the simulator will report.

    The account gate compares what the *broker* reported against what was
    configured, so a fixture that left the second unset would fail every
    authorised run for a reason unrelated to what the test is about.
    """
    return Settings(_env_file=None, trading_mode="PAPER", ibkr_account=ACCOUNT)


@pytest.fixture
def cleanup_enabled_config(system_config: SystemConfig) -> SystemConfig:
    """The shipped configuration with both master switches pinned ON.

    Named after what it *is*, and used only by tests whose subject is what
    happens once an operator has deliberately opened both switches. The shipped
    values stay asserted, once each, in ``test_gates.py`` — where that is the
    fact under test.
    """
    return system_config.model_copy(
        update={
            "cleanup": system_config.cleanup.model_copy(update={"enabled": True}),
            "execution": system_config.execution.model_copy(update={"enabled": True}),
        }
    )


@pytest.fixture
def cleanup_disabled_config(system_config: SystemConfig) -> SystemConfig:
    return system_config.model_copy(
        update={
            "cleanup": system_config.cleanup.model_copy(update={"enabled": False}),
            "execution": system_config.execution.model_copy(update={"enabled": False}),
        }
    )


def orphan_position(
    *,
    contract_id: int = ORPHAN_CALL_ID,
    quantity: Decimal = Decimal("1"),
    strike: Decimal = Decimal("540.00"),
    right: OptionRight = OptionRight.CALL,
    market_value: Decimal | None = Decimal("4877.46"),
) -> BrokerPosition:
    """A holding the account has and no execution of ours accounts for.

    Modelled on the four this repository actually found in its paper account:
    long one contract, SMH, priced by the broker, with nothing internal behind
    it.
    """
    position = option_position(
        contract_id=contract_id,
        quantity=quantity,
        strike=strike,
        right=right,
        symbol="SMH",
        average_cost=Decimal("3955.7673"),
        market_value=market_value,
        currency="USD",
    )
    return position.model_copy(
        update={"market_price": Decimal("48.77") if market_value is not None else None}
    )


def target_from(
    position: BrokerPosition,
    *,
    finding_id: str = "finding-test",
    reconciliation_id: str = "reconciliation-test",
    account_reference: str = MASKED,
) -> CleanupTarget:
    """A cleanup target built directly, for the unit tests that need only one."""
    assert position.contract_id is not None
    return CleanupTarget(
        key=f"cid:{position.contract_id}",
        contract_id=position.contract_id,
        position_id="position-test",
        account_reference=account_reference,
        underlying=position.symbol,
        symbol=position.symbol,
        asset_class=SecurityType.OPTION,
        expiration=position.expiration,
        strike=position.strike,
        right=position.right,
        multiplier=position.multiplier,
        local_symbol=position.local_symbol,
        currency=position.currency,
        quantity=position.quantity,
        average_cost=position.average_cost,
        market_price=position.market_price,
        market_value=position.market_value,
        finding_id=finding_id,
        reconciliation_id=reconciliation_id,
        broker_source="SIMULATOR",
        observed_at=NOW,
    )


class WiredCleanup(CleanupService):
    """A cleanup service that remembers what a test wired it to."""

    broker: SimulatedBroker
    execution_ledger: StubExecutionRepository


@pytest.fixture
def make_service(
    settings: Settings,
    clock: FixedClock,
    tmp_path: Path,
) -> Callable[..., WiredCleanup]:
    """A cleanup service over a temporary store and an in-process broker.

    The *same* simulator instance is handed to the read path and the write
    path, so a test can assert that a review left ``orders_submitted`` at zero
    on the very object an authorised run would have used.
    """

    def build(
        positions: Sequence[BrokerPosition] = (),
        *,
        config: SystemConfig,
        read_only: bool = False,
        state: SimulatedBrokerState | None = None,
    ) -> WiredCleanup:
        broker_state = state or SimulatedBrokerState(
            account_id=ACCOUNT, currency="EUR", positions=list(positions)
        )
        connection = SimulatedBroker(
            broker_state, clock=clock, trading_mode=TradingMode.PAPER, read_only=read_only
        )
        ledger = StubExecutionRepository(tmp_path / "data" / "execution")
        position_service = PositionService(
            settings=settings,
            config=config,
            clock=clock,
            execution_repository=ledger,
            broker_factory=lambda *a, **k: connection,
            root=tmp_path,
        )
        reservations = ReservationService(
            settings=settings,
            config=config,
            clock=clock,
            allocation_repository=StubAllocationRepository([]),
            execution_repository=ledger,
            root=tmp_path,
        )
        reconciliation = ReconciliationService(
            settings=settings,
            config=config,
            clock=clock,
            position_service=position_service,
            reservation_service=reservations,
            execution_repository=ledger,
            root=tmp_path,
        )
        executions = ExecutionService(
            settings=settings,
            config=config,
            clock=clock,
            execution_repository=ledger,
            broker_factory=lambda *a, **k: connection,
            root=tmp_path,
        )
        service = WiredCleanup(
            settings=settings,
            config=config,
            clock=clock,
            reconciliation_service=reconciliation,
            execution_service=executions,
            repository=FilesystemCleanupRepository(tmp_path / "data" / "cleanup"),
            root=tmp_path,
        )
        service.broker = connection
        service.execution_ledger = ledger
        return service

    return build


@pytest.fixture
def service(
    make_service: Callable[..., WiredCleanup], cleanup_enabled_config: SystemConfig
) -> WiredCleanup:
    """Two orphan holdings, both master switches on."""
    return make_service(
        [
            orphan_position(),
            orphan_position(
                contract_id=ORPHAN_PUT_ID,
                strike=Decimal("545.00"),
                right=OptionRight.PUT,
                market_value=Decimal("103.00"),
            ),
        ],
        config=cleanup_enabled_config,
    )


def orphan_keys(result: object) -> set[str]:
    """Every contract a reconciliation reported as an orphan."""
    findings = getattr(result, "findings", [])
    return {
        finding.identifier
        for finding in findings
        if finding.finding_type is ReconciliationFindingType.ORPHAN_BROKER_POSITION
    }
