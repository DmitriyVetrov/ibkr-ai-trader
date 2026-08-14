"""Running the monitor twice changes nothing.

Exit evaluation runs on a schedule, so "what does the second run do" is not an
edge case — it is the normal case. The property here is economic as well as
tidy: a duplicate record is untidy, a duplicate *exit order* closes a position
twice, once at a price that was decided on and once at whatever the market does
next.

One subtlety the tests below are explicit about: the *first* run genuinely
changes something — it moves the position from ``OPEN`` (never looked at) to
``MONITORING``. That is a real state change and the artifacts should differ.
Idempotency is the claim about every run after it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.domain.enums import (
    ExecutionState,
    ExitDecisionType,
    ExitReasonCode,
    PositionLifecycleState,
)

pytestmark = pytest.mark.integration

_S = PositionLifecycleState


# ---------------------------------------------------------------------------
# The artifacts
# ---------------------------------------------------------------------------
def test_a_repeated_evaluation_produces_an_identical_decision(open_long_call) -> None:
    """Runs two and three, over unchanged inputs, agree byte for byte."""
    service, _ = open_long_call
    service.monitor()

    second = service.monitor()
    third = service.monitor()

    assert second.result.decisions[0].decision_id == third.result.decisions[0].decision_id
    assert second.result.evaluations[0].content_hash == third.result.evaluations[0].content_hash
    assert second.result.decisions[0].model_dump(mode="json") == third.result.decisions[
        0
    ].model_dump(mode="json")


def test_the_first_run_is_new_and_the_next_is_a_re_observation(open_long_call, exit_repo) -> None:
    """Not a second judgement that happens to agree."""
    service, _ = open_long_call
    service.monitor()

    second = service.monitor()
    third = service.monitor()

    assert second.outcomes[0].is_new is True
    assert third.outcomes[0].is_new is False
    entries = exit_repo.history()
    assert sum(1 for entry in entries if entry.reobserved) == 1


def test_a_repeated_run_lands_on_the_same_run_id(open_long_call) -> None:
    """The immutable store would refuse a second record under a colliding id if
    the run had genuinely changed; an unchanged re-run is the same event."""
    service, _ = open_long_call
    service.monitor()

    second = service.monitor()
    third = service.monitor()

    assert second.result.run_id == third.result.run_id


def test_a_changed_price_produces_a_new_judgement(
    build_exit_service, data_repo, stored_research
) -> None:
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    service.monitor()
    settled = service.monitor()

    # A later ``as_of`` as well as a different price: re-storing content at the
    # same instant is a re-observation, and the repository would keep returning
    # the earlier snapshot.
    later = NOW + timedelta(minutes=5)
    factories.store_quotes(
        data_repo,
        [factories.option_quote(bid=Decimal("6.90"), ask=Decimal("7.10"), as_of=later)],
        as_of=later,
    )
    moved = service.monitor(as_of=later)

    assert settled.result.evaluations[0].content_hash != moved.result.evaluations[0].content_hash
    assert moved.outcomes[0].is_new is True
    assert moved.result.decisions[0].exit_quote == Decimal("6.90")


def test_the_trailing_state_does_not_advance_on_an_unchanged_re_run(
    build_exit_service, data_repo, exit_repo, stored_research
) -> None:
    """The peak and the level are the same, and the level never moves down."""
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("9.00"), ask=Decimal("9.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    position_id = service.open_positions()[0].position_id

    service.monitor()
    after_first = exit_repo.trailing(position_id)
    service.monitor()
    after_second = exit_repo.trailing(position_id)

    assert after_first is not None and after_second is not None
    assert after_second.peak_quote == after_first.peak_quote
    assert after_second.stop_quote == after_first.stop_quote


def test_a_replayed_lifecycle_event_is_recognised_and_dropped(open_long_call, exit_repo) -> None:
    service, _ = open_long_call
    service.monitor()
    position_id = service.open_positions()[0].position_id
    events = exit_repo.lifecycle_events(position_id)

    appended = exit_repo.append_lifecycle_event(events[0])

    assert appended is False
    assert len(exit_repo.lifecycle_events(position_id)) == len(events)


# ---------------------------------------------------------------------------
# The submission
# ---------------------------------------------------------------------------
def test_a_position_with_a_working_exit_is_never_sent_another(
    build_exit_service, data_repo, drive_lifecycle, stored_research
) -> None:
    """The lifecycle refuses before anything is built."""
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("2.00"), ask=Decimal("2.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    drive_lifecycle(service.open_positions()[0], _S.EXIT_REQUIRED, _S.EXIT_SUBMITTED)

    run = service.monitor()
    outcome = run.outcomes[0]

    assert outcome.decision.decision is ExitDecisionType.WAIT
    assert outcome.decision.primary_reason is ExitReasonCode.EXIT_ALREADY_SUBMITTED
    assert service.build_request(outcome, at=NOW) is None
    assert run.orders_submitted == 0


def test_an_unknown_exit_blocks_and_no_request_is_built(
    build_exit_service, data_repo, drive_lifecycle, stored_research
) -> None:
    """The most important idempotency case: the order may be live right now."""
    factories.store_quotes(
        data_repo, [factories.option_quote(bid=Decimal("2.00"), ask=Decimal("2.20"))]
    )
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    drive_lifecycle(
        service.open_positions()[0], _S.EXIT_REQUIRED, _S.EXIT_SUBMITTED, _S.EXIT_UNKNOWN
    )

    run = service.monitor()
    outcome = run.outcomes[0]

    assert outcome.decision.decision is ExitDecisionType.BLOCK
    assert outcome.decision.primary_reason is ExitReasonCode.EXIT_OUTCOME_UNKNOWN
    assert service.build_request(outcome, at=NOW) is None
    assert run.orders_submitted == 0


def test_an_unknown_exit_is_never_reclassified_by_the_passage_of_time(
    build_exit_service, data_repo, drive_lifecycle, stored_research
) -> None:
    """No elapsed time turns "we do not know" into "nothing was sent"."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    position = service.open_positions()[0]
    drive_lifecycle(position, _S.EXIT_REQUIRED, _S.EXIT_SUBMITTED, _S.EXIT_UNKNOWN)

    for days in (1, 7, 30):
        service.monitor(as_of=NOW + timedelta(days=days))

    lifecycle = service.lifecycle(position.position_id)
    assert lifecycle is not None
    assert lifecycle.state is _S.EXIT_UNKNOWN


def test_an_unknown_exit_the_broker_still_holds_becomes_blocked_on_confirmation(
    build_exit_service, data_repo, drive_lifecycle, stored_research
) -> None:
    """Resolution is by observation, and observing that the position is still
    there is not evidence that nothing was sent."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )
    drive_lifecycle(
        service.open_positions()[0], _S.EXIT_REQUIRED, _S.EXIT_SUBMITTED, _S.EXIT_UNKNOWN
    )

    confirmed = service.confirm()

    assert len(confirmed) == 1
    assert confirmed[0].state is _S.BLOCKED
    assert confirmed[0].blocked_reason is ExitReasonCode.EXIT_OUTCOME_UNKNOWN


def test_an_exit_that_filled_closes_the_position_on_broker_evidence(
    build_exit_service, data_repo, drive_lifecycle, stored_research
) -> None:
    """Only broker reality closes a position — not a fill report, not a
    submission, not a decision."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot([]),
    )
    position = service.open_positions()[0]
    drive_lifecycle(position, _S.EXIT_REQUIRED, _S.EXIT_SUBMITTED)

    confirmed = service.confirm()

    assert len(confirmed) == 1
    assert confirmed[0].state is _S.CLOSED
    assert confirmed[0].open_quantity == 0


# ---------------------------------------------------------------------------
# The reservation ledger is not moved by an exit
# ---------------------------------------------------------------------------
def test_a_close_execution_is_excluded_from_reservation_accounting() -> None:
    """An exit's fills are proceeds, not a second commitment of the same money.

    Consuming the reservation again would double-count it; releasing it would
    return capital with no realised profit and loss behind the figure, which is
    Milestone 11's, not this one's. The service filters ``CLOSE`` records out,
    and this asserts that the filter is what stands between the two.
    """
    from trading_system.domain.enums import ExecutionIntent
    from trading_system.infrastructure.settings import ReservationPolicyConfig
    from trading_system.reservations.lifecycle import resolve_reservation
    from trading_system.reservations.models import Reservation, reservation_identifier

    entry = factories.entry_execution()
    closing = entry.model_copy(
        update={
            "execution_id": "execution-exit-1",
            "execution_request_id": "exit-req-1",
            "intent": ExecutionIntent.CLOSE,
            "position_id": "strategypos-1",
            "capital_commitment": Decimal("0"),
            "maximum_loss": Decimal("0"),
            "state": ExecutionState.FILLED,
        }
    )
    reservation = Reservation(
        reservation_id=reservation_identifier(
            campaign_id="campaign-001",
            allocation_id="allocation-1",
            opportunity_id="opportunity-1",
        ),
        campaign_id="campaign-001",
        allocation_id="allocation-1",
        opportunity_id="opportunity-1",
        symbol="NVDA",
        strategy=entry.strategy,
        currency="EUR",
        authorized_amount=Decimal("1200.00"),
        authorized_max_loss=Decimal("1200.00"),
        authorized_quantity=2,
        authorized_at=NOW,
        remaining_amount=Decimal("1200.00"),
        created_at=NOW,
        updated_at=NOW,
    )
    policy = ReservationPolicyConfig()

    assert closing.intent.establishes_position is False
    entry_only = resolve_reservation(reservation, [entry], policy=policy)
    filtered = [record for record in (entry, closing) if record.intent.establishes_position]

    assert filtered == [entry]
    assert resolve_reservation(reservation, filtered, policy=policy) == entry_only
