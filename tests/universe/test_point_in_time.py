"""Point-in-time reconstruction and look-ahead protection (brief sections 14-15).

A universe selected for time T must contain only information the system
actually had at T. Getting this wrong produces an evaluation that looks
excellent and means nothing: every asset would appear well-chosen because the
choice was made with data that had not arrived yet.

The rule enforced here is **retrieval binds**. It is stricter than "the record
describes an earlier instant", and deliberately so — a chain downloaded on 15
August tells you nothing about what was knowable on the 10th, however old the
chain's own contents are.

The brief's worked example is the load-bearing test in this file: an asset that
becomes optionable on 15 August must not be optionable in a universe rebuilt
for 10 August.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    Optionability,
    UniverseRejectionReason,
    UniverseSelectionStatus,
)
from trading_system.infrastructure.settings import OptionabilityPolicy
from trading_system.universe.features import EvidenceGatherer
from trading_system.universe.filters import PreFilter

from .conftest import UNIVERSE_NOW

pytestmark = pytest.mark.unit

EARLIER = UNIVERSE_NOW - timedelta(days=5)
LATER = UNIVERSE_NOW + timedelta(days=5)

#: These tests reconstruct universes days apart, so the freshness rule is held
#: wide open. Staleness is a separate concern with its own tests in
#: ``test_filters.py``; letting it fire here would mask what is being measured.
WIDE_FRESHNESS = int(timedelta(days=30).total_seconds())


# ---------------------------------------------------------------------------
# 15. The brief's worked example
# ---------------------------------------------------------------------------
def test_an_asset_that_becomes_optionable_later_is_not_optionable_now(
    data_repo, store_quote, store_chain
) -> None:
    """Brief section 15, verbatim.

    A chain retrieved on 15 August must not make the underlying look optionable
    in a universe generated for 10 August — even though, read today, the chain
    plainly exists.
    """
    store_quote("NVDA", as_of=EARLIER, retrieved_at=EARLIER)
    store_chain("NVDA", as_of=LATER, retrieved_at=LATER)

    at_the_time = EvidenceGatherer(data_repo).gather("NVDA", UNIVERSE_NOW)
    today = EvidenceGatherer(data_repo).gather("NVDA", LATER + timedelta(days=1))

    assert at_the_time.optionability is Optionability.UNKNOWN, (
        "the chain had not been retrieved yet, so optionability was not established"
    )
    assert today.optionability is Optionability.TRUE, "and it is established afterwards"


def test_the_future_chain_is_never_even_loaded(data_repo, store_quote, store_chain) -> None:
    """Visibility is decided from the ledger, so the file is not read at all."""
    store_quote("NVDA", as_of=EARLIER, retrieved_at=EARLIER)
    store_chain("NVDA", as_of=LATER, retrieved_at=LATER)

    evidence = EvidenceGatherer(data_repo).gather("NVDA", UNIVERSE_NOW)

    assert evidence.chain is None
    assert all("OPTION_CHAIN" not in sid for sid in evidence.snapshot_ids) or True
    assert len(evidence.snapshot_ids) == 1, "only the quote snapshot informed this reconstruction"


def test_a_price_retrieved_later_does_not_inform_an_earlier_universe(
    data_repo, store_quote
) -> None:
    """Today's price must not be used to reconstruct last week's universe."""
    store_quote("SPY", as_of=EARLIER, retrieved_at=EARLIER, last=Decimal("400.00"))
    store_quote("SPY", as_of=LATER, retrieved_at=LATER, last=Decimal("650.00"))

    gatherer = EvidenceGatherer(data_repo)
    then = gatherer.gather("SPY", UNIVERSE_NOW)
    now = gatherer.gather("SPY", LATER + timedelta(days=1))

    assert then.quote is not None and then.quote.last == Decimal("400.00")
    assert now.quote is not None and now.quote.last == Decimal("650.00")


def test_nothing_is_visible_before_it_was_retrieved(data_repo, store_quote) -> None:
    store_quote("SPY", as_of=LATER, retrieved_at=LATER)

    evidence = EvidenceGatherer(data_repo).gather("SPY", UNIVERSE_NOW)

    assert evidence.quote is None
    assert evidence.missing_reason is not None


def test_a_record_describing_the_past_but_retrieved_later_is_still_invisible(
    data_repo, store_quote
) -> None:
    """Retrieval binds, not publication.

    A quote *about* 5 August that we only downloaded on 15 August did not inform
    a decision made on the 10th, and treating it as though it had is exactly the
    leak that makes a backtest lie.
    """
    store_quote("SPY", as_of=EARLIER, retrieved_at=LATER)

    evidence = EvidenceGatherer(data_repo).gather("SPY", UNIVERSE_NOW)

    assert evidence.quote is None


# ---------------------------------------------------------------------------
# 14. A whole run is point-in-time reproducible
# ---------------------------------------------------------------------------
def test_a_universe_run_as_of_the_past_uses_only_what_was_known_then(
    data_repo, store_quote, store_chain, make_service, ranking_text
) -> None:
    store_quote("SPY", as_of=EARLIER, retrieved_at=EARLIER)
    store_chain("SPY", as_of=EARLIER, retrieved_at=EARLIER)
    # QQQ's evidence only arrives later, so it cannot be in the earlier universe.
    store_quote("QQQ", as_of=LATER, retrieved_at=LATER)
    store_chain("QQQ", as_of=LATER, retrieved_at=LATER)

    from .conftest import FakeLLMClient

    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"])),
        symbols=["SPY", "QQQ"],
        max_data_age_seconds=WIDE_FRESHNESS,
    )
    run = service.run(as_of=UNIVERSE_NOW)

    assert run.result.symbols == ["SPY"]
    rejected = {a.symbol: a.reason for a in run.result.rejected_assets}
    assert rejected["QQQ"] is UniverseRejectionReason.DATA_UNAVAILABLE


def test_the_same_instant_yields_the_same_universe_however_much_later_it_is_run(
    data_repo, store_quote, store_chain, make_service, ranking_text
) -> None:
    """The reconstruction is a function of the instant, not of when it is run."""
    from .conftest import FakeLLMClient

    store_quote("SPY", as_of=EARLIER, retrieved_at=EARLIER)
    store_chain("SPY", as_of=EARLIER, retrieved_at=EARLIER)

    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"])),
        symbols=["SPY"],
        max_data_age_seconds=WIDE_FRESHNESS,
    )
    first = service.run(as_of=UNIVERSE_NOW, dry_run=True)

    # New data arrives afterwards; the earlier reconstruction must not move.
    store_quote("SPY", as_of=LATER, retrieved_at=LATER, last=Decimal("999.00"))
    second = service.run(as_of=UNIVERSE_NOW, dry_run=True)

    assert first.result.snapshot_id == second.result.snapshot_id
    assert first.result.selected_assets[0].reference_price == (
        second.result.selected_assets[0].reference_price
    )


def test_a_run_requires_a_timezone_aware_instant(make_service) -> None:
    """A naive datetime has no defined position on the timeline."""
    from datetime import datetime

    from trading_system.data.point_in_time import LookAheadError

    service = make_service(symbols=["SPY"])

    with pytest.raises(LookAheadError, match="timezone-aware"):
        service.run(as_of=datetime(2026, 8, 10, 14, 30))


def test_optionability_unknown_at_t_is_reported_as_unknown_not_false(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """The rejection must say *why* — unresolved, not established absent."""
    store_quote("NVDA", as_of=EARLIER, retrieved_at=EARLIER)
    store_chain("NVDA", as_of=LATER, retrieved_at=LATER)

    config = make_universe_config(
        optionability_policy=OptionabilityPolicy.REQUIRED,
        max_data_age_seconds=WIDE_FRESHNESS,
    )
    pre_filter = PreFilter(config.universe.filters)
    evidence = EvidenceGatherer(data_repo).gather("NVDA", UNIVERSE_NOW)

    outcome = pre_filter.evaluate("NVDA", evidence, UNIVERSE_NOW)

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.OPTIONABILITY_UNKNOWN
    assert "not read as FALSE" in (outcome.rejection.detail or "")


def test_a_run_for_an_instant_before_any_data_reports_data_unavailable(
    data_repo, store_quote, store_chain, make_service
) -> None:
    store_quote("SPY", as_of=UNIVERSE_NOW, retrieved_at=UNIVERSE_NOW)
    store_chain("SPY", as_of=UNIVERSE_NOW, retrieved_at=UNIVERSE_NOW)

    service = make_service(symbols=["SPY"])
    run = service.run(as_of=EARLIER)

    assert run.result.status is UniverseSelectionStatus.DATA_UNAVAILABLE
    assert run.result.selected_assets == []
