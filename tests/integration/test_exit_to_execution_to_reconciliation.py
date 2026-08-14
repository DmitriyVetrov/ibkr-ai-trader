"""The Milestone 10 loop, end to end, against the simulated broker.

.. code-block:: text

    open position (M9 reality)
          |
    exit evaluation (M10)          WAIT / EXIT / BLOCK
          |
    exit request -> M8             the ONLY path to an order
          |
    SimulatedBroker                a real submission, counted
          |
    broker observation (M9)        what actually happened
          |
    CLOSED / STILL OPEN / UNKNOWN

The claims this file exists to check are the ones no unit test can make,
because they are about the *seam*:

* a ``WAIT`` submits zero orders and a ``BLOCK`` submits zero orders;
* an ``EXIT`` submits **exactly one**, and the count is read off the broker's
  own counter rather than asserted;
* the exit order is a SELL of the contracts that were bought, as one combo;
* the position becomes ``CLOSED`` only when broker reality says so — not when
  the order was submitted, and not when Milestone 8 reported a fill;
* an ``UNKNOWN`` submission blocks every later run and is never re-sent.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from tests.exit import factories
from tests.exit.factories import NOW

from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import (
    ExecutionIntent,
    ExecutionState,
    ExitDecisionType,
    ExitReasonCode,
    LegAction,
    PositionLifecycleState,
    TradingMode,
)
from trading_system.execution.service import ExecutionService
from trading_system.execution.store import FilesystemExecutionRepository
from trading_system.exit.service import ExitService
from trading_system.exit.store import FilesystemExitRepository
from trading_system.exit.valuation import ExitQuoteReader
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.positions.service import PositionService
from trading_system.positions.store import FilesystemPositionRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def loop(tmp_path: Path, system_config: SystemConfig, market_research_run):
    """The whole chain, wired at ``tmp_path``, against the in-process simulator.

    ``execution.enabled`` is switched on for this fixture only. The shipped
    configuration ships it off, and a test that relied on the shipped value
    would be testing the switch rather than the loop.
    """
    from trading_system.research.store import FilesystemResearchRepository

    def build(*, bid: Decimal, held: bool = True, writable: bool = True):
        clock = FixedClock(NOW)
        data_root = tmp_path / "data"
        settings = Settings(_env_file=None, trading_mode="PAPER")
        config = system_config.model_copy(
            update={"execution": system_config.execution.model_copy(update={"enabled": True})}
        )

        repository = factories.data_repository(data_root, clock=clock)
        factories.store_quotes(
            repository, [factories.option_quote(bid=bid, ask=bid + Decimal("0.20"))]
        )
        FilesystemResearchRepository(data_root / "research").save(market_research_run)

        executions = FilesystemExecutionRepository(data_root / "execution")
        entry = factories.entry_execution(
            research_report_id=market_research_run.reports[0].report_id
        )
        executions.save(entry)

        positions_store = FilesystemPositionRepository(data_root / "positions")
        positions_store.save_snapshot(factories.position_snapshot([] if not held else None))

        broker = SimulatedBroker(
            SimulatedBrokerState(account_id=factories.ACCOUNT, currency="EUR"),
            clock=clock,
            trading_mode=TradingMode.PAPER,
            read_only=not writable,
        )
        broker.connect()

        execution_service = ExecutionService(
            settings=settings,
            config=config,
            clock=clock,
            execution_repository=executions,
            # The in-process simulator, supplied explicitly. Milestone 8 would
            # otherwise build its own writable connection, which needs a
            # gateway — and a test that reached one would be a bug in the test.
            broker_factory=lambda *args, **kwargs: broker,
            root=tmp_path,
        )
        exit_service = ExitService(
            settings=settings,
            config=config,
            clock=clock,
            exit_repository=FilesystemExitRepository(data_root / "exit"),
            position_service=PositionService(
                settings=settings,
                config=config,
                clock=clock,
                position_repository=positions_store,
                execution_repository=executions,
                root=tmp_path,
            ),
            execution_service=execution_service,
            quote_reader=ExitQuoteReader(repository),
            root=tmp_path,
        )
        return exit_service, execution_service, broker, entry, positions_store

    return build


# ---------------------------------------------------------------------------
# WAIT and BLOCK submit nothing
# ---------------------------------------------------------------------------
def test_a_wait_submits_zero_orders(loop) -> None:
    exit_service, _, broker, _, _ = loop(bid=Decimal("6.50"))

    run = exit_service.monitor(authorized=True)

    assert run.result.decisions[0].decision is ExitDecisionType.WAIT
    assert broker.orders_submitted == 0
    assert run.orders_submitted == 0


def test_a_block_submits_zero_orders(loop) -> None:
    """A position nobody could judge is never traded, authorised or not."""
    exit_service, _, broker, _, positions_store = loop(bid=Decimal("6.50"))
    # Replace the snapshot with a failed read: not an empty account.
    positions_store.save_snapshot(factories.position_snapshot(usable=False))

    run = exit_service.monitor(authorized=True)

    assert broker.orders_submitted == 0
    assert run.orders_submitted == 0


# ---------------------------------------------------------------------------
# EXIT submits exactly one order
# ---------------------------------------------------------------------------
def test_an_exit_submits_exactly_one_order(loop) -> None:
    """Counted on the broker itself, not asserted."""
    exit_service, _, broker, _, _ = loop(bid=Decimal("2.00"))

    run = exit_service.monitor(authorized=True, snapshot=None)

    assert run.result.decisions[0].decision is ExitDecisionType.EXIT
    assert broker.orders_submitted == 1
    assert len(run.result.exit_execution_ids) == 1


def test_the_exit_order_sells_the_contracts_that_were_bought(loop) -> None:
    exit_service, execution_service, broker, entry, _ = loop(bid=Decimal("2.00"))

    run = exit_service.monitor(authorized=True)
    record = execution_service.get(run.result.exit_execution_ids[0])

    assert record is not None
    assert record.intent is ExecutionIntent.CLOSE
    assert record.position_id == run.result.decisions[0].position_id
    assert record.entry_execution_id == entry.execution_id
    assert record.quantity == 2
    # Every leg on the *record* is the leg that was sent: inverted, so the
    # position ledger nets an exit fill as a subtraction.
    assert [leg.action for leg in record.legs] == [LegAction.SELL]

    # Milestone 8 closes its connection after a submission — one short-lived
    # connection per order, the Milestone 2 constraint. Reopen it to look.
    broker.connect()
    orders = broker.get_open_orders()
    assert len(orders) == 1
    assert orders[0].side.value == "SELL"


def test_a_close_execution_commits_no_new_capital(loop) -> None:
    """An exit returns money; it never authorises more."""
    exit_service, execution_service, _, _, _ = loop(bid=Decimal("2.00"))

    run = exit_service.monitor(authorized=True)
    record = execution_service.get(run.result.exit_execution_ids[0])

    assert record is not None
    assert record.capital_commitment == Decimal("0")
    assert record.maximum_loss == Decimal("0")


def test_the_limit_offered_is_at_or_below_the_reference(loop) -> None:
    """An exit can only ever ask for less than the price it was decided on."""
    exit_service, execution_service, _, _, _ = loop(bid=Decimal("2.00"))

    run = exit_service.monitor(authorized=True)
    record = execution_service.get(run.result.exit_execution_ids[0])

    assert record is not None
    assert record.submitted_price is not None
    assert record.submitted_price <= Decimal("2.00")


def test_the_lifecycle_moves_to_exit_submitted_not_to_closed(loop) -> None:
    """An acknowledgement is not a closure. Only broker reality closes."""
    exit_service, _, _, _, _ = loop(bid=Decimal("2.00"))

    run = exit_service.monitor(authorized=True)
    position_id = run.result.decisions[0].position_id

    lifecycle = exit_service.lifecycle(position_id)
    assert lifecycle is not None
    assert lifecycle.state is PositionLifecycleState.EXIT_SUBMITTED
    assert lifecycle.exit_execution_id is not None
    assert lifecycle.closed_at is None


# ---------------------------------------------------------------------------
# The second run never sends a second order
# ---------------------------------------------------------------------------
def test_a_second_authorised_run_sends_no_second_order(loop) -> None:
    """The whole idempotency claim, at the seam where it matters."""
    exit_service, _, broker, _, _ = loop(bid=Decimal("2.00"))

    exit_service.monitor(authorized=True)
    assert broker.orders_submitted == 1

    second = exit_service.monitor(authorized=True)

    assert broker.orders_submitted == 1
    assert second.result.decisions[0].primary_reason is ExitReasonCode.EXIT_ALREADY_SUBMITTED
    assert second.result.exit_execution_ids == []


def test_milestone_8_refuses_a_duplicate_exit_request_by_identity(loop) -> None:
    """Even bypassing the lifecycle, the execution ledger refuses."""
    from trading_system.domain.enums import ExecutionReasonCode

    exit_service, execution_service, broker, entry, _ = loop(bid=Decimal("2.00"))
    run = exit_service.monitor(authorized=True)
    assert broker.orders_submitted == 1

    # Rebuild the same request by hand and hand it straight to Milestone 8,
    # bypassing the lifecycle gate entirely. The execution ledger must refuse
    # on its own: two independent guards, because one of them could be wrong.
    from dataclasses import replace

    outcome = run.outcomes[0]
    fresh = exit_service.build_request(
        replace(
            outcome,
            position=replace(
                outcome.position,
                lifecycle=outcome.position.lifecycle.model_copy(
                    update={"state": PositionLifecycleState.EXIT_REQUIRED}
                ),
            ),
        ),
        at=NOW,
    )
    assert fresh is not None

    submission = execution_service.submit_exit(fresh, entry=entry, authorized=True, broker=broker)

    assert ExecutionReasonCode.ALREADY_SUBMITTED in submission.reason_codes
    assert broker.orders_submitted == 1


# ---------------------------------------------------------------------------
# Milestone 9 confirms the closure
# ---------------------------------------------------------------------------
def test_the_position_closes_only_when_the_broker_reports_it_gone(loop) -> None:
    exit_service, _, _, _, positions_store = loop(bid=Decimal("2.00"))
    run = exit_service.monitor(authorized=True)
    position_id = run.result.decisions[0].position_id

    still_open = exit_service.confirm()
    assert still_open == []
    lifecycle = exit_service.lifecycle(position_id)
    assert lifecycle is not None
    assert lifecycle.state is PositionLifecycleState.EXIT_SUBMITTED

    # Now the broker reports an empty account: the exit filled.
    positions_store.save_snapshot(factories.position_snapshot([]))
    confirmed = exit_service.confirm()

    assert len(confirmed) == 1
    assert confirmed[0].state is PositionLifecycleState.CLOSED
    assert confirmed[0].open_quantity == 0
    assert confirmed[0].closed_at is not None


def test_a_closed_position_is_no_longer_evaluated_for_exit(loop) -> None:
    exit_service, _, broker, _, positions_store = loop(bid=Decimal("2.00"))
    exit_service.monitor(authorized=True)
    positions_store.save_snapshot(factories.position_snapshot([]))
    exit_service.confirm()

    run = exit_service.monitor(authorized=True)

    assert broker.orders_submitted == 1
    assert run.result.exit_execution_ids == []


# ---------------------------------------------------------------------------
# UNKNOWN blocks every later run
# ---------------------------------------------------------------------------
def test_an_unknown_exit_blocks_and_is_never_re_sent(loop) -> None:
    """The most dangerous state in the milestone, at the seam."""
    from trading_system.broker.base import BrokerTimeoutError

    exit_service, execution_service, broker, _entry, _ = loop(bid=Decimal("2.00"))

    def _timeout(intent):
        raise BrokerTimeoutError("the broker did not answer")

    broker._submit_order = _timeout

    first = exit_service.monitor(authorized=True)
    execution_id = first.result.exit_execution_ids[0]
    record = execution_service.get(execution_id)
    assert record is not None
    assert record.state is ExecutionState.UNKNOWN

    position_id = first.result.decisions[0].position_id
    lifecycle = exit_service.lifecycle(position_id)
    assert lifecycle is not None
    assert lifecycle.state is PositionLifecycleState.EXIT_UNKNOWN

    before = broker.orders_submitted
    second = exit_service.monitor(authorized=True)

    assert second.result.decisions[0].decision is ExitDecisionType.BLOCK
    assert second.result.decisions[0].primary_reason is ExitReasonCode.EXIT_OUTCOME_UNKNOWN
    assert second.result.exit_execution_ids == []
    assert broker.orders_submitted == before


def test_an_unknown_exit_is_never_relabelled_failed(loop) -> None:
    """Milestone 8's invariant, carried into Milestone 10 unchanged."""
    from trading_system.broker.base import BrokerTimeoutError

    exit_service, execution_service, broker, _, _ = loop(bid=Decimal("2.00"))

    def _timeout(intent):
        raise BrokerTimeoutError("the broker did not answer")

    broker._submit_order = _timeout
    run = exit_service.monitor(authorized=True)

    record = execution_service.get(run.result.exit_execution_ids[0])

    assert record is not None
    assert record.state is not ExecutionState.FAILED
    assert record.state is ExecutionState.UNKNOWN


# ---------------------------------------------------------------------------
# The read-only guard
# ---------------------------------------------------------------------------
def test_a_read_only_broker_refuses_and_nothing_is_sent(loop) -> None:
    """``FAILED`` means the attempt provably never left the process."""
    exit_service, execution_service, broker, _, _ = loop(bid=Decimal("2.00"), writable=False)

    run = exit_service.monitor(authorized=True)
    record = execution_service.get(run.result.exit_execution_ids[0])

    assert record is not None
    assert record.state is ExecutionState.FAILED
    assert broker.orders_submitted == 0


# ---------------------------------------------------------------------------
# The expected-position ledger nets the exit
# ---------------------------------------------------------------------------
def test_the_exit_fill_nets_the_expected_position_to_zero(loop) -> None:
    """The mechanism by which a position actually becomes closed internally:
    the exit's fills subtract, the entry's added, and the net is zero."""
    from trading_system.positions.expected import expected_from_execution

    exit_service, execution_service, _, entry, _ = loop(bid=Decimal("2.00"))
    run = exit_service.monitor(authorized=True)
    exit_record = execution_service.get(run.result.exit_execution_ids[0])
    assert exit_record is not None

    filled_exit = exit_record.model_copy(
        update={"state": ExecutionState.FILLED, "filled_quantity": 2}
    )
    opened = expected_from_execution(entry, as_of=NOW, account_reference=factories.MASKED)
    closed = expected_from_execution(filled_exit, as_of=NOW, account_reference=factories.MASKED)

    assert opened[0].quantity == Decimal("2")
    assert closed[0].quantity == Decimal("-2")
    assert opened[0].key == closed[0].key
    assert [leg.action for leg in filled_exit.legs] == [LegAction.SELL]


def test_a_close_execution_derives_no_second_strategy_structure(loop) -> None:
    """Otherwise a partly-closed structure would surface as PARTIAL_STRUCTURE —
    a finding about an authorised position that is only half held, not about
    one that is half sold."""
    from trading_system.positions.expected import project_expected_positions

    exit_service, execution_service, _, entry, _ = loop(bid=Decimal("2.00"))
    run = exit_service.monitor(authorized=True)
    exit_record = execution_service.get(run.result.exit_execution_ids[0])
    assert exit_record is not None
    filled_exit = exit_record.model_copy(
        update={"state": ExecutionState.FILLED, "filled_quantity": 2}
    )

    projection = project_expected_positions(
        fills=[],
        executions=[entry, filled_exit],
        as_of=NOW,
        account_reference=factories.MASKED,
        snapshot=factories.position_snapshot(),
    )

    assert len(projection.strategies) == 1
    assert projection.strategies[0].execution_id == entry.execution_id
