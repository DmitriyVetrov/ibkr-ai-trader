"""The reconciliation service: the composition root for Milestone 9.

One read of the broker, one comparison, and every consequence recorded:

.. code-block:: text

    Broker (read-only, ONE short-lived connection)
          |
    account + positions + open orders + fills
          |
    BrokerPositionSnapshot + recorded fills      (positions/)
    AccountSnapshot                              (risk/, reused not forked)
          |
    resolve UNKNOWN executions from broker evidence   (execution ledger: appended)
          |
    reservation lifecycle                        (reservations/)
          |
    ReconciliationEngine                         deterministic, pure
          |
    immutable ReconciliationResult + events

Properties this service holds regardless of which path it takes:

* **It cannot trade.** Its only broker comes from
  :class:`~trading_system.positions.service.PositionService`, which builds a
  read-only connection and asserts the broker's own submitted-order counter is
  zero afterwards. The result model refuses to be constructed with a non-zero
  ``orders_submitted`` or ``corrective_orders``, and a test walks the import
  graph to prove the writable broker constructor is unreachable from here.
* **It repairs nothing.** No internal record is edited to agree with the
  broker, no position is adopted, no order is cancelled, and no compensating
  trade is proposed. What it writes is: observations of broker state,
  resolutions of ambiguous submissions *as the broker reported them*, and the
  economic consequences those resolutions have for committed capital.
* **It reads the broker once.** Account summary, positions, open orders and
  fills all come from ``ib_async``'s startup handshake cache, so one connection
  answers all four without a second uncached round trip. No health probe is
  issued first.
* **It is idempotent.** Re-running over unchanged state re-observes the same
  snapshot, records no new fill, resolves nothing twice and moves no capital —
  the result is content-addressed and lands on the same id.

Ordering matters and is deliberate: ambiguous executions are resolved *before*
reservations are applied, so a submission the broker turns out to have filled
consumes its capital in the same run that discovered the fill.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from trading_system.domain.enums import (
    ExecutionState,
    ReconciliationEventType,
    ReconciliationRunStatus,
)
from trading_system.execution.models import ExecutionRecord
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import (
    TRADING_RECONCILIATION_ID,
    TRADING_STATUS,
)
from trading_system.observability.instrument import traced
from trading_system.positions.expected import ExpectedProjection
from trading_system.positions.models import ObservedFill
from trading_system.positions.service import BrokerState, PositionCapture, PositionService
from trading_system.reconciliation.engine import ReconciliationEngine, ReconciliationInputs
from trading_system.reconciliation.models import (
    ReconciliationEvent,
    ReconciliationResult,
    reconciliation_event_identifier,
)
from trading_system.reconciliation.store import (
    FilesystemReconciliationRepository,
    ReconciliationHistoryEntry,
    ReconciliationRepository,
)
from trading_system.reconciliation.unknown import UnknownResolution, resolve_unknown
from trading_system.reservations.service import ReservationService, ReservationUpdate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.broker.base import Broker
    from trading_system.execution.store import ExecutionRepository
    from trading_system.risk.models import AccountSnapshot

__all__ = ["ReconciliationRun", "ReconciliationService"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    """One reconciliation, and everything a caller needs to report it."""

    result: ReconciliationResult
    capture: PositionCapture
    projection: ExpectedProjection
    updates: tuple[ReservationUpdate, ...] = ()
    resolutions: tuple[UnknownResolution, ...] = ()
    stored: bool = False
    #: False when the same comparison was already on file — a re-observation.
    is_new: bool = False
    dry_run: bool = False
    duration_seconds: float = 0.0

    @property
    def matched(self) -> bool:
        return self.result.matched

    @property
    def orders_submitted(self) -> int:
        """Read off the broker. Structurally zero, and reported as evidence."""
        return self.capture.orders_submitted

    @property
    def corrective_orders(self) -> int:
        """Always zero. Reconciliation reports; it does not repair."""
        return 0


class ReconciliationService:
    """Compares internal records against broker reality, and records the outcome."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        position_service: PositionService | None = None,
        reservation_service: ReservationService | None = None,
        reconciliation_repository: ReconciliationRepository | None = None,
        execution_repository: ExecutionRepository | None = None,
        broker_factory: object | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._positions = position_service or PositionService(
            settings=settings,
            config=config,
            clock=self._clock,
            execution_repository=execution_repository,
            broker_factory=broker_factory,
            root=root,
        )
        self._reservations = reservation_service or ReservationService(
            settings=settings,
            config=config,
            clock=self._clock,
            execution_repository=execution_repository,
            root=root,
        )
        self._repository = reconciliation_repository or FilesystemReconciliationRepository(
            data_root / "reconciliation"
        )
        self._execution_repository = execution_repository
        self._engine = ReconciliationEngine(config.reconciliation)

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> ReconciliationRepository:
        return self._repository

    @property
    def positions(self) -> PositionService:
        return self._positions

    @property
    def reservations(self) -> ReservationService:
        return self._reservations

    @property
    def engine(self) -> ReconciliationEngine:
        return self._engine

    @property
    def enabled(self) -> bool:
        return self._config.reconciliation.enabled

    def latest(self) -> ReconciliationResult | None:
        return self._repository.latest()

    def get(self, reconciliation_id: str) -> ReconciliationResult | None:
        return self._repository.get(reconciliation_id)

    def history(self, limit: int | None = None) -> list[ReconciliationHistoryEntry]:
        return self._repository.history(limit=limit)

    # --- the run -----------------------------------------------------------
    @traced(
        "reconciliation.run",
        count=_metrics.RECONCILIATION_RUNS_TOTAL,
        duration=_metrics.RECONCILIATION_DURATION,
        result_attributes=lambda run: {
            TRADING_RECONCILIATION_ID: run.result.reconciliation_id,
            TRADING_STATUS: run.result.status.value,
        },
        labels=lambda run: {"status": run.result.status.value},
    )
    def run(
        self,
        *,
        broker: Broker | None = None,
        as_of: datetime | None = None,
        dry_run: bool = False,
        state: BrokerState | None = None,
    ) -> ReconciliationRun:
        """Read the broker once, compare everything, and record what was found.

        ``dry_run`` computes the whole comparison and writes nothing at all: no
        snapshot, no fill, no execution resolution, no reservation movement and
        no result. It still opens a broker connection, because there is nothing
        to compare without one — but that connection is read-only and the run
        submits zero orders either way.
        """
        started = time.perf_counter()
        now = as_of or self._clock.now()
        policy = self._config.reconciliation

        broker_state = state or self._positions.read_broker_state(broker=broker)
        capture = self._positions.capture(
            state=broker_state, as_of=now, store=not dry_run, record_fills=not dry_run
        )
        account_snapshot = self._account_snapshot(broker_state, at=now, store=not dry_run)

        # Ambiguous submissions first: a submission the broker turns out to
        # have filled must consume its capital in the same run that learned so.
        resolutions = self._resolve_unknowns(
            broker_state, capture=capture, at=now, apply=not dry_run
        )

        self._reservations.sync(at=now)
        executions = self._execution_records()
        updates = self._reservations.apply_executions(
            executions=executions,
            at=now,
            source="reconciliation",
            dry_run=dry_run,
        )

        projection = self._positions.expected(
            as_of=now, snapshot=capture.snapshot, executions=self._filled(executions)
        )
        broker_fills = self._broker_fills(capture)

        inputs = ReconciliationInputs(
            campaign_id=self._config.campaign.campaign_id,
            broker=broker_state.broker,
            account_reference=capture.snapshot.account_reference,
            trading_mode=self._settings.trading_mode,
            as_of=now,
            observed_at=capture.snapshot.observed_at,
            snapshot=capture.snapshot,
            orders=tuple(broker_state.orders),
            broker_fills=tuple(broker_fills),
            account_read=broker_state.account_status,
            orders_read=broker_state.orders_status,
            fills_read=broker_state.executions_status,
            read_detail=broker_state.detail,
            expected=tuple(projection.positions),
            structures=tuple(projection.strategies),
            executions=tuple(executions),
            reservations=tuple((update.reservation, update.outcome) for update in updates),
            unknown_resolutions=tuple(resolutions),
            account_snapshot_id=account_snapshot.snapshot_id if account_snapshot else None,
            config_version=self._config.application.config_version,
        )
        result = self._engine.reconcile(inputs)

        stored = False
        is_new = False
        if not dry_run and policy.enabled:
            _, is_new = self._repository.save(result)
            stored = True
            self._record_events(result, updates=updates, resolutions=resolutions, at=now)

        _logger.info(
            "reconciliation.run",
            reconciliation_id=result.reconciliation_id,
            status=result.status.value,
            findings=len(result.findings),
            critical=result.counts.critical,
            orders_submitted=capture.orders_submitted,
            dry_run=dry_run,
        )
        return ReconciliationRun(
            result=result,
            capture=capture,
            projection=projection,
            updates=tuple(updates),
            resolutions=tuple(resolutions),
            stored=stored,
            is_new=is_new,
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )

    # --- internals ---------------------------------------------------------
    def _resolve_unknowns(
        self,
        state: BrokerState,
        *,
        capture: PositionCapture,
        at: datetime,
        apply: bool,
    ) -> list[UnknownResolution]:
        """Settle ambiguous submissions from broker evidence, and record the answers.

        Appends the broker's answer to the execution's own history — the same
        thing ``execution explain --resolve`` does, from the same evidence.
        Nothing here submits, cancels or retries: an ``UNKNOWN`` that the broker
        cannot settle stays ``UNKNOWN``.
        """
        policy = self._config.reconciliation
        if not policy.resolve_unknown_executions:
            return []

        repository = self._executions()
        resolutions: list[UnknownResolution] = []
        for record in self._execution_records():
            if record.state is not ExecutionState.UNKNOWN:
                continue
            sequence = len(repository.events(record.execution_id))
            resolution = resolve_unknown(
                record,
                orders=state.orders,
                fills=state.executions,
                orders_readable=state.orders_status.usable,
                fills_readable=state.executions_status.usable,
                fills_are_complete_history=policy.treat_broker_fills_as_complete_history,
                observed_at=capture.snapshot.observed_at,
                sequence=sequence,
                source=state.broker,
            )
            resolutions.append(resolution)
            if apply and resolution.event is not None:
                repository.append_event(resolution.event)
        return resolutions

    def _account_snapshot(
        self, state: BrokerState, *, at: datetime, store: bool
    ) -> AccountSnapshot | None:
        """Capture the account through the Milestone 7 artifact, not a new one.

        ``AccountSnapshot`` already means "what the broker said about the
        account at one instant", is already immutable and content-addressed,
        and is already what the risk engine reads. A second account model here
        would be a competing copy of the same fact.
        """
        if state.account is None or not state.account_status.usable:
            return None
        from trading_system.risk.account import build_account_snapshot
        from trading_system.risk.store import FilesystemAccountSnapshotRepository

        # Captured *after* the read, never before the instant it describes: the
        # broker stamps its own clock on the account, and a snapshot claiming to
        # have been taken earlier than the state it reports is refused by the
        # model — correctly, since that would be a claim to have seen the future.
        captured_at = max(self._clock.now(), state.account.as_of, at)
        snapshot = build_account_snapshot(
            state.account,
            list(state.positions),
            broker=state.broker,
            trading_mode=self._settings.trading_mode,
            captured_at=captured_at,
            orders_submitted=state.orders_submitted,
            read_only=state.read_only,
            simulated=state.broker.upper() in {"SIMULATOR", "SIMULATED"},
        )
        if store:
            FilesystemAccountSnapshotRepository(self._data_root / "accounts").save(snapshot)
        return snapshot

    def _broker_fills(self, capture: PositionCapture) -> list[ObservedFill]:
        """Every fill this capture saw, whether newly recorded or already known.

        Both matter to a comparison: a fill recorded on an earlier run is still
        a fill the broker is reporting now, and leaving it out would make the
        second reconciliation of unchanged state look different from the first.
        """
        return [*capture.recorded_fills, *capture.reobserved_fills]

    def _record_events(
        self,
        result: ReconciliationResult,
        *,
        updates: Sequence[ReservationUpdate],
        resolutions: Sequence[UnknownResolution],
        at: datetime,
    ) -> None:
        """Append this run's own history, in the order things happened."""
        sequence = len(self._repository.events(result.reconciliation_id))

        def append(
            event_type: ReconciliationEventType,
            *,
            detail: str,
            reservation_id: str | None = None,
            execution_id: str | None = None,
        ) -> None:
            nonlocal sequence
            self._repository.append_event(
                ReconciliationEvent(
                    event_id=reconciliation_event_identifier(
                        reconciliation_id=result.reconciliation_id,
                        sequence=sequence,
                        event_type=event_type.value,
                    ),
                    reconciliation_id=result.reconciliation_id,
                    sequence=sequence,
                    event_type=event_type,
                    occurred_at=at,
                    observed_at=at,
                    source="reconciliation",
                    detail=detail,
                    reservation_id=reservation_id,
                    execution_id=execution_id,
                )
            )
            sequence += 1

        append(
            ReconciliationEventType.RECONCILIATION_STARTED,
            detail=f"comparing against {result.broker} for account {result.account_reference}",
        )
        if result.positions_read.usable:
            append(
                ReconciliationEventType.BROKER_SNAPSHOT_CAPTURED,
                detail=(
                    f"{result.broker_position_count} broker position(s) in snapshot "
                    f"{result.position_snapshot_id}"
                ),
            )
        else:
            append(
                ReconciliationEventType.BROKER_READ_FAILED,
                detail=f"broker positions read as {result.positions_read.value}",
            )
        append(
            ReconciliationEventType.INTERNAL_LEDGER_READ,
            detail=(
                f"{result.expected_position_count} expected position(s), "
                f"{len(result.execution_ids)} execution(s), "
                f"{len(result.reservation_ids)} reservation(s)"
            ),
        )
        for resolution in resolutions:
            if resolution.resolved:
                append(
                    ReconciliationEventType.EXECUTION_RESOLVED,
                    detail=resolution.detail,
                    execution_id=resolution.execution_id,
                )
        for update in updates:
            if not update.applied:
                continue
            reservation = update.reservation
            if reservation.released_amount > 0:
                append(
                    ReconciliationEventType.RESERVATION_RELEASED,
                    detail=f"{reservation.released_amount} {reservation.currency} released",
                    reservation_id=reservation.reservation_id,
                )
            elif reservation.locked_by_uncertainty:
                append(
                    ReconciliationEventType.RESERVATION_RETAINED,
                    detail=update.outcome.detail,
                    reservation_id=reservation.reservation_id,
                )
            elif reservation.consumed_amount > 0:
                append(
                    ReconciliationEventType.RESERVATION_CONSUMED,
                    detail=f"{reservation.consumed_amount} {reservation.currency} consumed",
                    reservation_id=reservation.reservation_id,
                )
        append(
            ReconciliationEventType.RECONCILIATION_COMPLETED,
            detail=(
                f"{result.status.value}: {result.counts.mismatches} discrepancy(ies), "
                f"{result.counts.critical} critical, 0 corrective orders"
            ),
        )

    def _executions(self) -> ExecutionRepository:
        repository = self._execution_repository
        if repository is None:
            from trading_system.execution.store import FilesystemExecutionRepository

            repository = FilesystemExecutionRepository(self._data_root / "execution")
            self._execution_repository = repository
        return repository

    def _execution_records(self) -> list[ExecutionRecord]:
        repository = self._executions()
        records = [
            record
            for entry in repository.history()
            if (record := repository.current(entry.execution_id)) is not None
        ]
        return sorted(records, key=lambda record: record.created_at)

    def _filled(self, records: Sequence[ExecutionRecord]) -> list[ExecutionRecord]:
        """Executions that establish a position. ``UNKNOWN`` is deliberately absent."""
        return [
            record
            for record in records
            if record.filled_quantity > 0
            and record.state
            in (
                ExecutionState.FILLED,
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.CANCELLED,
                ExecutionState.EXPIRED,
            )
        ]


def blocks_new_executions(result: ReconciliationResult | None) -> bool:
    """Whether the last reconciliation permits opening a new position.

    ``None`` blocks: never having reconciled is not the same as having
    reconciled cleanly, and an unknown broker state is not a safe state to open
    a position from. This is the Milestone 1 rule, restated where a scheduler
    will reach for it.
    """
    if result is None:
        return True
    return result.status is not ReconciliationRunStatus.MATCH
