"""The profit-and-loss service: the composition root for Milestone 11's money.

.. code-block:: text

    Milestone 10 lifecycle       -> which positions are CONFIRMED CLOSED
          |
    Milestone 9 recorded fills   -> what actually traded, on both sides
          |
    PnLCalculator                -> pure; COMPLETE / PARTIAL / NOT_AVAILABLE
          |
    immutable RealizedPnL
          |
    settlement decision          -> pure; capital returns only on proof
          |
    Milestone 9 reservation ledger  <- ONE capital ledger, extended
          |
    DailyPnL                     -> what the risk engine reads next time

Properties this service holds regardless of which path it takes:

* **It holds no broker.** There is no connection, no writable factory and no
  import anywhere in this package that reaches one; a test walks the transitive
  graph and asserts it. Whether a position is closed is read from Milestone 10's
  lifecycle, which read it from Milestone 9's snapshot, which read it from a
  read-only connection.
* **It consults no model.** What a trade made is arithmetic over fills.
* **It creates no second ledger.** Capital moves as an appended event on the
  Milestone 9 reservation, folded on read exactly as every other reservation
  event is. A second set of balances would be a second copy of the truth, and
  when two copies disagree there is no way to tell which is wrong.
* **It writes nothing upstream.** Executions, fills, positions, lifecycles and
  reconciliations are read; none is edited, ever.
* **It is safe to run repeatedly.** Every id is content-derived, the store
  recognises a re-observation, and settlement outcomes are deltas — so the
  second run over unchanged evidence returns no capital, records no new
  movement, and stores no second copy of one fact.
* **It refuses rather than estimates.** Every path that cannot produce a figure
  produces ``NOT_AVAILABLE`` with a reason, and a blocked settlement records
  the evidence that was missing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from trading_system import __version__ as application_version
from trading_system.domain.enums import (
    CommissionStatus,
    DailyPnLStatus,
    ExecutionIntent,
    ExecutionState,
    PositionLifecycleState,
    ReconciliationSeverity,
    SettlementStatus,
    StrategyType,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import (
    TRADING_PNL_ID,
    TRADING_POSITION_ID,
    TRADING_STATUS,
)
from trading_system.observability.instrument import traced
from trading_system.pnl.calculator import PnLCalculator, PnLInputs, session_date_of
from trading_system.pnl.models import (
    PNL_SCHEMA_VERSION,
    DailyPnL,
    PnLRunResult,
    RealizedPnL,
    ReservationSettlement,
    daily_pnl_identifier,
)
from trading_system.pnl.settlement import (
    SettlementInputs,
    build_settlement,
    settle,
    settlement_event,
)
from trading_system.pnl.store import FilesystemPnLRepository, PnLRepository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.execution.models import ExecutionRecord
    from trading_system.execution.store import ExecutionRepository
    from trading_system.exit.models import PositionLifecycleSnapshot
    from trading_system.exit.store import ExitRepository
    from trading_system.positions.models import BrokerPositionSnapshot, ObservedFill
    from trading_system.positions.store import FillRepository, PositionRepository
    from trading_system.reconciliation.store import ReconciliationRepository
    from trading_system.reservations.service import ReservationService

__all__ = ["ClosedPosition", "PnLRun", "PnLService", "SettlementOutcomeRecord"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    """One position Milestone 10 says is confirmed closed, and its evidence.

    Everything here is *read*. The lifecycle came from the exit ledger, which
    only reaches ``CLOSED`` when a broker snapshot reported none of the
    structure; the executions and fills came from the Milestone 8 and 9
    ledgers. Nothing in this dataclass was inferred.
    """

    position_id: str
    lifecycle: PositionLifecycleSnapshot
    entry: ExecutionRecord | None
    exits: tuple[ExecutionRecord, ...]
    entry_fills: tuple[ObservedFill, ...]
    exit_fills: tuple[ObservedFill, ...]
    execution_unknown: bool
    closure_confirmed: bool
    broker_read_usable: bool

    @property
    def underlying(self) -> str:
        return self.entry.underlying if self.entry is not None else "UNKNOWN"

    @property
    def strategy(self) -> StrategyType:
        # ``entry`` is always an OPEN execution, whose strategy the record
        # model requires; only a CLEANUP has none, and a cleanup never appears
        # here because it belongs to no position lifecycle.
        if self.entry is not None and self.entry.strategy is not None:
            return self.entry.strategy
        return StrategyType.LONG_CALL


@dataclass(frozen=True, slots=True)
class SettlementOutcomeRecord:
    """One settlement evaluation, and whether the ledger actually moved."""

    settlement: ReservationSettlement
    applied: bool
    stored: bool


@dataclass(frozen=True, slots=True)
class PnLRun:
    """What one settlement pass computed, moved and refused to move."""

    result: PnLRunResult
    results: tuple[RealizedPnL, ...] = ()
    settlements: tuple[SettlementOutcomeRecord, ...] = ()
    daily: DailyPnL | None = None
    stored: bool = False

    @property
    def orders_submitted(self) -> int:
        """Structurally zero. There is no broker in this package to submit one."""
        return 0

    @property
    def capital_returned(self) -> Decimal:
        return self.result.capital_returned


class PnLService:
    """Computes realised results and settles the capital behind them."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        pnl_repository: PnLRepository | None = None,
        exit_repository: ExitRepository | None = None,
        execution_repository: ExecutionRepository | None = None,
        fill_repository: FillRepository | None = None,
        position_repository: PositionRepository | None = None,
        reconciliation_repository: ReconciliationRepository | None = None,
        reservation_service: ReservationService | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._pnl = pnl_repository or FilesystemPnLRepository(data_root / "pnl")
        self._exit_repository = exit_repository
        self._execution_repository = execution_repository
        self._fill_repository = fill_repository
        self._position_repository = position_repository
        self._reconciliation_repository = reconciliation_repository
        self._reservations = reservation_service

        self._calculator = PnLCalculator(
            currency_precision=config.pnl.currency.precision,
            day_boundary_timezone=config.pnl.day_boundary_timezone,
        )

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> PnLRepository:
        return self._pnl

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def campaign_id(self) -> str:
        return self._config.campaign.campaign_id

    @property
    def enabled(self) -> bool:
        return self._config.pnl.enabled

    @property
    def calculator(self) -> PnLCalculator:
        return self._calculator

    def versions(self) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            strategy_spec_version=PNL_SCHEMA_VERSION,
        )

    def history(self, limit: int | None = None):  # type: ignore[no-untyped-def]
        return self._pnl.history(limit=limit)

    def get(self, pnl_id: str) -> RealizedPnL | None:
        return self._pnl.get(pnl_id)

    def for_position(self, position_id: str) -> list[RealizedPnL]:
        return self._pnl.for_position(position_id)

    def daily(self, session_date: date) -> DailyPnL | None:
        return self._pnl.daily(session_date)

    def daily_history(self, limit: int | None = None) -> list[DailyPnL]:
        return self._pnl.daily_history(limit=limit)

    def settlements_for(self, reservation_id: str) -> list[ReservationSettlement]:
        return self._pnl.settlements_for(reservation_id)

    # --- assembling what closed -------------------------------------------
    def closed_positions(self, *, as_of: datetime | None = None) -> list[ClosedPosition]:
        """Every position Milestone 10's lifecycle records as confirmed closed.

        Read from the *lifecycle*, not from an absence in a broker snapshot.
        A position missing from a snapshot might be a position the broker could
        not report; ``CLOSED`` in the lifecycle means an actual snapshot said
        the account holds none of the structure — a claim Milestone 10 makes
        only on broker reality.
        """
        exits = self._exits()
        if exits is None:
            return []
        executions = {record.execution_id: record for record in self._execution_records()}
        fills_by_execution = self._fills_by_execution()

        closed: list[ClosedPosition] = []
        for lifecycle in exits.all_lifecycles():
            if lifecycle.state is not PositionLifecycleState.CLOSED:
                continue
            entry = executions.get(lifecycle.entry_execution_id or "")
            if entry is None:
                entry = next(
                    (
                        record
                        for record in executions.values()
                        if record.intent is ExecutionIntent.OPEN
                        and record.position_id == lifecycle.position_id
                    ),
                    None,
                )
            closing = tuple(
                record
                for record in executions.values()
                if record.intent is ExecutionIntent.CLOSE
                and record.position_id == lifecycle.position_id
            )
            unknown = any(
                record.state is ExecutionState.UNKNOWN
                for record in (*closing, *(record for record in (entry,) if record is not None))
            )
            closed.append(
                ClosedPosition(
                    position_id=lifecycle.position_id,
                    lifecycle=lifecycle,
                    entry=entry,
                    exits=closing,
                    entry_fills=tuple(
                        fills_by_execution.get(entry.execution_id, ()) if entry else ()
                    ),
                    exit_fills=tuple(
                        fill
                        for record in closing
                        for fill in fills_by_execution.get(record.execution_id, ())
                    ),
                    execution_unknown=unknown,
                    # The lifecycle reached CLOSED because a broker snapshot
                    # reported none of the structure — Milestone 10 permits no
                    # other way in, and CLOSED is terminal. Both flags are
                    # therefore true *by construction* here, and neither is
                    # re-derived from the current snapshot: requiring a
                    # freshly-readable broker would mean a position confirmed
                    # closed this morning could not settle this afternoon
                    # because the gateway happens to be down, which is a
                    # different fact about a different thing.
                    #
                    # The flags remain on SettlementInputs because the pure
                    # settlement function must still refuse a caller that
                    # supplies weaker evidence, and tests/pnl/test_settlement.py
                    # exercises exactly that.
                    closure_confirmed=True,
                    broker_read_usable=True,
                )
            )
        return sorted(closed, key=lambda item: item.position_id)

    # --- operation: pnl.compute -------------------------------------------
    @traced(
        "pnl.compute",
        attributes=lambda self, position, **kwargs: {
            TRADING_POSITION_ID: position.position_id,
        },
        count=_metrics.PNL_RESULTS_TOTAL,
        duration=_metrics.PNL_DURATION,
        result_attributes=lambda record: {
            TRADING_PNL_ID: record.pnl_id,
            TRADING_STATUS: record.status.value,
        },
        labels=lambda record: {
            "status": record.status.value,
            "strategy": record.strategy.value,
        },
    )
    def compute(self, position: ClosedPosition, *, store: bool = True) -> RealizedPnL:
        """The realised result for one closed structure. Pure, then stored."""
        now = self._clock.now()
        entry = position.entry
        realized = self._calculator.compute(
            PnLInputs(
                position_id=position.position_id,
                campaign_id=(entry.campaign_id if entry else self.campaign_id),
                underlying=position.underlying,
                strategy=position.strategy,
                entry_fills=list(position.entry_fills),
                exit_fills=list(position.exit_fills),
                computed_at=now,
                day_boundary_timezone=self._config.pnl.day_boundary_timezone,
                currency_precision=self._config.pnl.currency.precision,
                require_commission_for_net=self._config.pnl.require_commission_for_net,
                execution_unknown=position.execution_unknown,
                position_closed_at=position.lifecycle.closed_at or position.lifecycle.updated_at,
                entry_execution_id=entry.execution_id if entry else None,
                exit_execution_ids=[record.execution_id for record in position.exits],
                allocation_id=entry.allocation_id if entry else None,
                opportunity_id=entry.opportunity_id if entry else None,
                research_report_id=entry.research_report_id if entry else None,
                contract_selection_id=entry.contract_selection_id if entry else None,
                account_reference=self._account_reference(position),
                broker_source=entry.broker if entry else "UNKNOWN",
                versions=self.versions(),
            )
        )
        if store:
            self._pnl.save(realized)
        _logger.info(
            "pnl.compute",
            position_id=position.position_id,
            status=realized.status.value,
            reason=[code.value for code in realized.reason_codes],
            realized=(
                str(realized.best_available_pnl)
                if realized.best_available_pnl is not None
                else None
            ),
            currency=realized.currency,
        )
        return realized

    # --- operation: pnl.settle --------------------------------------------
    @traced(
        "pnl.settle",
        attributes=lambda self, position, realized, **kwargs: {
            TRADING_POSITION_ID: position.position_id,
            TRADING_PNL_ID: realized.pnl_id,
        },
        count=_metrics.SETTLEMENTS_TOTAL,
        labels=lambda record: (
            {"status": record.settlement.status.value} if record is not None else {}
        ),
    )
    def settle_position(
        self, position: ClosedPosition, realized: RealizedPnL, *, dry_run: bool = False
    ) -> SettlementOutcomeRecord | None:
        """Return this position's committed capital, if the evidence permits.

        ``None`` when there is no reservation to settle — a position opened
        outside the allocation path, or one whose authorisation this campaign
        does not own. Recorded nowhere rather than as a refusal: there is
        nothing here that could have moved.
        """
        reservations = self._reservation_service()
        allocation_id = position.entry.allocation_id if position.entry else None
        reservation = (
            reservations.for_allocation(allocation_id) if allocation_id is not None else None
        )
        if reservation is None:
            return None

        now = self._clock.now()
        outcome = settle(
            SettlementInputs(
                reservation=reservation,
                position_id=position.position_id,
                closure_confirmed=position.closure_confirmed,
                broker_read_usable=position.broker_read_usable,
                execution_unknown=position.execution_unknown,
                realized=realized,
                reconciliation_findings=self._critical_findings(position),
                reconciliation_id=self._latest_reconciliation_id(),
                policy=self._config.pnl.settlement,
            )
        )
        settlement = build_settlement(
            reservation,
            outcome,
            position_id=position.position_id,
            pnl_id=realized.pnl_id if realized.available else None,
            settled_at=now,
            reconciliation_id=self._latest_reconciliation_id(),
        )
        if outcome.status is SettlementStatus.ALREADY_SETTLED or (
            outcome.status is SettlementStatus.NOT_APPLICABLE
        ):
            return SettlementOutcomeRecord(settlement=settlement, applied=False, stored=False)

        if dry_run:
            return SettlementOutcomeRecord(settlement=settlement, applied=False, stored=False)

        _, stored = self._pnl.save_settlement(settlement)
        applied = False
        if outcome.moved:
            event = settlement_event(
                reservation,
                outcome,
                settlement,
                sequence=len(reservations.repository.events(reservation.reservation_id)),
                occurred_at=now,
                observed_at=now,
            )
            applied = reservations.repository.append_event(event)

        _logger.info(
            "pnl.settle",
            position_id=position.position_id,
            reservation_id=reservation.reservation_id,
            status=settlement.status.value,
            block_reason=(
                settlement.block_reason.value if settlement.block_reason is not None else None
            ),
            settled=str(settlement.settled_amount),
            applied=applied,
        )
        return SettlementOutcomeRecord(settlement=settlement, applied=applied, stored=stored)

    # --- operation: pnl.run ------------------------------------------------
    def run(self, *, as_of: datetime | None = None, dry_run: bool = False) -> PnLRun:
        """Compute every outstanding result and settle what the evidence permits.

        Safe to run repeatedly and safe to run from a scheduler. Nothing lives
        in process memory: results are content-addressed, settlement outcomes
        are deltas, and the reservation ledger recognises a replayed event — so
        the second run over unchanged state returns no capital and records no
        second copy of one fact.
        """
        now = as_of or self._clock.now()
        positions = self.closed_positions(as_of=now)

        results: list[RealizedPnL] = []
        settlements: list[SettlementOutcomeRecord] = []
        for position in positions:
            realized = self.compute(position, store=not dry_run)
            results.append(realized)
            outcome = self.settle_position(position, realized, dry_run=dry_run)
            if outcome is not None:
                settlements.append(outcome)

        daily = self.daily_rollup(as_of=now, store=not dry_run)
        returned = sum(
            (record.settlement.settled_amount for record in settlements if record.applied),
            Decimal("0"),
        )
        result = PnLRunResult(
            run_id=self._run_identifier(now, results, settlements, dry_run=dry_run),
            campaign_id=self.campaign_id,
            as_of=now,
            generated_at=self._clock.now(),
            dry_run=dry_run,
            positions_examined=len(positions),
            results_computed=sum(1 for record in results if record.available),
            results_unavailable=sum(1 for record in results if not record.available),
            settlements_applied=sum(1 for record in settlements if record.applied),
            settlements_blocked=sum(
                1 for record in settlements if record.settlement.status is SettlementStatus.BLOCKED
            ),
            capital_returned=returned,
            currency=self._config.campaign.target_currency,
            pnl_ids=[record.pnl_id for record in results],
            settlement_ids=[record.settlement.settlement_id for record in settlements],
            daily_pnl_id=daily.daily_pnl_id if daily is not None else None,
            versions=self.versions(),
        )
        stored = False
        if not dry_run:
            self._pnl.save_run(result)
            stored = True

        _logger.info(
            "pnl.run",
            positions=len(positions),
            computed=result.results_computed,
            unavailable=result.results_unavailable,
            settled=result.settlements_applied,
            blocked=result.settlements_blocked,
            capital_returned=str(returned),
            orders_submitted=0,
        )
        return PnLRun(
            result=result,
            results=tuple(results),
            settlements=tuple(settlements),
            daily=daily,
            stored=stored,
        )

    # --- operation: pnl.daily ----------------------------------------------
    def daily_rollup(
        self,
        *,
        as_of: datetime | None = None,
        session: date | None = None,
        store: bool = True,
    ) -> DailyPnL | None:
        """One exchange-local day's realised result, and how reliable it is.

        The figure the daily loss limit is evaluated against. ``UNKNOWN`` is
        the state that matters: positions closed today and at least one
        produced no usable figure, which is emphatically not "no losses today".
        """
        now = as_of or self._clock.now()
        timezone = self._config.pnl.day_boundary_timezone
        session_date = session or session_date_of(now, timezone)

        records = [
            record
            for record in self._pnl.all()
            if record.session_date == session_date and record.campaign_id == self.campaign_id
        ]
        # One result per position: a position that closed in two tranches has
        # two records and both count, but a recomputation of the same tranche
        # is one fact seen twice.
        deduplicated: dict[str, RealizedPnL] = {}
        for record in sorted(records, key=lambda item: item.computed_at):
            deduplicated[record.pnl_id] = record
        results = sorted(deduplicated.values(), key=lambda item: item.pnl_id)
        if not results:
            return None

        usable = [record for record in results if record.available]
        unusable = [record for record in results if not record.available]
        status = DailyPnLStatus.TRACKED if not unusable else DailyPnLStatus.UNKNOWN

        total: Decimal | None = None
        gross: Decimal | None = None
        commission: Decimal | None = None
        commission_status = CommissionStatus.NOT_AVAILABLE
        loss: Decimal | None = None
        if status is DailyPnLStatus.TRACKED:
            figures = [record.best_available_pnl for record in usable]
            if any(figure is None for figure in figures):  # pragma: no cover - defensive
                status = DailyPnLStatus.UNKNOWN
                unusable = usable
            else:
                total = sum((figure for figure in figures if figure is not None), Decimal("0"))
                gross = sum(
                    (record.realized_gross_pnl or Decimal("0") for record in usable),
                    Decimal("0"),
                )
                if all(record.commission_status is CommissionStatus.KNOWN for record in usable):
                    commission_status = CommissionStatus.KNOWN
                    commission = sum(
                        (record.total_commission or Decimal("0") for record in usable),
                        Decimal("0"),
                    )
                elif any(record.commission_status is CommissionStatus.KNOWN for record in usable):
                    commission_status = CommissionStatus.PARTIAL
                loss = -total if total < 0 else Decimal("0")

        daily = DailyPnL(
            daily_pnl_id=daily_pnl_identifier(
                campaign_id=self.campaign_id,
                session_date=session_date,
                pnl_ids=[record.pnl_id for record in results],
                status=status.value,
            ),
            campaign_id=self.campaign_id,
            session_date=session_date,
            timezone=timezone,
            status=status,
            currency=self._config.campaign.target_currency,
            realized_pnl=total,
            realized_gross_pnl=gross,
            total_commission=commission,
            commission_status=commission_status,
            realized_loss=loss,
            positions_closed=len(results),
            positions_with_result=len(results) - len(unusable),
            positions_without_result=len(unusable),
            pnl_ids=[record.pnl_id for record in results],
            unavailable_position_ids=sorted({record.position_id for record in unusable}),
            computed_at=self._clock.now(),
            detail=(
                "every position that closed today produced a usable figure"
                if status is DailyPnLStatus.TRACKED
                else (
                    "at least one position that closed today produced no usable figure, so the "
                    "day's total is unknown. This is not zero loss: it is an absence of "
                    "knowledge about a day on which money moved"
                )
            ),
        )
        if store:
            self._pnl.save_daily(daily)
        return daily

    # --- internals ---------------------------------------------------------
    def _run_identifier(
        self,
        as_of: datetime,
        results: Sequence[RealizedPnL],
        settlements: Sequence[SettlementOutcomeRecord],
        *,
        dry_run: bool,
    ) -> str:
        """Derive a run's identity from what it *concluded*, not only its inputs.

        The settlement outcomes are in the digest, and that is what makes this
        correct rather than merely unique. The same closed position settled
        once and then found already-settled is two genuinely different runs
        reaching different answers — the first returned capital, the second
        moved nothing — and an id derived from the inputs alone would collide
        them, whereupon the immutable store would refuse to write the second.

        Exactly the lesson ``allocation`` records about including the
        campaign's committed state and ``execution`` about the ledger's, for
        the same reason. An unchanged re-run still lands on the same id, which
        is what makes it idempotent rather than a second record of one event.
        """
        from trading_system.data.hashing import stable_hash

        digest = stable_hash(
            [
                "PNL_RUN",
                PNL_SCHEMA_VERSION,
                self.campaign_id,
                as_of.isoformat(),
                sorted(record.pnl_id for record in results),
                sorted(record.settlement.settlement_id for record in settlements),
                self._config.application.config_version,
                dry_run,
            ]
        )
        return f"pnlrun-{digest[:20]}"

    def _account_reference(self, position: ClosedPosition) -> str | None:
        fills = (*position.entry_fills, *position.exit_fills)
        return fills[0].account_reference if fills else None

    def _exits(self) -> ExitRepository | None:
        if self._exit_repository is None:
            from trading_system.exit.store import FilesystemExitRepository

            self._exit_repository = FilesystemExitRepository(self._data_root / "exit")
        return self._exit_repository

    def _executions(self) -> ExecutionRepository:
        if self._execution_repository is None:
            from trading_system.execution.store import FilesystemExecutionRepository

            self._execution_repository = FilesystemExecutionRepository(
                self._data_root / "execution"
            )
        return self._execution_repository

    def _fills(self) -> FillRepository:
        if self._fill_repository is None:
            from trading_system.positions.store import FilesystemFillRepository

            self._fill_repository = FilesystemFillRepository(self._data_root / "fills")
        return self._fill_repository

    def _positions(self) -> PositionRepository:
        if self._position_repository is None:
            from trading_system.positions.store import FilesystemPositionRepository

            self._position_repository = FilesystemPositionRepository(self._data_root / "positions")
        return self._position_repository

    def _reservation_service(self) -> ReservationService:
        if self._reservations is None:
            from trading_system.reservations.service import ReservationService as _Service

            self._reservations = _Service(
                settings=self._settings,
                config=self._config,
                clock=self._clock,
                root=self._data_root.parent,
            )
        return self._reservations

    def _execution_records(self) -> list[ExecutionRecord]:
        repository = self._executions()
        return [
            record
            for entry in repository.history()
            if (record := repository.current(entry.execution_id)) is not None
        ]

    def _fills_by_execution(self) -> dict[str, list[ObservedFill]]:
        grouped: dict[str, list[ObservedFill]] = {}
        for fill in self._fills().all():
            if fill.execution_id is None:
                # A fill with no execution of ours behind it is real and is
                # recorded, but it belongs to no trade this system authorised.
                # Counting it would let an orphan position produce a result
                # attributed to a campaign that never opened it.
                continue
            grouped.setdefault(fill.execution_id, []).append(fill)
        return grouped

    def _latest_snapshot(self) -> BrokerPositionSnapshot | None:
        return self._positions().latest_usable()

    def _reconciliation(self) -> ReconciliationRepository | None:
        if self._reconciliation_repository is None:
            from trading_system.reconciliation.store import (
                FilesystemReconciliationRepository,
            )

            self._reconciliation_repository = FilesystemReconciliationRepository(
                self._data_root / "reconciliation"
            )
        return self._reconciliation_repository

    def _latest_reconciliation_id(self) -> str | None:
        repository = self._reconciliation()
        if repository is None:
            return None
        latest = repository.latest()
        return latest.reconciliation_id if latest is not None else None

    def _critical_findings(self, position: ClosedPosition) -> list[str]:
        """Critical reconciliation findings touching this position.

        Only ``CRITICAL`` counts. A warning about a position we do not own, or
        an informational note about an orphan somewhere else in the account, is
        not a reason to hold this campaign's capital hostage — but a critical
        disagreement about what is actually held is.
        """
        repository = self._reconciliation()
        if repository is None:
            return []
        latest = repository.latest()
        if latest is None:
            return []

        records = [record for record in (position.entry, *position.exits) if record is not None]
        contract_ids = {leg.contract_id for record in records for leg in record.legs}
        execution_ids = {record.execution_id for record in records}
        allocation_ids = {record.allocation_id for record in records}

        findings: list[str] = []
        for finding in latest.findings:
            if finding.severity is not ReconciliationSeverity.CRITICAL:
                continue
            if (
                (finding.contract_id is not None and finding.contract_id in contract_ids)
                or (finding.execution_id is not None and finding.execution_id in execution_ids)
                or (finding.allocation_id is not None and finding.allocation_id in allocation_ids)
                or finding.identifier == position.position_id
            ):
                findings.append(finding.finding_type.value)
        return sorted(set(findings))
