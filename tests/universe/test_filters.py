"""The deterministic pre-filter (Milestone 4 brief section 31).

Every case the brief enumerates gets a test, and each asserts the *machine-
readable reason*, not merely that the asset was dropped. A filter that rejects
the right assets for unrecorded reasons is unauditable: "why was AAPL not
researched on 10 August" has to be answerable from the stored run.

The distinction these tests protect hardest is the one between *unavailable*
and *below threshold*. ``PRICE_UNAVAILABLE`` and ``PRICE_BELOW_MINIMUM`` are
separate outcomes because "we have no price" and "the price is too low" are
different facts, and a filter that collapsed them would let a data gap look
like a judgement about the asset.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.data.repository import FilesystemDataRepository
from trading_system.domain.enums import (
    DataQualityIssue,
    Optionability,
    SecurityType,
    UniverseEligibility,
    UniverseRejectionReason,
)
from trading_system.infrastructure.settings import OptionabilityPolicy
from trading_system.universe.features import EvidenceGatherer
from trading_system.universe.filters import PreFilter
from trading_system.universe.models import PriceField

from .conftest import UNIVERSE_NOW

pytestmark = pytest.mark.unit


def _filter(make_universe_config: Callable[..., object], **kwargs: object) -> PreFilter:
    config = make_universe_config(**kwargs)
    return PreFilter(config.universe.filters)  # type: ignore[attr-defined]


def _evaluate(
    repo: FilesystemDataRepository,
    pre_filter: PreFilter,
    symbol: str = "SPY",
):
    """Gather evidence through the real repository, then filter it."""
    evidence = EvidenceGatherer(repo).gather(symbol, UNIVERSE_NOW)
    return pre_filter.evaluate(symbol, evidence, UNIVERSE_NOW)


# ---------------------------------------------------------------------------
# 11. The happy path — a valid candidate is accepted
# ---------------------------------------------------------------------------
def test_a_valid_candidate_is_accepted(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY")
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config))

    assert outcome.eligible
    candidate = outcome.candidate
    assert candidate is not None
    assert candidate.deterministic_eligibility is UniverseEligibility.ELIGIBLE
    assert candidate.optionability is Optionability.TRUE
    assert candidate.reference_price == Decimal("500.15")
    assert candidate.reference_price_field == PriceField.LAST
    assert candidate.underlying_volume == Decimal("75000000")
    assert candidate.source.provider == "IBKR"
    assert candidate.source.snapshot_ids, "a candidate must name the snapshots behind it"


def test_the_reference_price_records_which_field_it_came_from(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """A threshold comparison against an unnamed field cannot be audited later."""
    store_quote("SPY", last=None, close=None)
    store_chain("SPY")

    candidate = _evaluate(data_repo, _filter(make_universe_config)).candidate

    assert candidate is not None
    assert candidate.reference_price_field == PriceField.MID
    assert candidate.reference_price == Decimal("500.15")  # (500.10 + 500.20) / 2


# ---------------------------------------------------------------------------
# 1. Below minimum price
# ---------------------------------------------------------------------------
def test_a_price_below_the_minimum_is_rejected(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", last=Decimal("2.50"), close=Decimal("2.40"), bid=None, ask=None)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, min_price=Decimal("5.00")))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.PRICE_BELOW_MINIMUM
    assert "2.50" in (outcome.rejection.detail or "")


def test_an_unavailable_price_is_not_a_low_price(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """A quote with no price at all is a distinct outcome from a cheap one."""
    store_quote("SPY", last=None, close=None, bid=None, ask=None)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.PRICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# 2. Insufficient volume
# ---------------------------------------------------------------------------
def test_volume_below_the_minimum_is_rejected(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", average_daily_volume=Decimal("1000"))
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, min_volume=1_000_000))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.VOLUME_BELOW_MINIMUM


def test_an_unknown_volume_never_passes_a_liquidity_floor(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """None is not zero, and it is certainly not "enough"."""
    store_quote("SPY", average_daily_volume=None)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, min_volume=1_000_000))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.VOLUME_UNAVAILABLE


def test_a_healthy_session_volume_never_substitutes_for_a_missing_average(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """`min_average_daily_volume` asks about an average, so only the average answers.

    A session volume far above the floor is present and deliberately ignored:
    falling back to it would read a threshold about a 90-day average off a
    single day, and on IBKR's delayed feed that single day is the corrupted
    tick 74.
    """
    store_quote("SPY", volume=Decimal("500000000"), average_daily_volume=None)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, min_volume=1_000_000))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.VOLUME_UNAVAILABLE


def test_the_volume_rejection_names_underlying_liquidity_not_option_liquidity(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """Milestone 4 brief section 11: the two must never be conflated in wording."""
    store_quote("SPY", average_daily_volume=Decimal("1000"))
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, min_volume=1_000_000))

    detail = (outcome.rejection.detail or "").lower()
    assert "underlying" in detail
    assert "does not establish option liquidity" in detail


# ---------------------------------------------------------------------------
# 3. Stale data
# ---------------------------------------------------------------------------
def test_stale_data_is_rejected(data_repo, store_quote, store_chain, make_universe_config) -> None:
    old = UNIVERSE_NOW - timedelta(days=3)
    store_quote("SPY", as_of=old, retrieved_at=old)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, max_data_age_seconds=86_400))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.DATA_STALE


def test_data_within_the_window_survives(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    recent = UNIVERSE_NOW - timedelta(hours=2)
    store_quote("SPY", as_of=recent, retrieved_at=recent)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, max_data_age_seconds=86_400))

    assert outcome.eligible


# ---------------------------------------------------------------------------
# 4. research_usable is false
# ---------------------------------------------------------------------------
def test_a_record_the_quality_engine_rejected_never_reaches_research(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """The data layer flags, the universe layer gates. Neither corrects."""
    store_quote(
        "SPY",
        research_usable=False,
        issues=[DataQualityIssue.SUSPICIOUS_VOLUME],
    )
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.DATA_NOT_RESEARCH_USABLE
    assert "SUSPICIOUS_VOLUME" in (outcome.rejection.detail or "")


def test_research_usability_can_be_switched_off_by_policy(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", research_usable=False, issues=[DataQualityIssue.SUSPICIOUS_VOLUME])
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, require_research_usable=False))

    assert outcome.eligible
    assert outcome.candidate is not None
    assert outcome.candidate.data_quality.research_usable is False, (
        "the flag travels with the candidate even when the gate is open"
    )


# ---------------------------------------------------------------------------
# 5. Unsupported security type
# ---------------------------------------------------------------------------
def test_an_unsupported_security_type_is_rejected(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", security_type=SecurityType.FUTURE)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.SECURITY_TYPE_NOT_ALLOWED


# ---------------------------------------------------------------------------
# 6. Unsupported currency
# ---------------------------------------------------------------------------
def test_an_unsupported_currency_is_rejected(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", currency="EUR")
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, allowed_currencies=["USD"]))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.CURRENCY_NOT_ALLOWED


def test_a_missing_currency_is_rejected_rather_than_assumed(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", currency=None)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.CURRENCY_NOT_ALLOWED


def test_an_exchange_outside_the_allow_list_is_rejected(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", exchange="LSE")
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, allowed_exchanges=["ARCA"]))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.EXCHANGE_NOT_ALLOWED


# ---------------------------------------------------------------------------
# 7 & 8. Optionability
# ---------------------------------------------------------------------------
def test_an_empty_chain_means_not_optionable(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """A provider answering "no expirations" is evidence, and it is FALSE."""
    store_quote("SPY")
    store_chain("SPY", expirations=[], strikes=[])

    outcome = _evaluate(data_repo, _filter(make_universe_config))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.OPTIONABILITY_FALSE
    assert outcome.rejection.optionability is Optionability.FALSE


def test_unknown_optionability_is_rejected_when_the_policy_requires_it(
    data_repo, store_quote, make_universe_config
) -> None:
    store_quote("SPY")  # no chain stored at all

    outcome = _evaluate(
        data_repo,
        _filter(make_universe_config, optionability_policy=OptionabilityPolicy.REQUIRED),
    )

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.OPTIONABILITY_UNKNOWN
    assert outcome.rejection.optionability is Optionability.UNKNOWN


def test_unknown_optionability_passes_when_the_policy_allows_it(
    data_repo, store_quote, make_universe_config
) -> None:
    store_quote("SPY")

    outcome = _evaluate(
        data_repo,
        _filter(make_universe_config, optionability_policy=OptionabilityPolicy.UNKNOWN_ALLOWED),
    )

    assert outcome.eligible
    assert outcome.candidate is not None
    assert outcome.candidate.optionability is Optionability.UNKNOWN, (
        "the unresolved state travels with the candidate; it is never upgraded to TRUE"
    )


def test_a_missing_chain_is_never_read_as_not_optionable(
    data_repo, store_quote, make_universe_config
) -> None:
    """The rejection reason distinguishes "unknown" from "established false"."""
    store_quote("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is not UniverseRejectionReason.OPTIONABILITY_FALSE
    assert outcome.rejection.reason is UniverseRejectionReason.OPTIONABILITY_UNKNOWN


def test_an_ignored_policy_admits_both_unknown_and_false(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY")
    store_chain("SPY", expirations=[], strikes=[])

    outcome = _evaluate(
        data_repo,
        _filter(make_universe_config, optionability_policy=OptionabilityPolicy.IGNORED),
    )

    assert outcome.eligible
    assert outcome.candidate is not None
    assert outcome.candidate.optionability is Optionability.FALSE


# ---------------------------------------------------------------------------
# 9. Explicit exclusion
# ---------------------------------------------------------------------------
def test_an_excluded_symbol_is_rejected_before_data_is_consulted(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY")
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, exclusions=["SPY"]))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.EXCLUDED_BY_CONFIGURATION


def test_exclusions_are_case_insensitive(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY")
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, exclusions=["spy"]))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.EXCLUDED_BY_CONFIGURATION


# ---------------------------------------------------------------------------
# 10. Duplicate symbols
# ---------------------------------------------------------------------------
def test_a_duplicate_symbol_yields_one_canonical_candidate(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY")
    store_chain("SPY")
    pre_filter = _filter(make_universe_config)
    evidence = EvidenceGatherer(data_repo).gather_all(["SPY"], UNIVERSE_NOW)

    outcomes = pre_filter.apply(["SPY", "SPY", "spy"], evidence, UNIVERSE_NOW)

    eligible = [o for o in outcomes if o.eligible]
    duplicates = [
        o
        for o in outcomes
        if o.rejection is not None
        and o.rejection.reason is UniverseRejectionReason.DUPLICATE_SYMBOL
    ]
    assert len(eligible) == 1, "one canonical candidate"
    assert len(duplicates) == 2, "and the duplicates are recorded, not silently dropped"


# ---------------------------------------------------------------------------
# Missing data, and the candidate cap
# ---------------------------------------------------------------------------
def test_a_symbol_with_no_stored_data_is_rejected_as_unavailable(
    data_repo, make_universe_config
) -> None:
    outcome = _evaluate(data_repo, _filter(make_universe_config), symbol="ZZZZ")

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.DATA_UNAVAILABLE


def test_the_candidate_cap_is_enforced_visibly(
    data_repo, optionable_symbols, make_universe_config
) -> None:
    """Excess candidates are recorded as rejections, never quietly dropped."""
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA", "AAPL"])
    pre_filter = _filter(make_universe_config, max_candidates=2, max_selected=2)
    evidence = EvidenceGatherer(data_repo).gather_all(symbols, UNIVERSE_NOW)

    outcomes = pre_filter.apply(symbols, evidence, UNIVERSE_NOW)

    assert len([o for o in outcomes if o.eligible]) == 2
    capped = [
        o.symbol
        for o in outcomes
        if o.rejection is not None
        and o.rejection.reason is UniverseRejectionReason.CANDIDATE_LIMIT_EXCEEDED
    ]
    assert capped == ["NVDA", "AAPL"], "dropped in the source's own order, deterministically"


def test_every_rejection_carries_a_machine_readable_reason(
    data_repo, store_quote, make_universe_config
) -> None:
    """Brief section 31: no rejection may be explained only in prose."""
    store_quote("SPY", last=Decimal("1.00"), close=None, bid=None, ask=None)
    pre_filter = _filter(make_universe_config)
    evidence = EvidenceGatherer(data_repo).gather_all(["SPY", "ZZZZ"], UNIVERSE_NOW)

    outcomes = pre_filter.apply(["SPY", "ZZZZ"], evidence, UNIVERSE_NOW)

    for outcome in outcomes:
        assert outcome.rejection is not None
        assert isinstance(outcome.rejection.reason, UniverseRejectionReason)
        assert outcome.rejection.detail, "and a human-readable detail alongside it"


def test_the_filter_order_is_stable_for_an_asset_failing_several_rules(
    data_repo, store_quote, make_universe_config
) -> None:
    """An asset that fails many checks must always report the same first one."""
    store_quote("SPY", currency="EUR", last=Decimal("0.10"), close=None, volume=None)
    pre_filter = _filter(make_universe_config)

    reasons = {_evaluate(data_repo, pre_filter).rejection.reason for _ in range(5)}

    assert reasons == {UniverseRejectionReason.CURRENCY_NOT_ALLOWED}
