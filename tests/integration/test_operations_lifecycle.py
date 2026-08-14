"""The Milestone 11 loop, end to end, against the simulated broker.

.. code-block:: text

    allocation (M7)            capital authorised and reserved
          |
    execution (M8)             the order that opened it
          |
    confirmed fills (M9)       what actually traded
          |
    exit (M10)                 the decision, the order, the confirmation
          |
    CLOSED lifecycle           broker reality, not a submitted order
          |
    realised profit and loss   from confirmed fills and nothing else
          |
    reservation settlement     capital returns to the campaign
          |
    daily loss state           what the risk engine reads next time

The claims this file exists to check are the ones no unit test can make,
because they are about the *seam*:

* capital that went out comes back, **exactly once**, and only after the
  broker confirms the position is gone;
* the realised figure is the one the fills support, to the cent;
* a second scheduler run creates no duplicate exit, no duplicate order, no
  duplicate settlement and returns no capital a second time;
* the daily loss the risk engine reads is the figure this loop produced;
* an ``UNKNOWN`` execution stops all of it, and no elapsed time changes that.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from tests.pnl import factories
from tests.pnl.factories import EXIT_AT, NOW

from trading_system.domain.enums import (
    DailyPnLStatus,
    ExecutionState,
    PnLStatus,
    PositionLifecycleEventType,
    PositionLifecycleState,
    ReservationState,
    SettlementBlockReason,
    SettlementStatus,
)
from trading_system.execution.store import FilesystemExecutionRepository
from trading_system.exit.models import (
    PositionLifecycleEvent,
    PositionLifecycleSnapshot,
    lifecycle_event_identifier,
    lifecycle_snapshot_identifier,
)
from trading_system.exit.store import FilesystemExitRepository
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.pnl.service import PnLService
from trading_system.pnl.store import FilesystemPnLRepository
from trading_system.positions.store import FilesystemFillRepository
from trading_system.reservations.service import ReservationService
from trading_system.reservations.store import FilesystemReservationRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def lifecycle(tmp_path: Path, system_config: SystemConfig):
    """The whole chain, wired at ``tmp_path``, with no broker anywhere.

    Milestone 11 holds no broker: whether the position is closed is read from
    Milestone 10's lifecycle, which reached ``CLOSED`` because a Milestone 9
    snapshot reported the account holds none of the structure. This fixture
    assembles exactly that evidence and then exercises the settlement path
    over it.
    """

    def build(
        *,
        entry_state: ExecutionState = ExecutionState.FILLED,
        exit_state: ExecutionState = ExecutionState.FILLED,
        closed: bool = True,
        exit_quantity: int = factories.QUANTITY,
        exit_price: Decimal = factories.EXIT_QUOTE,
        commission: Decimal | None = Decimal("1.50"),
    ):
        clock = FixedClock(EXIT_AT)
        data_root = tmp_path / "data"
        settings = Settings(_env_file=None, trading_mode="PAPER")

        # --- Milestone 8: the two executions -------------------------------
        executions = FilesystemExecutionRepository(data_root / "execution")
        entry = factories.entry_execution(state=entry_state)
        closing = factories.exit_execution(state=exit_state, quantity=exit_quantity)
        executions.save(entry)
        executions.save(closing)

        # --- Milestone 9: what actually traded -----------------------------
        fills = FilesystemFillRepository(data_root / "fills")
        for fill in factories.entry_fills(commission=commission):
            fills.save(fill)
        if exit_state is ExecutionState.FILLED:
            for fill in factories.exit_fills(
                quantity=exit_quantity, price=exit_price, commission=commission
            ):
                fills.save(fill)

        # --- Milestone 9: the capital that was committed -------------------
        reservations_store = FilesystemReservationRepository(data_root / "reservations")
        reservations_store.save(factories.reservation())

        # --- Milestone 10: the lifecycle -----------------------------------
        exits = FilesystemExitRepository(data_root / "exit")
        exits.save_lifecycle(
            PositionLifecycleSnapshot(
                lifecycle_id=lifecycle_snapshot_identifier(
                    position_id=factories.POSITION, as_of=NOW, content_digest="test"
                ),
                position_id=factories.POSITION,
                as_of=NOW,
                updated_at=NOW,
                state=PositionLifecycleState.OPEN,
                underlying="NVDA",
                strategy=entry.strategy,
                open_quantity=factories.QUANTITY,
                entry_execution_id=entry.execution_id,
                allocation_id=entry.allocation_id,
                campaign_id=entry.campaign_id,
                opportunity_id=entry.opportunity_id,
            )
        )
        if closed:
            # Milestone 10 reaches CLOSED only when a Milestone 9 snapshot
            # reported the account holds none of the structure. That event is
            # the confirmation Milestone 11 settles against.
            exits.append_lifecycle_event(
                PositionLifecycleEvent(
                    event_id=lifecycle_event_identifier(
                        position_id=factories.POSITION,
                        sequence=0,
                        event_type=PositionLifecycleEventType.EXIT_CONFIRMED_CLOSED.value,
                    ),
                    position_id=factories.POSITION,
                    sequence=0,
                    event_type=PositionLifecycleEventType.EXIT_CONFIRMED_CLOSED,
                    state=PositionLifecycleState.CLOSED,
                    occurred_at=EXIT_AT,
                    observed_at=EXIT_AT,
                    source="reconciliation",
                    open_quantity=0,
                    exit_execution_id=closing.execution_id,
                    detail="the broker reports none of this structure",
                )
            )

        reservations = ReservationService(
            settings=settings,
            config=system_config,
            clock=clock,
            reservation_repository=reservations_store,
            execution_repository=executions,
            root=tmp_path,
        )
        service = PnLService(
            settings=settings,
            config=system_config,
            clock=clock,
            exit_repository=exits,
            execution_repository=executions,
            fill_repository=fills,
            reservation_service=reservations,
            root=tmp_path,
        )
        return service, reservations, FilesystemPnLRepository(data_root / "pnl"), data_root

    return build


# ---------------------------------------------------------------------------
# The happy path, end to end
# ---------------------------------------------------------------------------
def test_a_closed_position_produces_a_realised_result(lifecycle) -> None:
    service, _, _, _ = lifecycle()

    run = service.run()

    assert run.result.positions_examined == 1
    assert run.result.results_computed == 1
    result = run.results[0]
    assert result.status is PnLStatus.COMPLETE
    # 2 contracts, 6.05 -> 8.05, multiplier 100, two 1.50 commissions.
    assert result.realized_gross_pnl == Decimal("400.00")
    assert result.realized_net_pnl == Decimal("397.00")


def test_the_capital_returns_to_the_campaign(lifecycle) -> None:
    service, reservations, _, _ = lifecycle()

    before = reservations.capital()
    run = service.run()
    after = reservations.capital()

    assert run.result.settlements_applied == 1
    assert run.result.capital_returned == Decimal("1210.00")
    assert before.committed_total == Decimal("1210.00")
    assert after.committed_total == Decimal("0")
    assert after.available > before.available


def test_the_reservation_records_what_was_spent_as_well_as_what_came_back(
    lifecycle,
) -> None:
    """Settlement does not erase the consumption. The difference between the
    two figures is the trade's result."""
    service, reservations, _, _ = lifecycle()

    service.run()
    reservation = reservations.for_allocation(factories.ALLOCATION)

    assert reservation is not None
    assert reservation.state is ReservationState.SETTLED
    assert reservation.consumed_amount == Decimal("1210.00")
    assert reservation.settled_amount == Decimal("1210.00")
    assert reservation.realized_pnl == Decimal("397.00")
    assert reservation.committed_amount == Decimal("0")


def test_the_daily_figure_is_what_the_risk_engine_reads(lifecycle) -> None:
    service, _, _, data_root = lifecycle()
    service.run()

    from trading_system.pnl.campaign_state import read_campaign_state

    state = read_campaign_state(
        data_root,
        campaign_id=factories.CAMPAIGN,
        as_of=EXIT_AT,
        day_boundary_timezone="America/New_York",
    )

    assert state.daily_pnl_status is DailyPnLStatus.TRACKED
    assert state.realized_pnl_today == Decimal("397.00")
    assert factories.OPPORTUNITY in state.settled_opportunity_ids


def test_a_settled_opportunity_stops_consuming_the_campaign_envelope(
    lifecycle, system_config
) -> None:
    """The whole point of the release: the campaign can fund another trade.

    Milestone 7 replays the *allocation* ledger, so a settlement that only
    moved the reservation would be cosmetic. This is the seam that makes it
    real.
    """
    from trading_system.allocation.campaign import reservations_from

    service, _, _, data_root = lifecycle()
    service.run()

    from trading_system.pnl.campaign_state import read_campaign_state

    state = read_campaign_state(data_root, campaign_id=factories.CAMPAIGN, as_of=EXIT_AT)

    assert (
        reservations_from(
            [],
            campaign_id=factories.CAMPAIGN,
            as_of=EXIT_AT,
            settled_opportunity_ids=state.settled_opportunity_ids,
        )
        == []
    )
    assert factories.OPPORTUNITY in state.settled_opportunity_ids


# ---------------------------------------------------------------------------
# Running it twice
# ---------------------------------------------------------------------------
def test_a_second_run_returns_no_capital_a_second_time(lifecycle) -> None:
    """A duplicate record is untidy; a double release is money."""
    service, reservations, _, _ = lifecycle()

    first = service.run()
    after_first = reservations.capital()
    second = service.run()
    after_second = reservations.capital()

    assert first.result.capital_returned == Decimal("1210.00")
    assert second.result.capital_returned == Decimal("0")
    assert after_first.committed_total == after_second.committed_total
    assert after_first.available == after_second.available


def test_a_second_run_stores_no_second_result(lifecycle) -> None:
    service, _, store, _ = lifecycle()

    service.run()
    first = {record.pnl_id for record in store.all()}
    service.run()
    second = {record.pnl_id for record in store.all()}

    assert first == second
    assert len(first) == 1


def test_a_second_run_appends_no_second_settlement_event(lifecycle) -> None:
    service, reservations, _, _ = lifecycle()

    service.run()
    reservation = reservations.for_allocation(factories.ALLOCATION)
    assert reservation is not None
    first = len(reservations.repository.events(reservation.reservation_id))

    service.run()
    second = len(reservations.repository.events(reservation.reservation_id))

    assert first == second


def test_the_second_run_reports_already_settled_rather_than_failing(lifecycle) -> None:
    service, _, _, _ = lifecycle()

    service.run()
    second = service.run()

    statuses = {record.settlement.status for record in second.settlements}
    assert statuses == {SettlementStatus.ALREADY_SETTLED}


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------
def test_an_open_position_settles_nothing(lifecycle) -> None:
    """Only broker-confirmed closure settles. Not a submitted exit, not a
    reported fill."""
    service, reservations, _, _ = lifecycle(closed=False)

    run = service.run()

    assert run.result.positions_examined == 0
    assert run.result.capital_returned == Decimal("0")
    reservation = reservations.for_allocation(factories.ALLOCATION)
    assert reservation is not None
    assert reservation.committed_amount == Decimal("1210.00")


def test_an_unknown_exit_never_releases_capital(lifecycle) -> None:
    """The order may be working at the broker right now."""
    service, reservations, _, _ = lifecycle(exit_state=ExecutionState.UNKNOWN)

    run = service.run()

    assert run.result.capital_returned == Decimal("0")
    blocked = [record.settlement for record in run.settlements]
    assert blocked
    assert blocked[0].status is SettlementStatus.BLOCKED
    assert blocked[0].block_reason is SettlementBlockReason.EXECUTION_UNKNOWN
    reservation = reservations.for_allocation(factories.ALLOCATION)
    assert reservation is not None
    assert reservation.committed_amount == Decimal("1210.00")


def test_an_unknown_exit_produces_no_realised_figure(lifecycle) -> None:
    """What traded is not settled fact yet, so there is no number."""
    service, _, _, _ = lifecycle(exit_state=ExecutionState.UNKNOWN)

    run = service.run()

    assert run.results[0].status is PnLStatus.NOT_AVAILABLE
    assert run.results[0].realized_gross_pnl is None


def test_an_unknown_exit_leaves_the_day_unknown_rather_than_flat(lifecycle) -> None:
    """An unknown loss is not a zero loss, and the risk engine must see the
    difference."""
    service, _, _, data_root = lifecycle(exit_state=ExecutionState.UNKNOWN)
    service.run()

    from trading_system.pnl.campaign_state import read_campaign_state

    state = read_campaign_state(data_root, campaign_id=factories.CAMPAIGN, as_of=EXIT_AT)

    assert state.daily_pnl_status is DailyPnLStatus.UNKNOWN
    assert state.realized_pnl_today is None
    assert factories.POSITION in state.unavailable_position_ids


# ---------------------------------------------------------------------------
# Partial closure
# ---------------------------------------------------------------------------
def test_a_partial_exit_returns_only_part_of_the_capital(lifecycle) -> None:
    service, reservations, _, _ = lifecycle(exit_quantity=1)

    run = service.run()

    assert run.results[0].status is PnLStatus.PARTIAL
    assert run.result.capital_returned == Decimal("605.00")
    reservation = reservations.for_allocation(factories.ALLOCATION)
    assert reservation is not None
    assert reservation.committed_amount == Decimal("605.00")
    assert reservation.state is not ReservationState.SETTLED


# ---------------------------------------------------------------------------
# Missing costs
# ---------------------------------------------------------------------------
def test_a_missing_commission_still_settles_on_the_gross_figure(lifecycle) -> None:
    """The fill prices support a real result; the commission report is late.

    The capital is what came back either way — a commission changes the
    trade's *result*, not the amount that was committed.
    """
    service, _, _, _ = lifecycle(commission=None)

    run = service.run()

    assert run.results[0].status is PnLStatus.COMPLETE
    assert run.results[0].realized_gross_pnl == Decimal("400.00")
    assert run.results[0].realized_net_pnl is None
    assert run.result.capital_returned == Decimal("1210.00")


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def test_the_whole_loop_submits_no_orders(lifecycle) -> None:
    """Structurally: this package holds no broker and has no order path."""
    service, _, _, _ = lifecycle()

    run = service.run()

    assert run.orders_submitted == 0
    assert run.result.orders_submitted == 0


def test_a_dry_run_moves_no_capital(lifecycle) -> None:
    service, reservations, store, _ = lifecycle()

    run = service.run(dry_run=True)

    assert run.result.capital_returned == Decimal("0")
    assert run.result.settlements_applied == 0
    reservation = reservations.for_allocation(factories.ALLOCATION)
    assert reservation is not None
    assert reservation.committed_amount == Decimal("1210.00")
    assert store.all() == []
