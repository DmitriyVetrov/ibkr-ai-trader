"""The whole orphan-cleanup loop, against the simulated broker.

.. code-block:: text

    pre-existing broker holding      nothing internal accounts for it
          |
    reconciliation                   ORPHAN_BROKER_POSITION
          |
    review                           0 orders, no writable broker constructed
          |
    authorised cleanup               ONE order per holding, through M8
          |
    broker observation               the account no longer reports it
          |
    reconciliation                   the orphan finding is gone
          |
    immutable cleanup run            what was targeted, sent, filled, observed

Two properties are asserted at every step and neither is computed by this
system: the broker's own submitted-order counter, and what the broker says the
account holds. Everything else in the loop is a claim about those two.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

# Constants and helpers only. The *fixtures* this suite uses are registered in
# ``tests/integration/conftest.py``, so their names stay out of this module —
# imported here they would shadow the parameters that request them.
from tests.cleanup.conftest import (
    ORPHAN_CALL_ID,
    ORPHAN_CALL_KEY,
    ORPHAN_PUT_ID,
    WiredCleanup,
    orphan_keys,
    orphan_position,
)

from trading_system.broker.base import BrokerTimeoutError
from trading_system.cleanup.models import CleanupOutcomeStatus, CleanupRunStatus
from trading_system.domain.enums import ExecutionIntent, OptionRight, ReconciliationRunStatus
from trading_system.infrastructure.settings import SystemConfig

pytestmark = pytest.mark.integration


@pytest.fixture
def four_orphans(make_service, cleanup_enabled_config: SystemConfig) -> WiredCleanup:
    """Four pre-existing holdings, shaped like the ones actually found.

    Two calls and two puts on one underlying, all long one contract. They
    *look* like a straddle and a strangle, and nothing recorded that they are:
    the cleanup closes four independent holdings and asserts no structure,
    which is the whole point of the arrangement.
    """
    service: WiredCleanup = make_service(
        [
            orphan_position(contract_id=ORPHAN_CALL_ID, strike=Decimal("540.00")),
            orphan_position(
                contract_id=ORPHAN_PUT_ID,
                strike=Decimal("545.00"),
                right=OptionRight.PUT,
                market_value=Decimal("103.00"),
            ),
            orphan_position(contract_id=903223753, strike=Decimal("542.50")),
            orphan_position(
                contract_id=903224246,
                strike=Decimal("542.50"),
                right=OptionRight.PUT,
                market_value=Decimal("92.89"),
            ),
        ],
        config=cleanup_enabled_config,
    )
    return service


# ---------------------------------------------------------------------------
# The happy path, end to end
# ---------------------------------------------------------------------------
def test_the_whole_loop(four_orphans: WiredCleanup) -> None:
    service = four_orphans
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    # 1. Reconciliation reports four orphans and submits nothing.
    before = service.reconciliation.run()
    assert before.result.status is ReconciliationRunStatus.MISMATCH
    assert len(orphan_keys(before.result)) == 4
    assert before.result.orders_submitted == 0
    assert before.result.corrective_orders == 0

    # 2. A review shows all four and still submits nothing.
    review = service.run(authorized=False)
    assert len(review.plan.selection.targets) == 4
    assert review.run.orders_submitted == 0
    assert service.broker.orders_submitted == 0

    # 3. The authorised cleanup sends exactly one order per holding.
    outcome = service.run(authorized=True)
    assert outcome.run.orders_submitted == 4
    assert service.broker.orders_submitted == 4
    assert outcome.run.corrective_orders == 0

    # 4. Broker observation says the account holds none of them.
    assert all(item.observed_quantity_after == Decimal("0") for item in outcome.run.outcomes)
    assert {item.status for item in outcome.run.outcomes} == {CleanupOutcomeStatus.CLOSED}
    assert outcome.run.status is CleanupRunStatus.COMPLETE

    # 5. The following reconciliation no longer reports them.
    assert outcome.after is not None
    assert orphan_keys(outcome.after.result) == set()

    # 6. The original comparison is untouched.
    original = service.reconciliation.get(before.result.reconciliation_id)
    assert original is not None
    assert len(orphan_keys(original)) == 4


def test_no_structure_is_invented_from_holdings_that_look_like_one(
    four_orphans: WiredCleanup,
) -> None:
    """Four independent single-leg orders, never a combo nobody recorded."""
    service = four_orphans
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True)

    for submission in outcome.plan.submissions:
        assert submission.intent is not None
        assert len(submission.intent.legs) == 1
        assert submission.intent.strategy_type is None
    assert len({item.broker_order_id for item in outcome.run.outcomes}) == 4


def test_nothing_downstream_believes_the_positions_were_ours(
    four_orphans: WiredCleanup,
) -> None:
    service = four_orphans
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True
    service.run(authorized=True)

    # The internal position ledger is still empty: a cleanup establishes
    # nothing and ends nothing of ours.
    projection = service.reconciliation.positions.expected()
    assert projection.positions == ()
    assert projection.strategies == ()

    # No reservation moved, so no campaign capital was committed or returned.
    assert service.reconciliation.reservations.repository.all_current() == []

    # And every execution record says what it is.
    for entry in service.executions.repository.history():
        record = service.executions.repository.current(entry.execution_id)
        assert record is not None
        assert record.intent is ExecutionIntent.CLEANUP
        assert record.capital_commitment == Decimal("0")
        assert record.maximum_loss == Decimal("0")


def test_the_stored_run_answers_the_audit_questions(four_orphans: WiredCleanup) -> None:
    service = four_orphans
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True)
    stored = service.repository.get_run(outcome.run.run_id)

    assert stored is not None
    assert stored.source_reconciliation_id == outcome.run.source_reconciliation_id
    assert stored.result_reconciliation_id is not None
    assert stored.reason == "PRE_EXISTING_ORPHAN_POSITION"
    assert len(stored.outcomes) == 4
    assert stored.orders_submitted == 4
    assert stored.corrective_orders == 0
    assert any(gate.startswith("PASS TRADING_MODE_IS_PAPER") for gate in stored.gates)


# ---------------------------------------------------------------------------
# The unhappy paths, each end to end
# ---------------------------------------------------------------------------
def test_a_rejected_order_leaves_the_holding_and_reports_it(
    four_orphans: WiredCleanup,
) -> None:
    service = four_orphans
    service.broker.state.book.reject_next = "no trading permissions for this contract"

    outcome = service.run(authorized=True)

    rejected = [i for i in outcome.run.outcomes if i.status is CleanupOutcomeStatus.REJECTED]
    assert len(rejected) == 1
    assert outcome.after is not None
    # Every holding is still there: nothing filled.
    assert len(orphan_keys(outcome.after.result)) == 4


def test_a_timeout_is_uncertain_and_never_retried(four_orphans: WiredCleanup) -> None:
    service = four_orphans
    service.broker.state.book.raise_next = BrokerTimeoutError("no answer from the gateway")

    first = service.run(authorized=True)
    assert first.run.status is CleanupRunStatus.UNCERTAIN
    uncertain = [i for i in first.run.outcomes if i.status is CleanupOutcomeStatus.UNCERTAIN]
    assert len(uncertain) == 1

    submitted = service.broker.orders_submitted
    second = service.run(authorized=True)

    assert service.broker.orders_submitted == submitted, "an UNCERTAIN submission was retried"
    assert second.run.orders_submitted == 0
    # Two independent blocks now hold, and either alone would be enough: the
    # first attempt's order is working at the broker, and the ledger knows an
    # order for this holding has already reached it. Both are named.
    assert all(item.gate_failures or item.reason_codes for item in second.run.outcomes)
    assert any(
        "NO_WORKING_BROKER_ORDER" in failure
        for item in second.run.outcomes
        for failure in item.gate_failures
    )


def test_a_partial_fill_is_reported_and_not_continued(
    make_service, cleanup_enabled_config: SystemConfig
) -> None:
    service = make_service([orphan_position(quantity=Decimal("10"))], config=cleanup_enabled_config)
    service.broker.state.book.fill_on_submit = 4
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True)

    item = outcome.run.outcomes[0]
    assert item.status is CleanupOutcomeStatus.PARTIALLY_CLOSED
    assert item.filled_quantity == 4
    assert item.observed_quantity_after == Decimal("6")
    assert outcome.run.orders_submitted == 1

    # The holding is still an orphan and is still reported as one.
    assert outcome.after is not None
    assert orphan_keys(outcome.after.result) == {ORPHAN_CALL_KEY}


def test_running_it_twice_over_a_clean_account_changes_nothing(
    four_orphans: WiredCleanup,
) -> None:
    service = four_orphans
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    service.run(authorized=True)
    runs_after_first = len(service.repository.history())
    orders_after_first = service.broker.orders_submitted

    second = service.run(authorized=True)

    assert service.broker.orders_submitted == orders_after_first
    assert second.run.status is CleanupRunStatus.NO_TARGETS
    # Nothing happened, so nothing immutable was written about it.
    assert len(service.repository.history()) == runs_after_first
