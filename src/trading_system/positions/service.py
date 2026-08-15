"""The position service: the composition root for the Milestone 9 ledger.

One stage, two records, and a single boundary to the broker:

.. code-block:: text

    Broker (read-only)                <- ONE short-lived connection
          |
    account + positions + orders + fills
          |
    normalisation                     <- pure, in snapshot.py / fills.py
          |
    BrokerPositionSnapshot            <- immutable, content-addressed
    recorded fills                    <- deduplicated on the broker's own ids
          |
    ExpectedPosition projection       <- from CONFIRMED FILLS only
          |
    StrategyPosition                  <- the logical structure view

Properties this service holds regardless of which path it takes:

* **It cannot trade.** It builds its broker through
  :func:`~trading_system.broker.factory.build_broker`, which returns a
  read-only connection whatever the settings say, and every read asserts the
  broker's own submitted-order counter is still zero afterwards. A test walks
  the import graph and asserts the writable constructor is unreachable from
  here.
* **It reads the broker once.** Account summary, positions, open orders and
  fills are all served from ``ib_async``'s startup handshake cache, so one
  connection supplies all four without a second uncached round trip — the
  constraint Milestone 2 measured and every later milestone has respected. No
  health probe is issued first: spending the connection's one reliable request
  on a diagnostic is exactly what that constraint forbids.
* **A failed read is never an empty account.** Each of the four reads carries
  its own :class:`~trading_system.domain.enums.BrokerReadStatus`, and a failure
  produces a snapshot that says so rather than one that says the account holds
  nothing.
* **It re-decides nothing.** No sizing, no risk, no strategy, no model. The
  execution ledger and the allocation ledger are read; neither is written.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_system.domain.enums import (
    AcquisitionProvenance,
    BrokerReadStatus,
    ExecutionState,
)
from trading_system.domain.models import (
    BrokerAccount,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
)
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import (
    PositionsConfig,
    Settings,
    SystemConfig,
    project_root,
)
from trading_system.positions.expected import ExpectedProjection, project_expected_positions
from trading_system.positions.fills import ContractTerms, terms_from_legs, to_observed_fills
from trading_system.positions.models import (
    BrokerPositionSnapshot,
    ObservedFill,
    mask_account,
)
from trading_system.positions.snapshot import build_position_snapshot, unavailable_snapshot
from trading_system.positions.store import (
    FilesystemFillRepository,
    FilesystemPositionRepository,
    FillRepository,
    PositionRepository,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from trading_system.broker.base import Broker
    from trading_system.execution.models import ExecutionRecord
    from trading_system.execution.store import ExecutionRepository

__all__ = ["BrokerState", "PositionCapture", "PositionService"]

_logger = get_logger(__name__)


def _attempt(
    label: str,
    call: Callable[[], Any],
    empty: Any,
    failures: list[str],
) -> tuple[Any, BrokerReadStatus]:
    """Run one broker read, and classify how it went.

    Every failure mode gets its own status because they mean different things
    to a reconciliation. A timeout is "the account is unknown right now"; an
    unavailable broker is "we never got to look"; a malformed answer is "the
    broker said something we could not turn into a position". None of them is
    "the account holds nothing", which is what an empty list means and only an
    empty list means.
    """
    from trading_system.broker.base import BrokerError, BrokerTimeoutError

    try:
        value = call()
    except BrokerTimeoutError as exc:
        failures.append(f"{label}: {exc}")
        return empty, BrokerReadStatus.TIMEOUT
    except BrokerError as exc:
        failures.append(f"{label}: {exc}")
        return empty, BrokerReadStatus.UNAVAILABLE
    except Exception as exc:  # a translation fault, not a market event
        failures.append(f"{label}: {exc!r}")
        return empty, BrokerReadStatus.MALFORMED
    if isinstance(value, list):
        return value, (BrokerReadStatus.OK if value else BrokerReadStatus.EMPTY)
    return value, BrokerReadStatus.OK


@dataclass(frozen=True, slots=True)
class BrokerState:
    """Everything one connection was able to read, and what it could not.

    Four independent statuses rather than one. A broker that answered with
    positions but refused the execution list has told us something useful about
    positions, and collapsing that into a single "unavailable" would throw away
    the part that worked; collapsing it into "available" would let a missing
    execution list read as an account that has never traded.
    """

    broker: str
    account: BrokerAccount | None = None
    positions: tuple[BrokerPosition, ...] = ()
    orders: tuple[BrokerOrder, ...] = ()
    executions: tuple[BrokerExecution, ...] = ()

    account_status: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    positions_status: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    orders_status: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED
    executions_status: BrokerReadStatus = BrokerReadStatus.NOT_REQUESTED

    #: Orders *this read* submitted, as a delta across it. Structurally zero.
    #: A delta rather than the broker's lifetime counter, because the same
    #: connection may legitimately have submitted an order earlier in the
    #: process — what must be zero is what reading position state did.
    orders_submitted: int = 0
    read_only: bool = True
    connected: bool = False
    detail: str | None = None
    failures: tuple[str, ...] = ()

    @property
    def account_id(self) -> str | None:
        return self.account.account_id if self.account else None

    @property
    def usable(self) -> bool:
        """Whether anything at all was read. Not whether everything was."""
        return self.connected and self.positions_status.usable


@dataclass(frozen=True, slots=True)
class PositionCapture:
    """The outcome of one ``positions snapshot``."""

    snapshot: BrokerPositionSnapshot
    state: BrokerState
    stored: bool = False
    recorded_fills: tuple[ObservedFill, ...] = ()
    reobserved_fills: tuple[ObservedFill, ...] = ()
    refused_fills: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    warnings: tuple[str, ...] = ()

    @property
    def orders_submitted(self) -> int:
        """Read off the broker. Structurally zero; recorded as evidence."""
        return self.state.orders_submitted


class PositionService:
    """Captures broker position state and projects the internal ledger."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        position_repository: PositionRepository | None = None,
        fill_repository: FillRepository | None = None,
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

        self._positions = position_repository or FilesystemPositionRepository(
            data_root / "positions"
        )
        self._fills = fill_repository or FilesystemFillRepository(data_root / "fills")
        self._execution_repository: ExecutionRepository | None = execution_repository
        #: Injected so a test can supply a controlled broker without patching a
        #: module. Defaults to the read-only factory — the writable one is not
        #: reachable from this package at all, and a test asserts it.
        self._broker_factory = broker_factory

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> PositionRepository:
        return self._positions

    @property
    def fills(self) -> FillRepository:
        return self._fills

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def policy(self) -> PositionsConfig:
        return self._config.positions

    def latest_snapshot(self) -> BrokerPositionSnapshot | None:
        return self._positions.latest()

    def latest_usable_snapshot(self) -> BrokerPositionSnapshot | None:
        return self._positions.latest_usable()

    def snapshot_as_of(self, instant: datetime) -> BrokerPositionSnapshot | None:
        return self._positions.reconstruct(instant)

    # --- reading the broker ------------------------------------------------
    def read_broker_state(
        self,
        *,
        broker: Broker | None = None,
        want_orders: bool = True,
        want_executions: bool = True,
    ) -> BrokerState:
        """Read account, positions, orders and fills over one connection.

        Every read is attempted independently and every failure is recorded
        rather than raised: a broker that answers three of four questions has
        told us three useful things, and losing them because of the fourth
        would make reconciliation all-or-nothing against a gateway that is only
        ever partly cooperative.
        """
        with self._broker(broker) as (connection, failure):
            if connection is None:
                return BrokerState(
                    broker="NONE",
                    connected=False,
                    detail=failure or "no broker connection could be opened",
                    failures=(failure or "broker unavailable",),
                    account_status=BrokerReadStatus.UNAVAILABLE,
                    positions_status=BrokerReadStatus.UNAVAILABLE,
                    orders_status=BrokerReadStatus.UNAVAILABLE,
                    executions_status=BrokerReadStatus.UNAVAILABLE,
                )

            failures: list[str] = []
            before = connection.orders_submitted

            account, account_status = _attempt("account", connection.get_account, None, failures)
            positions, positions_status = _attempt(
                "positions", connection.get_positions, [], failures
            )
            orders, orders_status = (
                _attempt("open orders", connection.get_open_orders, [], failures)
                if want_orders
                else ([], BrokerReadStatus.NOT_REQUESTED)
            )
            executions, executions_status = (
                _attempt("executions", connection.get_executions, [], failures)
                if want_executions
                else ([], BrokerReadStatus.NOT_REQUESTED)
            )

            submitted = connection.orders_submitted - before
            state = BrokerState(
                broker=connection.name,
                account=account,
                positions=tuple(positions or []),
                orders=tuple(orders or []),
                executions=tuple(executions or []),
                account_status=account_status,
                positions_status=positions_status,
                orders_status=orders_status,
                executions_status=executions_status,
                orders_submitted=submitted,
                read_only=connection.read_only,
                connected=True,
                detail="; ".join(failures) or None,
                failures=tuple(failures),
            )

        if state.orders_submitted:
            # Should be impossible: the connection is read-only by construction.
            # Kept as a live assertion because the cost of checking is a
            # comparison and the cost of not checking is a broker statement.
            raise RuntimeError(
                f"reading position state submitted {state.orders_submitted} order(s) during the "
                f"read itself. This path must never mutate broker state"
            )
        return state

    # --- capture -----------------------------------------------------------
    def capture(
        self,
        *,
        broker: Broker | None = None,
        as_of: datetime | None = None,
        store: bool = True,
        record_fills: bool = True,
        state: BrokerState | None = None,
    ) -> PositionCapture:
        """Read the broker and store what it reported.

        A failed read is stored too. The attempt is part of the record, and the
        stored snapshot says ``UNAVAILABLE`` — which no consumer can mistake
        for an empty account, because the model refuses to let it carry
        positions and every reader checks ``usable``.
        """
        started = time.perf_counter()
        now = as_of or self._clock.now()
        observed = self._clock.now()
        policy = self._config.positions
        broker_state = state or self.read_broker_state(broker=broker)

        if not broker_state.positions_status.usable:
            snapshot = unavailable_snapshot(
                broker=broker_state.broker,
                account_id=broker_state.account_id,
                trading_mode=self._settings.trading_mode,
                as_of=now,
                observed_at=observed,
                status=broker_state.positions_status,
                detail=(
                    broker_state.detail
                    or "broker position state could not be read; this is not an empty account"
                ),
                mask_visible=policy.account_mask_visible_characters,
            )
        else:
            snapshot = build_position_snapshot(
                broker_state.positions,
                broker=broker_state.broker,
                account_id=broker_state.account_id,
                trading_mode=self._settings.trading_mode,
                as_of=now,
                observed_at=observed,
                mask_visible=policy.account_mask_visible_characters,
                orders_submitted=broker_state.orders_submitted,
                read_only=broker_state.read_only,
                provenance_by_key=self._provenance_by_key(),
            )

        # A failed read is stored too: the attempt is part of the record, and
        # the stored snapshot says UNAVAILABLE rather than looking like an
        # account that holds nothing.
        worth_storing = (
            bool(snapshot.positions) or not snapshot.usable or policy.snapshot.store_empty_snapshots
        )
        stored = False
        if store and worth_storing:
            self._positions.save_snapshot(snapshot)
            stored = True

        recorded: list[ObservedFill] = []
        known: list[ObservedFill] = []
        refused: tuple[str, ...] = ()
        if record_fills and broker_state.executions_status.usable:
            observed_fills, refused = self._translate_fills(broker_state, observed_at=observed)
            if store:
                recorded, known = self._fills.save_many(observed_fills)
            else:
                existing = self._fills.known_ids()
                recorded = [f for f in observed_fills if f.fill_id not in existing]
                known = [f for f in observed_fills if f.fill_id in existing]

        _logger.info(
            "positions.capture",
            broker=broker_state.broker,
            account=snapshot.account_reference,
            read_status=snapshot.read_status.value,
            positions=len(snapshot.positions),
            fills_recorded=len(recorded),
            orders_submitted=broker_state.orders_submitted,
        )
        return PositionCapture(
            snapshot=snapshot,
            state=broker_state,
            stored=stored,
            recorded_fills=tuple(recorded),
            reobserved_fills=tuple(known),
            refused_fills=refused,
            duration_seconds=time.perf_counter() - started,
            warnings=tuple(broker_state.failures),
        )

    # --- the internal ledger -----------------------------------------------
    def expected(
        self,
        *,
        as_of: datetime | None = None,
        snapshot: BrokerPositionSnapshot | None = None,
        executions: Sequence[ExecutionRecord] | None = None,
    ) -> ExpectedProjection:
        """Project the internal ledger from confirmed fills.

        The executions considered are those in a state where the broker
        reported a fill. ``UNKNOWN`` is deliberately absent from that set: a
        submission whose outcome was never learned establishes no position, and
        counting one would be exactly the optimism this milestone exists to
        prevent.
        """
        now = as_of or self._clock.now()
        records = list(executions if executions is not None else self._filled_executions())
        reference = self._account_reference(snapshot)
        return project_expected_positions(
            fills=self._fills.all(),
            executions=records,
            as_of=now,
            account_reference=reference,
            snapshot=snapshot if snapshot is not None else self._positions.latest_usable(),
        )

    def execution_records(self) -> list[ExecutionRecord]:
        """Every stored execution, current state, oldest first."""
        repository = self._executions()
        if repository is None:
            return []
        records = [
            record
            for entry in repository.history()
            if (record := repository.current(entry.execution_id)) is not None
        ]
        return sorted(records, key=lambda record: record.created_at)

    # --- internals ---------------------------------------------------------
    def _executions(self) -> ExecutionRepository | None:
        if self._execution_repository is None:
            from trading_system.execution.store import FilesystemExecutionRepository

            self._execution_repository = FilesystemExecutionRepository(
                self._data_root / "execution"
            )
        return self._execution_repository

    def _filled_executions(self) -> list[ExecutionRecord]:
        return [
            record
            for record in self.execution_records()
            if record.state
            in (
                ExecutionState.FILLED,
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.CANCELLED,
            )
            and record.filled_quantity > 0
        ]

    def _account_reference(self, snapshot: BrokerPositionSnapshot | None) -> str:
        if snapshot is not None:
            return snapshot.account_reference
        latest = self._positions.latest()
        if latest is not None:
            return latest.account_reference
        return mask_account(
            self._settings.ibkr_account_id,
            visible=self._config.positions.account_mask_visible_characters,
        )

    def _provenance_by_key(self) -> dict[str, AcquisitionProvenance]:
        """Which observed contracts an execution of ours accounts for.

        Everything else stays ``UNKNOWN``. A pre-existing holding in a real
        paper account has no execution history here, and giving it an invented
        one would fabricate exactly the audit trail this system exists to keep
        honest.
        """
        from trading_system.positions.expected import leg_key

        known: dict[str, AcquisitionProvenance] = {}
        for record in self._filled_executions():
            if not record.intent.establishes_position:
                # Only an execution that *acquired* something can explain how a
                # holding got here. A CLOSE names contracts an OPEN already
                # claimed, so restricting this changes nothing for it — and it
                # keeps an orphan cleanup from stamping SYSTEM_EXECUTION on a
                # holding whose acquisition provenance is, and stays, UNKNOWN.
                continue
            for leg in record.legs:
                known[leg_key(leg, symbol=record.underlying)] = (
                    AcquisitionProvenance.SYSTEM_EXECUTION
                )
        return known

    def _translate_fills(
        self, state: BrokerState, *, observed_at: datetime
    ) -> tuple[list[ObservedFill], tuple[str, ...]]:
        """Normalise the broker's execution reports, linking ours where we can."""
        terms: dict[int, ContractTerms] = {}
        execution_by_order: dict[str, str] = {}
        allocation_by_order: dict[str, str] = {}
        opportunity_by_order: dict[str, str] = {}
        for record in self.execution_records():
            terms.update(terms_from_legs(record.legs))
            if record.broker_order_id:
                execution_by_order[record.broker_order_id] = record.execution_id
                # An orphan cleanup has neither, and must not be given one:
                # the fill it produces is real and is recorded as ours, but it
                # belongs to no allocation and no opportunity.
                if record.allocation_id is not None:
                    allocation_by_order[record.broker_order_id] = record.allocation_id
                if record.opportunity_id is not None:
                    opportunity_by_order[record.broker_order_id] = record.opportunity_id

        fills, refused = to_observed_fills(
            state.executions,
            observed_at=observed_at,
            account_reference=mask_account(
                state.account_id,
                visible=self._config.positions.account_mask_visible_characters,
            ),
            mask_visible=self._config.positions.account_mask_visible_characters,
            terms_by_contract=terms,
            execution_by_order=execution_by_order,
            allocation_by_order=allocation_by_order,
            opportunity_by_order=opportunity_by_order,
            simulated=state.broker.upper() in {"SIMULATOR", "SIMULATED"},
        )
        return fills, tuple(refused)

    @contextmanager
    def _broker(self, supplied: Broker | None) -> Iterator[tuple[Broker | None, str | None]]:
        """Open a read-only broker, and always close it.

        Short-lived on purpose: Milestone 2 established that only the first
        uncached round trip on a TWS connection is reliably answered, so one
        capture gets one connection and the connection is closed after it. No
        health probe is issued — that would spend the connection's one reliable
        request on a diagnostic.
        """
        if supplied is not None:
            yield supplied, None
            return

        from trading_system.broker.base import BrokerError
        from trading_system.broker.factory import build_broker

        factory = self._broker_factory or build_broker
        try:
            connection = factory(self._settings, clock=self._clock)  # type: ignore[operator]
        except BrokerError as exc:
            yield None, str(exc)
            return

        try:
            connection.connect()
        except BrokerError as exc:
            connection.disconnect()
            yield None, str(exc)
            return

        try:
            yield connection, None
        finally:
            connection.disconnect()
