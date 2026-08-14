"""Fixtures for the exit-management suites.

Three rules hold across every test here:

* **No network, no broker, no model.** The exit service is constructed with a
  repository, a configuration and an injected clock. Where a broker is needed
  at all it is the simulator, supplied explicitly, and every assertion about
  orders reads the broker's own counter rather than asserting zero by hand.
* **No writing into the repository's own ``data/``.** Every store is rooted at
  ``tmp_path``.
* **Positions are built the way the system builds them.** An entry execution
  with confirmed fills, a broker snapshot, and Milestone 9's own projection —
  never a hand-assembled ``OpenPosition``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.execution.models import ExecutionRecord
from trading_system.execution.store import FilesystemExecutionRepository
from trading_system.exit.service import ExitService
from trading_system.exit.store import FilesystemExitRepository
from trading_system.exit.valuation import ExitQuoteReader
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.positions.service import PositionService
from trading_system.positions.store import FilesystemPositionRepository


@pytest.fixture
def exit_now() -> datetime:
    return NOW


@pytest.fixture
def exit_clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def data_repo(data_root: Path, exit_clock: FixedClock):
    return factories.data_repository(data_root, clock=exit_clock)


@pytest.fixture
def exit_repo(data_root: Path) -> FilesystemExitRepository:
    return FilesystemExitRepository(data_root / "exit")


@pytest.fixture
def execution_repo(data_root: Path) -> FilesystemExecutionRepository:
    return FilesystemExecutionRepository(data_root / "execution")


@pytest.fixture
def position_repo(data_root: Path) -> FilesystemPositionRepository:
    return FilesystemPositionRepository(data_root / "positions")


@pytest.fixture
def paper_settings() -> Settings:
    """PAPER, read-only, exactly as the suite's safety guard forces."""
    return Settings()


@pytest.fixture
def build_exit_service(
    paper_settings: Settings,
    system_config: SystemConfig,
    exit_clock: FixedClock,
    data_root: Path,
    exit_repo: FilesystemExitRepository,
    execution_repo: FilesystemExecutionRepository,
    position_repo: FilesystemPositionRepository,
    data_repo,
) -> Callable[..., ExitService]:
    """Build a service wired entirely at ``tmp_path``.

    Returned as a factory rather than a value so a test can rebuild it — which
    is how the restart guarantee is checked: the second service shares only the
    *filesystem* with the first, so anything it remembers came off disk.
    """

    def _make(
        *,
        executions: Sequence[ExecutionRecord] = (),
        snapshot: object | None = None,
        config: SystemConfig | None = None,
    ) -> ExitService:
        resolved = config or system_config
        for record in executions:
            execution_repo.save(record)
        if snapshot is not None:
            position_repo.save_snapshot(snapshot)  # type: ignore[arg-type]

        positions = PositionService(
            settings=paper_settings,
            config=resolved,
            clock=exit_clock,
            position_repository=position_repo,
            execution_repository=execution_repo,
            root=data_root.parent,
        )
        return ExitService(
            settings=paper_settings,
            config=resolved,
            clock=exit_clock,
            exit_repository=exit_repo,
            position_service=positions,
            quote_reader=ExitQuoteReader(data_repo),
            root=data_root.parent,
        )

    return _make


@pytest.fixture
def stored_research(data_root: Path, market_research_run) -> str:
    """A real Milestone 5 run on disk, so the thesis policy has something to read.

    Stored through the research repository rather than handed to the service,
    because the exit stage *consumes* research the way it will in production —
    by reading the immutable run — and a test that injected a projection would
    not exercise the read at all.
    """
    from trading_system.research.store import FilesystemResearchRepository

    FilesystemResearchRepository(data_root / "research").save(market_research_run)
    return str(market_research_run.reports[0].report_id)


@pytest.fixture
def open_long_call(build_exit_service, data_repo, stored_research):
    """One open long call, priced, with everything a full evaluation needs."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    execution = factories.entry_execution(research_report_id=stored_research)
    service = build_exit_service(executions=[execution], snapshot=factories.position_snapshot())
    return service, execution


@pytest.fixture
def drive_lifecycle(exit_repo: FilesystemExitRepository):
    """Walk a position's lifecycle to a state, one legal edge at a time.

    Deliberately *through the graph* rather than by writing the state directly:
    a fixture that could plant an unreachable state would let a test assert
    behaviour the system can never actually be in.
    """
    from trading_system.domain.enums import PositionLifecycleEventType, PositionLifecycleState
    from trading_system.exit.models import PositionLifecycleEvent, lifecycle_event_identifier

    event_for = {
        PositionLifecycleState.MONITORING: PositionLifecycleEventType.LIFECYCLE_MONITORED,
        PositionLifecycleState.TRAILING_ACTIVE: PositionLifecycleEventType.TRAILING_ACTIVATED,
        PositionLifecycleState.EXIT_REQUIRED: PositionLifecycleEventType.EXIT_REQUIRED,
        PositionLifecycleState.EXIT_SUBMITTED: PositionLifecycleEventType.EXIT_SUBMITTED,
        PositionLifecycleState.EXIT_UNKNOWN: PositionLifecycleEventType.EXIT_STATE_UNKNOWN,
        PositionLifecycleState.CLOSED: PositionLifecycleEventType.EXIT_CONFIRMED_CLOSED,
        PositionLifecycleState.BLOCKED: PositionLifecycleEventType.LIFECYCLE_BLOCKED,
    }

    def _drive(
        position,
        *states,
        exit_execution_id: str = "execution-exit-1",
    ) -> None:
        from trading_system.domain.enums import ExitReasonCode

        exit_repo.save_lifecycle(position.lifecycle)
        for sequence, state in enumerate(states):
            exit_repo.append_lifecycle_event(
                PositionLifecycleEvent(
                    event_id=lifecycle_event_identifier(
                        position_id=position.position_id,
                        sequence=sequence,
                        event_type=event_for[state].value,
                    ),
                    position_id=position.position_id,
                    sequence=sequence,
                    event_type=event_for[state],
                    state=state,
                    occurred_at=NOW,
                    observed_at=NOW,
                    source="test",
                    reason_code=(
                        ExitReasonCode.MARKET_DATA_UNAVAILABLE
                        if state is PositionLifecycleState.BLOCKED
                        else None
                    ),
                    exit_execution_id=exit_execution_id,
                )
            )

    return _drive


@pytest.fixture
def open_straddle(build_exit_service, data_repo, stored_research):
    """One open straddle: two legs, one structure, one lifecycle."""
    from decimal import Decimal

    from trading_system.domain.enums import OptionRight, StrategyType

    factories.store_quotes(
        data_repo,
        [
            factories.option_quote(),
            factories.option_quote(
                contract_id=factories.PUT_CONTRACT_ID,
                right=OptionRight.PUT,
                bid=Decimal("4.20"),
                ask=Decimal("4.40"),
                last=Decimal("4.30"),
            ),
        ],
    )
    execution = factories.entry_execution(
        legs=factories.straddle_legs(),
        strategy=StrategyType.LONG_STRADDLE,
        research_report_id=stored_research,
    )
    snapshot = factories.position_snapshot(
        [
            factories.broker_position(),
            factories.broker_position(contract_id=factories.PUT_CONTRACT_ID, right=OptionRight.PUT),
        ]
    )
    service = build_exit_service(executions=[execution], snapshot=snapshot)
    return service, execution
