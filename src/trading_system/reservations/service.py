"""The reservation service: the composition root for campaign capital.

.. code-block:: text

    allocation ledger (M7)      -> one Reservation per APPROVED authorisation
    execution ledger (M8)       -> what actually happened to each one
    deterministic lifecycle     -> consume / release / hold, on evidence only
    immutable reservation ledger

Properties this service holds regardless of which path it takes:

* **It holds no broker.** There is no connection, no factory and no import
  anywhere in this package that reaches one, and a test asserts it. Capital
  moves on the evidence the execution ledger already recorded; a service that
  could ask the broker itself would be a second, competing source of truth.
* **It consults no model.** No LLM client, no prompt, no agent. How much of the
  campaign is committed is arithmetic.
* **It writes nothing upstream.** The allocation ledger and the execution
  ledger are read. Neither is edited, ever — a reservation that disagreed with
  its authorisation would be resolved by changing this ledger, not that one.
* **It is idempotent.** Applying the same execution state twice produces the
  same reservation: outcomes are deltas against current state, and a replayed
  event is recognised by id and recorded nowhere.

The one thing this service will not do, under any configuration, is release the
capital of an ``UNKNOWN`` execution.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from trading_system.domain.enums import (
    AllocationOutcome,
    ExecutionState,
    ReservationEventType,
    ReservationReasonCode,
    ReservationState,
)
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.reservations.lifecycle import ReservationOutcome, resolve_reservation
from trading_system.reservations.models import (
    CampaignCapital,
    Reservation,
    ReservationEvent,
    reservation_identifier,
)
from trading_system.reservations.store import (
    FilesystemReservationRepository,
    ReservationRepository,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.allocation.models import CampaignAllocation
    from trading_system.allocation.store import AllocationRepository
    from trading_system.execution.models import ExecutionRecord
    from trading_system.execution.store import ExecutionRepository

__all__ = ["ReservationService", "ReservationSync", "ReservationUpdate"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReservationSync:
    """What ``reservations`` learned from the allocation ledger."""

    created: tuple[Reservation, ...] = ()
    existing: tuple[Reservation, ...] = ()

    @property
    def total(self) -> int:
        return len(self.created) + len(self.existing)


@dataclass(frozen=True, slots=True)
class ReservationUpdate:
    """One reservation, the conclusion drawn about it, and what was recorded."""

    reservation: Reservation
    outcome: ReservationOutcome
    event: ReservationEvent | None = None

    @property
    def applied(self) -> bool:
        """Whether this update actually moved money."""
        return self.event is not None and self.outcome.changed


class ReservationService:
    """Builds reservations from authorisations and resolves them from executions."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        reservation_repository: ReservationRepository | None = None,
        allocation_repository: AllocationRepository | None = None,
        execution_repository: ExecutionRepository | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._reservations = reservation_repository or FilesystemReservationRepository(
            data_root / "reservations"
        )
        self._allocation_repository: AllocationRepository | None = allocation_repository
        self._execution_repository: ExecutionRepository | None = execution_repository

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> ReservationRepository:
        return self._reservations

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def campaign_id(self) -> str:
        return self._config.campaign.campaign_id

    def all(self) -> list[Reservation]:
        return self._reservations.all_current()

    def get(self, reservation_id: str) -> Reservation | None:
        return self._reservations.current(reservation_id)

    def for_allocation(self, allocation_id: str) -> Reservation | None:
        return self._reservations.for_allocation(allocation_id)

    # --- building ----------------------------------------------------------
    def sync(self, *, at: datetime | None = None) -> ReservationSync:
        """Ensure every approved authorisation has a reservation. Idempotent.

        Derives one reservation per authorisation, with an id derived from the
        authorisation itself — so replaying the allocation ledger produces the
        same reservation rather than a second one holding the same capital
        twice.

        Dry-run allocation runs are skipped. A diagnostic never reserves
        anything, which is what makes ``allocation run --dry-run`` safe to
        repeat.
        """
        now = at or self._clock.now()
        created: list[Reservation] = []
        existing: list[Reservation] = []

        for allocation in self._approved_allocations():
            reservation_id = reservation_identifier(
                campaign_id=allocation.campaign_id,
                allocation_id=allocation.allocation_id,
                opportunity_id=allocation.opportunity_id,
            )
            stored = self._reservations.current(reservation_id)
            if stored is not None:
                existing.append(stored)
                continue
            reservation = self._reservation_for(allocation, reservation_id, at=now)
            self._reservations.save(reservation)
            created.append(reservation)

        _logger.info(
            "reservations.sync",
            campaign=self.campaign_id,
            created=len(created),
            existing=len(existing),
        )
        return ReservationSync(created=tuple(created), existing=tuple(existing))

    def _reservation_for(
        self, allocation: CampaignAllocation, reservation_id: str, *, at: datetime
    ) -> Reservation:
        return Reservation(
            reservation_id=reservation_id,
            campaign_id=allocation.campaign_id,
            allocation_id=allocation.allocation_id,
            opportunity_id=allocation.opportunity_id,
            symbol=allocation.symbol,
            strategy=allocation.strategy,
            currency=allocation.currency or self._config.campaign.currency,
            authorized_amount=allocation.capital_committed,
            authorized_max_loss=allocation.total_max_loss,
            authorized_quantity=allocation.quantity,
            authorized_at=allocation.as_of,
            remaining_amount=allocation.capital_committed,
            state=ReservationState.RESERVED,
            reason_codes=[ReservationReasonCode.AUTHORIZED],
            created_at=at,
            updated_at=at,
            contract_selection_id=allocation.contract_selection_id,
            research_report_id=allocation.research_report_id,
            allocation_run_id=allocation.run_id,
            detail=(
                "authorised by Milestone 7 and not yet acted on. Committed, not invested: only "
                "a confirmed broker fill turns a reservation into a position"
            ),
        )

    # --- resolving ---------------------------------------------------------
    def apply_executions(
        self,
        *,
        executions: Sequence[ExecutionRecord] | None = None,
        reconciliation_id: str | None = None,
        at: datetime | None = None,
        source: str = "reconciliation",
        dry_run: bool = False,
    ) -> list[ReservationUpdate]:
        """Move each reservation to what the execution ledger now supports.

        ``dry_run`` computes every outcome and writes nothing, which is what
        ``reservations validate`` uses: an operator can see exactly what would
        move before anything does.
        """
        now = at or self._clock.now()
        records = list(executions if executions is not None else self._execution_records())
        by_allocation: dict[str, list[ExecutionRecord]] = {}
        for record in records:
            if not record.intent.establishes_position:
                # A Milestone 10 exit carries the same allocation id as the
                # entry it closes, and it must not resolve that authorisation's
                # reservation. A reservation answers "how much of the campaign
                # is committed", and an exit's fills are *proceeds*: consuming
                # the reservation a second time would double-count the money,
                # and releasing it would return capital to the campaign without
                # any realised profit and loss behind the figure — which is
                # Milestone 11's, not this one's.
                continue
            by_allocation.setdefault(record.allocation_id, []).append(record)

        updates: list[ReservationUpdate] = []
        for reservation in self._reservations.all_current():
            outcome = resolve_reservation(
                reservation,
                by_allocation.get(reservation.allocation_id, []),
                policy=self._config.reconciliation.reservations,
            )
            if not outcome.changed and outcome.state is reservation.state:
                updates.append(ReservationUpdate(reservation=reservation, outcome=outcome))
                continue

            event = outcome.to_event(
                reservation,
                sequence=len(self._reservations.events(reservation.reservation_id)),
                occurred_at=now,
                observed_at=now,
                source=source,
                reconciliation_id=reconciliation_id,
            )
            if dry_run:
                updates.append(
                    ReservationUpdate(reservation=reservation, outcome=outcome, event=None)
                )
                continue

            appended = self._reservations.append_event(event)
            updated = self._reservations.current(reservation.reservation_id) or reservation
            _logger.info(
                "reservations.applied",
                reservation_id=reservation.reservation_id,
                state=updated.state.value,
                consumed=str(updated.consumed_amount),
                released=str(updated.released_amount),
                remaining=str(updated.remaining_amount),
                reason=outcome.reason_code.value,
            )
            updates.append(
                ReservationUpdate(
                    reservation=updated,
                    outcome=outcome,
                    event=event if appended else None,
                )
            )
        return updates

    def release(
        self,
        reservation_id: str,
        *,
        executions: Sequence[ExecutionRecord] | None = None,
        at: datetime | None = None,
        source: str = "cli",
    ) -> ReservationUpdate:
        """Release a reservation's remaining capital, if there is proof it is free.

        Deliberately not a general-purpose "force release". The refusal that
        matters is ``UNKNOWN``: an execution whose outcome was never learned may
        be a live order, and a command that could free its capital anyway would
        make every other guard in this milestone decorative. Resolve the
        execution against the broker first — ``execution explain --resolve`` or
        ``reconciliation run`` — and this command becomes unnecessary, because
        the resolved state releases it on its own.
        """
        now = at or self._clock.now()
        reservation = self._reservations.current(reservation_id)
        if reservation is None:
            raise KeyError(f"no reservation {reservation_id}")

        records = list(
            executions
            if executions is not None
            else [
                record
                for record in self._execution_records()
                if record.allocation_id == reservation.allocation_id
                and record.intent.establishes_position
            ]
        )
        blocking = [record for record in records if record.state is ExecutionState.UNKNOWN]
        if blocking:
            return ReservationUpdate(
                reservation=reservation,
                outcome=ReservationOutcome(
                    state=ReservationState.UNKNOWN,
                    reason_code=ReservationReasonCode.RELEASE_REFUSED_UNKNOWN,
                    event_type=ReservationEventType.RESERVATION_RETAINED_UNKNOWN,
                    detail=(
                        f"execution {blocking[0].execution_id} is UNKNOWN: an order may be live "
                        f"at the broker. Releasing this capital could let the campaign fund the "
                        f"same trade twice. Resolve it by observing the broker first"
                    ),
                ),
            )
        live = [record for record in records if record.submitted and record.filled_quantity == 0]
        if live:
            return ReservationUpdate(
                reservation=reservation,
                outcome=ReservationOutcome(
                    state=reservation.state,
                    reason_code=ReservationReasonCode.ORDER_WORKING,
                    event_type=ReservationEventType.RESERVATION_OBSERVED,
                    detail=(
                        f"execution {live[0].execution_id} is {live[0].state.value}: the order "
                        f"is working at the broker and its capital is still committed"
                    ),
                ),
            )

        outcome = resolve_reservation(
            reservation, records, policy=self._config.reconciliation.reservations
        )
        if not outcome.changed:
            return ReservationUpdate(reservation=reservation, outcome=outcome)

        event = outcome.to_event(
            reservation,
            sequence=len(self._reservations.events(reservation_id)),
            occurred_at=now,
            observed_at=now,
            source=source,
        )
        self._reservations.append_event(event)
        return ReservationUpdate(
            reservation=self._reservations.current(reservation_id) or reservation,
            outcome=outcome,
            event=event,
        )

    # --- the campaign's capital -------------------------------------------
    def capital(self, *, as_of: datetime | None = None) -> CampaignCapital:
        """What the campaign's money is actually doing.

        The answer Milestone 7 could not give. ``locked_by_unknown`` is
        reported separately because an operator asking "why can I not open
        another position" deserves to see that the reason is an unresolved
        order rather than a spent budget.
        """
        now = as_of or self._clock.now()
        campaign = self._config.campaign
        reservations = [
            reservation
            for reservation in self._reservations.all_current()
            if reservation.campaign_id == campaign.campaign_id
        ]
        authorized = sum((r.authorized_amount for r in reservations), Decimal("0"))
        consumed = sum((r.consumed_amount for r in reservations), Decimal("0"))
        released = sum((r.released_amount for r in reservations), Decimal("0"))
        committed = sum((r.committed_amount for r in reservations), Decimal("0"))
        unknown = [r for r in reservations if r.locked_by_uncertainty]
        return CampaignCapital(
            campaign_id=campaign.campaign_id,
            as_of=now,
            currency=campaign.currency,
            budget=campaign.budget_eur,
            reserve=campaign.reserve_eur,
            authorized_total=authorized,
            consumed_total=consumed,
            released_total=released,
            committed_total=committed,
            locked_by_unknown=sum((r.committed_amount for r in unknown), Decimal("0")),
            available=campaign.budget_eur - campaign.reserve_eur - committed,
            reservation_count=len(reservations),
            unknown_count=len(unknown),
        )

    # --- internals ---------------------------------------------------------
    def _allocations(self) -> AllocationRepository:
        repository = self._allocation_repository
        if repository is None:
            from trading_system.allocation.store import FilesystemAllocationRepository

            repository = FilesystemAllocationRepository(self._data_root / "allocation")
            self._allocation_repository = repository
        return repository

    def _executions(self) -> ExecutionRepository:
        repository = self._execution_repository
        if repository is None:
            from trading_system.execution.store import FilesystemExecutionRepository

            repository = FilesystemExecutionRepository(self._data_root / "execution")
            self._execution_repository = repository
        return repository

    def _approved_allocations(self) -> list[CampaignAllocation]:
        approved: dict[str, CampaignAllocation] = {}
        for run in self._allocations().all_runs():
            if run.dry_run:
                continue
            for allocation in run.allocations:
                if allocation.outcome is not AllocationOutcome.APPROVED:
                    continue
                # First authorisation wins, exactly as the campaign ledger
                # replay does: a later run that re-derived the same opportunity
                # recognised the existing reservation rather than creating one.
                approved.setdefault(allocation.allocation_id, allocation)
        return sorted(approved.values(), key=lambda a: (a.as_of, a.allocation_id))

    def _execution_records(self) -> list[ExecutionRecord]:
        repository = self._executions()
        records = [
            record
            for entry in repository.history()
            if (record := repository.current(entry.execution_id)) is not None
        ]
        return sorted(records, key=lambda record: record.created_at)
