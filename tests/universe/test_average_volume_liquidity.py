"""The liquidity floor reads the average, and only the average.

``config/universe.yaml`` names the threshold ``min_average_daily_volume``. It
now evaluates the field that actually means that — IBKR tick 21 (``avVolume``,
via generic tick 165) — rather than the current session's cumulative volume,
which was never what the threshold asked about and which IBKR's delayed feed
corrupts (tick 74; see ``tests/broker/test_ibkr_average_volume.py`` for the
wire capture).

The rule with teeth: **there is no fallback**. A missing average is an explicit
``VOLUME_UNAVAILABLE``, never zero, never "close enough", and never quietly
answered from ``volume``.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from trading_system.data.repository import FilesystemDataRepository
from trading_system.domain.enums import (
    DataQualityIssue,
    UniverseEligibility,
    UniverseRejectionReason,
)
from trading_system.universe.features import EvidenceGatherer
from trading_system.universe.filters import PreFilter

from .conftest import UNIVERSE_NOW

pytestmark = pytest.mark.unit

#: The SPY values captured off the wire on 2026-08-15.
CORRUPT_SESSION_VOLUME = Decimal("31367915626456")
CLEAN_AVERAGE_VOLUME = Decimal("52014430")


def _filter(make_universe_config: Callable[..., object], **kwargs: object) -> PreFilter:
    config = make_universe_config(**kwargs)
    return PreFilter(config.universe.filters)  # type: ignore[attr-defined]


def _evaluate(repo: FilesystemDataRepository, pre_filter: PreFilter, symbol: str = "SPY"):
    evidence = EvidenceGatherer(repo).gather(symbol, UNIVERSE_NOW)
    return pre_filter.evaluate(symbol, evidence, UNIVERSE_NOW)


# ---------------------------------------------------------------------------
# E. The threshold itself
# ---------------------------------------------------------------------------
def test_an_average_at_or_above_the_minimum_is_eligible(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", average_daily_volume=Decimal("1000000"))
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, min_volume=1_000_000))

    assert outcome.rejection is None
    assert outcome.candidate is not None
    assert outcome.candidate.deterministic_eligibility is UniverseEligibility.ELIGIBLE
    assert outcome.candidate.average_daily_volume == Decimal("1000000")


def test_an_average_below_the_minimum_is_rejected(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", average_daily_volume=Decimal("999999"))
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, min_volume=1_000_000))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.VOLUME_BELOW_MINIMUM


def test_the_candidate_records_both_volumes_separately(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """An audit has to be able to see what was gated on *and* what was reported."""
    store_quote(
        "SPY",
        volume=CORRUPT_SESSION_VOLUME,
        average_daily_volume=CLEAN_AVERAGE_VOLUME,
    )
    store_chain("SPY")

    outcome = _evaluate(
        data_repo,
        _filter(make_universe_config, min_volume=1_000_000, require_research_usable=False),
    )

    assert outcome.candidate is not None
    assert outcome.candidate.average_daily_volume == CLEAN_AVERAGE_VOLUME
    assert outcome.candidate.underlying_volume == CORRUPT_SESSION_VOLUME


# ---------------------------------------------------------------------------
# D + F. A corrupt session volume decides nothing about the average
# ---------------------------------------------------------------------------
def test_a_suspicious_session_volume_does_not_reject_a_valid_average(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """Requirement F, at the liquidity gate.

    The exact SPY pair from the capture: a session volume that cannot be real
    beside an average that plainly is. The liquidity check must reach its
    verdict from the second and ignore the first.
    """
    store_quote(
        "SPY",
        volume=CORRUPT_SESSION_VOLUME,
        average_daily_volume=CLEAN_AVERAGE_VOLUME,
        issues=[DataQualityIssue.SUSPICIOUS_VOLUME],
        research_usable=False,
    )
    store_chain("SPY")

    outcome = _evaluate(
        data_repo,
        _filter(make_universe_config, min_volume=1_000_000, require_research_usable=False),
    )

    assert outcome.rejection is None, (
        "a flagged session volume must not veto a sound average daily volume"
    )
    assert outcome.candidate is not None
    assert outcome.candidate.average_daily_volume == CLEAN_AVERAGE_VOLUME


def test_a_missing_average_is_unavailable_even_beside_a_huge_session_volume(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """Requirement D: never `average_daily_volume or volume`."""
    store_quote("SPY", volume=CORRUPT_SESSION_VOLUME, average_daily_volume=None)
    store_chain("SPY")

    outcome = _evaluate(
        data_repo,
        _filter(make_universe_config, min_volume=1_000_000, require_research_usable=False),
    )

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.VOLUME_UNAVAILABLE
    assert "not a substitute" in (outcome.rejection.detail or "")


def test_a_missing_average_is_never_read_as_zero(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """Zero would be *below* the floor; unavailable is a different fact.

    With the floor switched off entirely, a zero reading and an absent reading
    become distinguishable: absence must survive as `None` rather than having
    been silently materialised as a number somewhere upstream.
    """
    store_quote("SPY", average_daily_volume=None)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config, min_volume=0))

    assert outcome.candidate is not None
    assert outcome.candidate.average_daily_volume is None


# ---------------------------------------------------------------------------
# G. Point-in-time
# ---------------------------------------------------------------------------
def test_the_average_obeys_the_point_in_time_rule(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    """A quote retrieved after the instant is invisible, average included.

    The new field rides the existing snapshot machinery rather than a path of
    its own, so there is no way for it to arrive from the future while the
    prices beside it do not.
    """
    later = UNIVERSE_NOW.replace(year=UNIVERSE_NOW.year + 1)
    store_quote("SPY", as_of=later, retrieved_at=later, average_daily_volume=CLEAN_AVERAGE_VOLUME)
    store_chain("SPY")

    outcome = _evaluate(data_repo, _filter(make_universe_config))

    assert outcome.rejection is not None
    assert outcome.rejection.reason is UniverseRejectionReason.DATA_UNAVAILABLE


def test_the_average_visible_at_t_is_the_one_stored_at_t(
    data_repo, store_quote, store_chain, make_universe_config
) -> None:
    store_quote("SPY", average_daily_volume=CLEAN_AVERAGE_VOLUME)
    store_chain("SPY")

    evidence = EvidenceGatherer(data_repo).gather("SPY", UNIVERSE_NOW)

    assert evidence.quote is not None
    assert evidence.quote.average_daily_volume == CLEAN_AVERAGE_VOLUME
