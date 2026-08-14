"""Point-in-time safety: a future fact never influences an exit decision.

The same rule Milestone 3 established, applied to the stage that acts on it.
*Retrieval binds*: a quote we had not fetched was not a quote we had, however
recent the price it describes. A decision made from a leaked record would look
exactly like a good one, which is why a leak stops the evaluation rather than
being quietly filtered out.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.data.point_in_time import LookAheadError
from trading_system.domain.enums import ExitDecisionType, ExitQuoteField, ExitReasonCode
from trading_system.exit.valuation import ExitQuoteReader, value_position

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
def test_a_quote_retrieved_after_the_instant_is_invisible(data_repo) -> None:
    """Retrieval binds, not the price the quote describes."""
    later = NOW + timedelta(hours=1)
    factories.store_quotes(
        data_repo,
        [factories.option_quote(as_of=later, retrieved_at=later)],
        as_of=later,
        retrieved_at=later,
    )

    lookup = ExitQuoteReader(data_repo).read("NVDA", NOW)

    assert lookup.by_contract_id in (None, {})
    assert lookup.snapshot_id is None


def test_an_expiration_after_the_instant_is_not_look_ahead(data_repo) -> None:
    """Future-dated *content* is ordinary: an option expiring next month is
    exactly what a chain is for."""
    factories.store_quotes(data_repo, [factories.option_quote()])

    lookup = ExitQuoteReader(data_repo).read("NVDA", NOW)

    assert lookup.by_contract_id
    quote = lookup.by_contract_id[factories.CALL_CONTRACT_ID]
    assert quote.contract.expiration is not None
    assert quote.contract.expiration > NOW.date()


def test_a_stored_record_that_was_not_knowable_raises_rather_than_being_dropped(
    data_repo,
) -> None:
    """A look-ahead leak is a correctness bug in storage, not a market outcome.

    Quietly shortening the list would let a decision be made from the records
    that happened to be visible, and nothing would say the set was wrong.
    """
    from trading_system.data.point_in_time import assert_no_look_ahead

    future = factories.option_quote(
        as_of=NOW + timedelta(hours=1), retrieved_at=NOW + timedelta(hours=1)
    )

    with pytest.raises(LookAheadError):
        assert_no_look_ahead([future], NOW)


def test_a_leak_blocks_the_evaluation_rather_than_producing_a_decision(
    build_exit_service, data_repo, monkeypatch, stored_research
) -> None:
    """The service turns the leak into ``POINT_IN_TIME_ERROR`` and evaluates no
    policy against the leaked data."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=stored_research)],
        snapshot=factories.position_snapshot(),
    )

    def _leak(self, symbol, as_of):
        raise LookAheadError("a stored option quote was retrieved after the instant")

    monkeypatch.setattr(ExitQuoteReader, "read", _leak)

    run = service.monitor()
    decision = run.result.decisions[0]

    assert decision.decision is ExitDecisionType.BLOCK
    assert decision.primary_reason is ExitReasonCode.POINT_IN_TIME_ERROR
    assert len(run.result.evaluations[0].outcomes) == 1


def test_the_newest_visible_quote_wins_deterministically(data_repo) -> None:
    """Two quotes for one contract resolve on ``as_of`` then retrieval, never
    on whichever the filesystem returned first."""
    earlier = NOW - timedelta(hours=2)
    factories.store_quotes(
        data_repo,
        [factories.option_quote(as_of=earlier, retrieved_at=earlier, bid=Decimal("5.00"))],
        as_of=earlier,
        retrieved_at=earlier,
    )
    factories.store_quotes(data_repo, [factories.option_quote(bid=Decimal("6.50"))])

    lookup = ExitQuoteReader(data_repo).read("NVDA", NOW)

    assert lookup.by_contract_id is not None
    assert lookup.by_contract_id[factories.CALL_CONTRACT_ID].bid == Decimal("6.50")


def test_a_replay_of_a_past_instant_sees_what_was_visible_then(data_repo) -> None:
    """The property the whole point-in-time engine exists for."""
    earlier = NOW - timedelta(hours=2)
    factories.store_quotes(
        data_repo,
        [factories.option_quote(as_of=earlier, retrieved_at=earlier, bid=Decimal("5.00"))],
        as_of=earlier,
        retrieved_at=earlier,
    )
    factories.store_quotes(data_repo, [factories.option_quote(bid=Decimal("6.50"))])

    replay = ExitQuoteReader(data_repo).read("NVDA", earlier)

    assert replay.by_contract_id is not None
    assert replay.by_contract_id[factories.CALL_CONTRACT_ID].bid == Decimal("5.00")


# ---------------------------------------------------------------------------
# Freshness is measured against the evaluation instant, not wall clock
# ---------------------------------------------------------------------------
def test_a_quote_captured_at_the_evaluation_instant_has_age_zero(data_repo) -> None:
    """The rule ``risk.yaml`` records: the chain is anchored at one ``as_of``,
    so a historical replay is not penalised for being run today."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    lookup = ExitQuoteReader(data_repo).read("NVDA", NOW)

    valuation = value_position(
        [factories.held_leg()],
        lookup=lookup,
        as_of=NOW,
        quote_field=ExitQuoteField.BID,
        open_quantity=2,
        entry_quote=Decimal("6.00"),
        multiplier=100,
    )

    assert valuation.max_quote_age_seconds == 0.0


def test_a_quote_from_hours_ago_is_stale_at_the_evaluation_instant(data_repo) -> None:
    earlier = NOW - timedelta(hours=12)
    factories.store_quotes(
        data_repo,
        [factories.option_quote(as_of=earlier, retrieved_at=earlier)],
        as_of=earlier,
        retrieved_at=earlier,
    )
    lookup = ExitQuoteReader(data_repo).read("NVDA", NOW)

    valuation = value_position(
        [factories.held_leg()],
        lookup=lookup,
        as_of=NOW,
        quote_field=ExitQuoteField.BID,
        open_quantity=2,
        entry_quote=Decimal("6.00"),
        multiplier=100,
    )

    assert valuation.max_quote_age_seconds == 12 * 3600


# ---------------------------------------------------------------------------
# Research and contract metadata
# ---------------------------------------------------------------------------
def test_a_research_report_the_position_does_not_name_is_never_consulted(
    build_exit_service, data_repo, stored_research
) -> None:
    """The thesis comes from the report the *entry* named, not the newest one."""
    from trading_system.exit.service import ExitService

    factories.store_quotes(data_repo, [factories.option_quote()])
    service: ExitService = build_exit_service(
        executions=[factories.entry_execution(research_report_id="research-does-not-exist")],
        snapshot=factories.position_snapshot(),
    )

    run = service.monitor()

    assert run.result.decisions[0].primary_reason is ExitReasonCode.THESIS_DATA_UNAVAILABLE


def test_an_entry_naming_no_research_report_has_no_thesis_to_check(
    build_exit_service, data_repo, stored_research
) -> None:
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[factories.entry_execution(research_report_id=None)],
        snapshot=factories.position_snapshot(),
    )

    run = service.monitor()

    assert run.result.decisions[0].primary_reason is ExitReasonCode.THESIS_DATA_UNAVAILABLE


def test_a_future_dated_catalyst_cannot_invalidate_a_thesis_today() -> None:
    """An event that has not happened settles nothing."""
    from trading_system.domain.enums import ThesisConditionOutcome
    from trading_system.exit.thesis import ThesisView, check_conditions

    view = ThesisView(
        conditions=(("The NVDA results do not produce the move.", None),),
        catalysts=(("NVDA results", NOW + timedelta(days=10)),),
    )

    checks = check_conditions(view, at=NOW)

    assert checks[0].outcome is ThesisConditionOutcome.NOT_EVALUATED
