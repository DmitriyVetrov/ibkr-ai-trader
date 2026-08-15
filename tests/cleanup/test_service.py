"""What the cleanup service actually does, against the in-process simulator.

Every test here asserts the broker's own submitted-order counter, not a figure
this system computed. That is the difference between "we believe we sent
nothing" and "the thing that would have sent it was never asked".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.cleanup.conftest import (
    ORPHAN_CALL_ID,
    ORPHAN_CALL_KEY,
    ORPHAN_PUT_ID,
    WiredCleanup,
    orphan_position,
)
from trading_system.broker.base import BrokerTimeoutError
from trading_system.cleanup.models import CleanupOutcomeStatus, CleanupRunStatus
from trading_system.domain.enums import ExecutionIntent, ExecutionState, OptionRight
from trading_system.infrastructure.settings import SystemConfig

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The review path
# ---------------------------------------------------------------------------
def test_a_review_submits_nothing(service: WiredCleanup) -> None:
    outcome = service.run(authorized=False)

    assert outcome.run.status is CleanupRunStatus.DRY_RUN
    assert outcome.run.orders_submitted == 0
    # Read off the broker, not from our own record of what we did.
    assert service.broker.orders_submitted == 0


def test_a_review_shows_the_exact_orders_it_would_send(service: WiredCleanup) -> None:
    outcome = service.run(authorized=False)

    assert len(outcome.plan.submissions) == 2
    for submission in outcome.plan.submissions:
        assert submission.dry_run is True
        assert submission.intent is not None
        assert submission.record is not None
        assert submission.record.state is ExecutionState.VALIDATED
        assert submission.record.quantity == 1
        assert submission.intent.limit_price is not None


def test_a_review_stores_nothing(service: WiredCleanup) -> None:
    service.run(authorized=False)

    assert service.repository.history() == []
    assert service.repository.requests() == []
    assert service.executions.repository.history() == []


def test_a_review_constructs_no_writable_broker(
    make_service, cleanup_enabled_config: SystemConfig
) -> None:
    """Structural, not a flag: the review path never asks for one.

    The factory raises an ``AssertionError`` rather than a ``BrokerError``,
    deliberately — the execution service catches the latter and turns it into a
    tidy status line, which would let this test pass while a connection had
    already been attempted.
    """
    calls: list[str] = []

    def never_called(*args: object, **kwargs: object) -> object:
        calls.append("build")
        raise AssertionError("a review must never construct a broker for submission")

    service = make_service([orphan_position()], config=cleanup_enabled_config)
    service.executions._broker_factory = never_called

    outcome = service.run(authorized=False)

    assert calls == [], "the writable broker factory was called during a review"
    assert outcome.run.orders_submitted == 0


# ---------------------------------------------------------------------------
# The authorised path
# ---------------------------------------------------------------------------
def test_an_authorised_cleanup_sends_one_order_per_holding(service: WiredCleanup) -> None:
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True)

    assert outcome.run.orders_submitted == 2
    assert service.broker.orders_submitted == 2
    assert {o.status for o in outcome.run.outcomes} == {CleanupOutcomeStatus.CLOSED}
    assert outcome.run.status is CleanupRunStatus.COMPLETE


def test_only_broker_observation_closes_a_target(service: WiredCleanup) -> None:
    """A fill report alone leaves the target WORKING, not CLOSED."""
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = False  # the broker still reports it

    outcome = service.run(authorized=True)

    assert {o.status for o in outcome.run.outcomes} == {CleanupOutcomeStatus.WORKING}
    assert all(o.observed_quantity_after == Decimal("1") for o in outcome.run.outcomes)
    assert outcome.run.status is not CleanupRunStatus.COMPLETE


def test_the_order_sells_exactly_what_the_broker_holds(service: WiredCleanup) -> None:
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True)

    for item in outcome.run.outcomes:
        assert item.requested_quantity == int(item.observed_quantity_before)
        assert item.filled_quantity <= item.requested_quantity


def test_the_reconciliation_after_no_longer_reports_the_orphans(
    service: WiredCleanup,
) -> None:
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True)

    assert outcome.after is not None
    assert outcome.before is not None
    from tests.cleanup.conftest import orphan_keys

    assert orphan_keys(outcome.before.result) == {ORPHAN_CALL_KEY, f"cid:{ORPHAN_PUT_ID}"}
    assert orphan_keys(outcome.after.result) == set()
    # And the original comparison is untouched on disk.
    stored = service.reconciliation.get(outcome.run.source_reconciliation_id)
    assert stored is not None
    assert orphan_keys(stored) == {ORPHAN_CALL_KEY, f"cid:{ORPHAN_PUT_ID}"}


# ---------------------------------------------------------------------------
# What a cleanup must never touch
# ---------------------------------------------------------------------------
def test_cleanup_does_not_adopt_position(service: WiredCleanup) -> None:
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True
    service.run(authorized=True)

    records = service.executions.repository.history()
    assert records, "the cleanup should have written execution records"
    for entry in records:
        record = service.executions.repository.current(entry.execution_id)
        assert record is not None
        assert record.intent is ExecutionIntent.CLEANUP
        assert record.allocation_id is None
        assert record.purchase_card_id is None
        assert record.risk_decision_id is None
        assert record.opportunity_id is None
        assert record.strategy is None


def test_cleanup_creates_no_expected_position(service: WiredCleanup) -> None:
    """The load-bearing exclusion: its fills stay out of the internal ledger.

    Netting them in would manufacture an expected position of minus one for a
    contract this system never expected to hold, and the very next
    reconciliation would report that invention as a discrepancy.
    """
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True
    service.run(authorized=True)

    projection = service.reconciliation.positions.expected()
    assert projection.positions == ()
    assert projection.strategies == ()


def test_cleanup_does_not_mutate_campaign_budget(service: WiredCleanup) -> None:
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True
    service.run(authorized=True)

    reservations = service.reconciliation.reservations
    assert reservations.repository.all_current() == []


def test_cleanup_does_not_create_fake_pnl(service: WiredCleanup, tmp_path) -> None:
    """No position lifecycle closed, so no realised result exists to compute."""
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True
    service.run(authorized=True)

    assert not (tmp_path / "data" / "pnl").exists()


def test_the_acquisition_provenance_stays_unknown(service: WiredCleanup) -> None:
    """Selling a holding does not make this system the one that bought it."""
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = False
    outcome = service.run(authorized=True)

    assert outcome.after is not None
    observed = outcome.after.capture.snapshot.positions
    assert observed
    assert {position.provenance.value for position in observed} == {"UNKNOWN"}


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_already_closed_is_idempotent(service: WiredCleanup) -> None:
    """The second run finds nothing to close and sends nothing."""
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    first = service.run(authorized=True)
    assert first.run.orders_submitted == 2

    before = service.broker.orders_submitted
    second = service.run(authorized=True)

    assert second.run.orders_submitted == 0
    assert service.broker.orders_submitted == before
    assert second.run.status is CleanupRunStatus.NO_TARGETS


def test_a_second_run_while_the_position_persists_sends_no_second_order(
    service: WiredCleanup,
) -> None:
    """The ledger check, independent of broker observation.

    The holding is still reported (the fill has not propagated), so selection
    still targets it — and the execution ledger refuses, because an order for
    this contract has already reached the broker once.
    """
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = False

    first = service.run(authorized=True)
    assert first.run.orders_submitted == 2

    second = service.run(authorized=True)

    assert second.run.orders_submitted == 0
    assert service.broker.orders_submitted == 2
    codes = {code.value for item in second.run.outcomes for code in item.reason_codes}
    assert "ALREADY_SUBMITTED" in codes


def test_an_unfilled_working_order_blocks_a_second_submission(
    service: WiredCleanup,
) -> None:
    """Acknowledged and unfilled is still an order that may fill."""
    first = service.run(authorized=True)
    assert first.run.orders_submitted == 2

    second = service.run(authorized=True)
    assert second.run.orders_submitted == 0
    assert service.broker.orders_submitted == 2


# ---------------------------------------------------------------------------
# The unhappy paths
# ---------------------------------------------------------------------------
def test_unknown_blocks_retry(service: WiredCleanup) -> None:
    """A submission whose outcome was never learned is never re-sent."""
    service.broker.state.book.raise_next = BrokerTimeoutError("the broker never answered")

    first = service.run(authorized=True)

    uncertain = [o for o in first.run.outcomes if o.status is CleanupOutcomeStatus.UNCERTAIN]
    assert uncertain, "a timeout after submission must be UNCERTAIN, never FAILED"
    assert first.run.status is CleanupRunStatus.UNCERTAIN

    submitted = service.broker.orders_submitted
    second = service.run(authorized=True)

    assert second.run.orders_submitted == 0
    assert service.broker.orders_submitted == submitted, "an UNKNOWN submission was retried"


def test_a_broker_rejection_is_reported_as_rejected_not_refused(
    service: WiredCleanup,
) -> None:
    """The attempt reached the broker; a gate refusal never does."""
    service.broker.state.book.reject_next = "insufficient permissions for this contract"

    outcome = service.run(authorized=True)

    rejected = [o for o in outcome.run.outcomes if o.status is CleanupOutcomeStatus.REJECTED]
    assert rejected
    assert "insufficient permissions" in (rejected[0].detail or "")
    assert rejected[0].orders_submitted == 1
    # The broker's own counter, and no second attempt against the same holding.
    assert outcome.run.orders_submitted == service.broker.orders_submitted


def test_partial_fill(make_service, cleanup_enabled_config: SystemConfig) -> None:
    """Four of ten is four. The remainder is reported and nothing else is sent."""
    service = make_service([orphan_position(quantity=Decimal("10"))], config=cleanup_enabled_config)
    service.broker.state.book.fill_on_submit = 4
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True)

    item = outcome.run.outcomes[0]
    assert item.status is CleanupOutcomeStatus.PARTIALLY_CLOSED
    assert item.requested_quantity == 10
    assert item.filled_quantity == 4
    assert item.observed_quantity_after == Decimal("6")
    assert outcome.run.orders_submitted == 1
    assert service.broker.orders_submitted == 1


def test_a_partial_fill_is_not_continued_automatically(
    make_service, cleanup_enabled_config: SystemConfig
) -> None:
    service = make_service([orphan_position(quantity=Decimal("10"))], config=cleanup_enabled_config)
    service.broker.state.book.fill_on_submit = 4
    service.broker.state.net_fills_into_positions = True
    service.run(authorized=True)

    submitted = service.broker.orders_submitted
    # The holding is now 6 where the reconciliation reported 10, so selection
    # refuses it outright: the account moved after the report an operator read.
    second = service.run(authorized=True)

    assert second.run.orders_submitted == 0
    assert service.broker.orders_submitted == submitted


# ---------------------------------------------------------------------------
# The switches
# ---------------------------------------------------------------------------
def test_a_disabled_configuration_submits_nothing_even_when_authorised(
    make_service, cleanup_disabled_config: SystemConfig
) -> None:
    service = make_service([orphan_position()], config=cleanup_disabled_config)

    outcome = service.run(authorized=True)

    assert outcome.run.orders_submitted == 0
    assert service.broker.orders_submitted == 0
    assert any("CLEANUP_ENABLED" in gate for gate in outcome.run.gates if gate.startswith("FAIL"))


def test_a_refused_run_still_records_every_gate(
    make_service, cleanup_disabled_config: SystemConfig
) -> None:
    """A run that submitted nothing is as worth auditing as one that did."""
    service = make_service([orphan_position()], config=cleanup_disabled_config)

    outcome = service.run(authorized=True)

    assert any(gate.startswith("PASS") for gate in outcome.run.gates)
    assert any(gate.startswith("FAIL") for gate in outcome.run.gates)
    assert outcome.stored is True


def test_an_account_with_no_orphans_targets_nothing(
    make_service, cleanup_enabled_config: SystemConfig
) -> None:
    service = make_service([], config=cleanup_enabled_config)

    outcome = service.run(authorized=True)

    assert outcome.run.status is CleanupRunStatus.NO_TARGETS
    assert outcome.run.orders_submitted == 0
    assert service.broker.orders_submitted == 0


def test_contract_ids_narrow_an_authorised_run(
    make_service, cleanup_enabled_config: SystemConfig
) -> None:
    service = make_service(
        [
            orphan_position(),
            orphan_position(
                contract_id=ORPHAN_PUT_ID, strike=Decimal("545.00"), right=OptionRight.PUT
            ),
        ],
        config=cleanup_enabled_config,
    )
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True, contract_ids=[ORPHAN_CALL_ID])

    assert outcome.run.orders_submitted == 1
    assert [item.contract_id for item in outcome.run.outcomes] == [ORPHAN_CALL_ID]


# ---------------------------------------------------------------------------
# The audit artifact
# ---------------------------------------------------------------------------
def test_an_authorised_run_stores_its_request_and_its_result(
    service: WiredCleanup,
) -> None:
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    outcome = service.run(authorized=True)

    assert outcome.stored is True
    stored = service.repository.get_run(outcome.run.run_id)
    assert stored is not None
    assert stored.cleanup_request_id == outcome.run.cleanup_request_id
    assert stored.result_reconciliation_id is not None

    request = service.repository.get_request(stored.cleanup_request_id)
    assert request is not None
    assert {target.key for target in request.targets} == {
        ORPHAN_CALL_KEY,
        f"cid:{ORPHAN_PUT_ID}",
    }


def test_the_artifact_answers_every_audit_question(service: WiredCleanup) -> None:
    service.broker.state.book.fill_on_submit = 1
    service.broker.state.net_fills_into_positions = True

    run = service.run(authorized=True).run
    item = run.outcomes[0]

    assert run.source_reconciliation_id  # which reconciliation identified it
    assert run.result_reconciliation_id  # what the following one concluded
    assert run.cleanup_request_id  # what authorised it
    assert run.reason == "PRE_EXISTING_ORPHAN_POSITION"  # why
    assert item.key and item.contract_id  # what was targeted
    assert item.execution_id and item.order_intent_id  # what order was built
    assert item.broker_order_id  # what the broker called it
    assert item.filled_quantity  # what filled
    assert item.observed_quantity_after == Decimal("0")  # the final broker position
