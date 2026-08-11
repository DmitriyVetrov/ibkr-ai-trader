"""Deterministic post-validation of the agent's output (brief section 21).

Every check here answers the same question: *can this response be trusted?* The
answer is binary and the consequence is uniform — a violating response is
rejected in full, never partially accepted and never repaired.

That uniformity is the point. A validator that dropped the one bad row and kept
the rest would store a universe the model did not choose, while still recording
it as the model's output. The audit trail would be a fiction, and the fiction
would look exactly like a clean run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    ConfidenceLevel,
    DataQuality,
    Optionability,
    SecurityType,
    UniverseEligibility,
    UniverseSelectionReason,
)
from trading_system.universe.models import (
    CandidateAsset,
    CandidateProvenance,
    DataQualitySummary,
    FilterConfigSnapshot,
    UniverseAgentRanking,
    UniverseAssetRanking,
    UniverseSelectionInput,
    UniverseSourceRef,
)
from trading_system.universe.validation import AgentOutputInvalidError, validate_ranking

from .conftest import UNIVERSE_NOW

pytestmark = pytest.mark.unit

RUN_ID = "universe-test-1"


def _candidate(
    symbol: str = "SPY",
    *,
    optionability: Optionability = Optionability.TRUE,
    research_usable: bool = True,
    freshness_valid: bool = True,
    classification: DataQuality = DataQuality.OK,
    volume: Decimal | None = Decimal("75000000"),
    price: Decimal | None = Decimal("500.15"),
) -> CandidateAsset:
    return CandidateAsset(
        symbol=symbol,
        security_type=SecurityType.STOCK,
        currency="USD",
        deterministic_eligibility=UniverseEligibility.ELIGIBLE,
        optionability=optionability,
        reference_price=price,
        underlying_volume=volume,
        data_quality=DataQualitySummary(
            research_usable=research_usable,
            classification=classification,
            freshness_valid=freshness_valid,
        ),
        research_usable=research_usable,
        source=CandidateProvenance(
            provider="IBKR",
            retrieved_at=UNIVERSE_NOW,
            snapshot_ids=[f"snap-{symbol.lower()}"],
        ),
    )


def _input(*candidates: CandidateAsset) -> UniverseSelectionInput:
    return UniverseSelectionInput(
        run_id=RUN_ID,
        as_of=UNIVERSE_NOW,
        universe_source=UniverseSourceRef(
            kind="STATIC", name="test", version="1", symbol_count=len(candidates)
        ),
        deterministic_filter_config=FilterConfigSnapshot(max_candidates=50, max_selected_assets=10),
        candidate_assets=list(candidates),
    )


def _ranking(*entries: UniverseAssetRanking, run_id: str = RUN_ID) -> UniverseAgentRanking:
    return UniverseAgentRanking(run_id=run_id, rankings=list(entries))


def _selected(
    symbol: str,
    rank: int,
    *,
    reasons: list[UniverseSelectionReason] | None = None,
) -> UniverseAssetRanking:
    return UniverseAssetRanking(
        symbol=symbol,
        selection="SELECTED",
        rank=rank,
        reasons=reasons or [UniverseSelectionReason.SUFFICIENT_DATA_QUALITY],
        confidence=ConfidenceLevel.HIGH,
    )


# ---------------------------------------------------------------------------
# 1. A valid response passes
# ---------------------------------------------------------------------------
def test_a_valid_response_is_accepted() -> None:
    agent_input = _input(_candidate("SPY"), _candidate("QQQ"))
    ranking = _ranking(_selected("SPY", 1), _selected("QQQ", 2))

    validate_ranking(ranking, agent_input, max_selected=10)


def test_every_issue_is_reported_at_once() -> None:
    """An operator debugging a prompt needs the whole picture, not the first line."""
    agent_input = _input(_candidate("SPY"))
    ranking = _ranking(_selected("SPY", 1), _selected("TSLA", 1), run_id="wrong-run")

    with pytest.raises(AgentOutputInvalidError) as caught:
        validate_ranking(ranking, agent_input, max_selected=10)

    codes = {issue.code for issue in caught.value.issues}
    assert {"RUN_ID_MISMATCH", "UNKNOWN_SYMBOL", "DUPLICATE_RANK"} <= codes


# ---------------------------------------------------------------------------
# 4. An unknown symbol
# ---------------------------------------------------------------------------
def test_a_symbol_the_agent_was_never_shown_is_rejected() -> None:
    agent_input = _input(_candidate("SPY"))
    ranking = _ranking(_selected("TSLA", 1))

    with pytest.raises(AgentOutputInvalidError, match="TSLA"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_the_agent_cannot_extend_the_candidate_pool() -> None:
    """Even alongside valid entries, one invented symbol voids the response."""
    agent_input = _input(_candidate("SPY"))
    ranking = _ranking(_selected("SPY", 1), _selected("NVDA", 2))

    with pytest.raises(AgentOutputInvalidError) as caught:
        validate_ranking(ranking, agent_input, max_selected=10)

    assert any(issue.code == "UNKNOWN_SYMBOL" for issue in caught.value.issues)


def test_the_same_symbol_twice_is_rejected() -> None:
    agent_input = _input(_candidate("SPY"), _candidate("QQQ"))
    ranking = _ranking(_selected("SPY", 1), _selected("SPY", 2))

    with pytest.raises(AgentOutputInvalidError) as caught:
        validate_ranking(ranking, agent_input, max_selected=10)

    assert any(issue.code == "DUPLICATE_SYMBOL" for issue in caught.value.issues)


# ---------------------------------------------------------------------------
# 6. Duplicate and non-contiguous ranks
# ---------------------------------------------------------------------------
def test_a_duplicate_rank_is_rejected() -> None:
    agent_input = _input(_candidate("SPY"), _candidate("QQQ"))
    ranking = _ranking(_selected("SPY", 1), _selected("QQQ", 1))

    with pytest.raises(AgentOutputInvalidError, match="unique"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_ranks_must_be_contiguous_from_one() -> None:
    agent_input = _input(_candidate("SPY"), _candidate("QQQ"))
    ranking = _ranking(_selected("SPY", 1), _selected("QQQ", 7))

    with pytest.raises(AgentOutputInvalidError, match="contiguous"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_a_selected_asset_must_carry_a_rank() -> None:
    """Enforced by the model itself, before validation is even reached."""
    with pytest.raises(ValueError, match="SELECTED requires a rank"):
        UniverseAssetRanking(
            symbol="SPY",
            selection="SELECTED",
            rank=None,
            reasons=[UniverseSelectionReason.SUFFICIENT_DATA_QUALITY],
            confidence=ConfidenceLevel.HIGH,
        )


def test_a_rejected_asset_must_not_carry_a_rank() -> None:
    with pytest.raises(ValueError, match="must not carry a rank"):
        UniverseAssetRanking(
            symbol="SPY",
            selection="NOT_SELECTED",
            rank=3,
            reasons=[UniverseSelectionReason.UNIVERSE_SIZE_LIMIT],
            confidence=ConfidenceLevel.LOW,
        )


# ---------------------------------------------------------------------------
# 7. Too many selected assets
# ---------------------------------------------------------------------------
def test_exceeding_the_maximum_is_rejected_rather_than_truncated() -> None:
    agent_input = _input(_candidate("SPY"), _candidate("QQQ"), _candidate("NVDA"))
    ranking = _ranking(_selected("SPY", 1), _selected("QQQ", 2), _selected("NVDA", 3))

    with pytest.raises(AgentOutputInvalidError) as caught:
        validate_ranking(ranking, agent_input, max_selected=2)

    issue = next(i for i in caught.value.issues if i.code == "TOO_MANY_SELECTED")
    assert "rejected rather than truncated" in issue.message


# ---------------------------------------------------------------------------
# 8 & 9. Unsupported enum values
# ---------------------------------------------------------------------------
def test_an_unsupported_selection_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="SELECTED or NOT_SELECTED"):
        UniverseAssetRanking(
            symbol="SPY",
            selection="MAYBE",
            reasons=[UniverseSelectionReason.SUFFICIENT_DATA_QUALITY],
            confidence=ConfidenceLevel.HIGH,
        )


def test_an_unsupported_confidence_is_rejected() -> None:
    with pytest.raises(ValueError):
        UniverseAssetRanking(
            symbol="SPY",
            selection="NOT_SELECTED",
            reasons=[UniverseSelectionReason.UNIVERSE_SIZE_LIMIT],
            confidence="VERY_HIGH",
        )


def test_an_unsupported_reason_code_is_rejected() -> None:
    """The vocabulary is closed; an agent that could invent one could justify anything."""
    with pytest.raises(ValueError):
        UniverseAssetRanking(
            symbol="SPY",
            selection="SELECTED",
            rank=1,
            reasons=["EARNINGS_BEAT_EXPECTED"],
            confidence=ConfidenceLevel.HIGH,
        )


def test_at_least_one_reason_is_required() -> None:
    with pytest.raises(ValueError):
        UniverseAssetRanking(
            symbol="SPY",
            selection="SELECTED",
            rank=1,
            reasons=[],
            confidence=ConfidenceLevel.HIGH,
        )


# ---------------------------------------------------------------------------
# 10. Fabricated evidence
# ---------------------------------------------------------------------------
def test_claiming_options_are_available_when_optionability_is_unknown_is_rejected() -> None:
    agent_input = _input(_candidate("SPY", optionability=Optionability.UNKNOWN))
    ranking = _ranking(_selected("SPY", 1, reasons=[UniverseSelectionReason.OPTIONS_AVAILABLE]))

    with pytest.raises(AgentOutputInvalidError, match="unestablished chain"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_claiming_liquidity_with_no_volume_figure_is_rejected() -> None:
    agent_input = _input(_candidate("SPY", volume=None))
    ranking = _ranking(
        _selected("SPY", 1, reasons=[UniverseSelectionReason.HIGH_UNDERLYING_LIQUIDITY])
    )

    with pytest.raises(AgentOutputInvalidError, match="no underlying volume was supplied"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_claiming_sufficient_quality_for_an_unusable_record_is_rejected() -> None:
    agent_input = _input(_candidate("SPY", research_usable=False))
    ranking = _ranking(
        _selected("SPY", 1, reasons=[UniverseSelectionReason.SUFFICIENT_DATA_QUALITY])
    )

    with pytest.raises(AgentOutputInvalidError, match="unusable for research"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_claiming_fresh_data_for_a_stale_record_is_rejected() -> None:
    agent_input = _input(_candidate("SPY", freshness_valid=False))
    ranking = _ranking(_selected("SPY", 1, reasons=[UniverseSelectionReason.FRESH_MARKET_DATA]))

    with pytest.raises(AgentOutputInvalidError, match="outside the configured freshness window"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_claiming_stale_data_for_a_fresh_record_is_rejected() -> None:
    agent_input = _input(_candidate("SPY", freshness_valid=True))
    ranking = _ranking(_selected("SPY", 1, reasons=[UniverseSelectionReason.STALE_MARKET_DATA]))

    with pytest.raises(AgentOutputInvalidError, match="within the configured freshness window"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_claiming_a_price_range_with_no_price_is_rejected() -> None:
    agent_input = _input(_candidate("SPY", price=None))
    ranking = _ranking(_selected("SPY", 1, reasons=[UniverseSelectionReason.PRICE_IN_RANGE]))

    with pytest.raises(AgentOutputInvalidError, match="no reference price"):
        validate_ranking(ranking, agent_input, max_selected=10)


def test_a_liquidity_band_is_the_agents_judgement_and_is_not_second_guessed() -> None:
    """Facts are enforced; opinions are not. A thin-but-present volume may still
    be called high, because "high" is a comparative judgement the agent is
    entitled to make from the pool it was shown."""
    agent_input = _input(_candidate("SPY", volume=Decimal("1200000")))
    ranking = _ranking(
        _selected("SPY", 1, reasons=[UniverseSelectionReason.HIGH_UNDERLYING_LIQUIDITY])
    )

    validate_ranking(ranking, agent_input, max_selected=10)


# ---------------------------------------------------------------------------
# 11. Empty result
# ---------------------------------------------------------------------------
def test_an_empty_ranking_is_valid() -> None:
    validate_ranking(_ranking(), _input(_candidate("SPY")), max_selected=10)


def test_selecting_nothing_while_ranking_everything_is_valid() -> None:
    agent_input = _input(_candidate("SPY"), _candidate("QQQ"))
    ranking = _ranking(
        UniverseAssetRanking(
            symbol="SPY",
            selection="NOT_SELECTED",
            reasons=[UniverseSelectionReason.UNIVERSE_SIZE_LIMIT],
            confidence=ConfidenceLevel.LOW,
        ),
        UniverseAssetRanking(
            symbol="QQQ",
            selection="NOT_SELECTED",
            reasons=[UniverseSelectionReason.UNIVERSE_SIZE_LIMIT],
            confidence=ConfidenceLevel.LOW,
        ),
    )

    validate_ranking(ranking, agent_input, max_selected=10)


# ---------------------------------------------------------------------------
# The agent cannot override a deterministic exclusion
# ---------------------------------------------------------------------------
def test_a_rejected_asset_cannot_even_be_expressed_in_the_agents_input() -> None:
    """Structural, not merely validated: the contract refuses to carry it."""
    rejected = _candidate("SPY").model_copy(
        update={"deterministic_eligibility": UniverseEligibility.REJECTED}
    )

    with pytest.raises(ValueError, match="must never reach the agent"):
        _input(rejected)


def test_duplicate_candidates_cannot_be_expressed_in_the_agents_input() -> None:
    with pytest.raises(ValueError, match="duplicate symbol"):
        _input(_candidate("SPY"), _candidate("SPY"))


def test_a_response_for_a_different_run_is_rejected() -> None:
    """Matching run ids stop a stale or replayed response being accepted."""
    with pytest.raises(AgentOutputInvalidError, match=r"RUN_ID|run "):
        validate_ranking(
            _ranking(_selected("SPY", 1), run_id="some-other-run"),
            _input(_candidate("SPY")),
            max_selected=10,
        )


def test_validation_does_not_mutate_the_ranking() -> None:
    """Rejection is total; there is no repair step that could leave a trace."""
    agent_input = _input(_candidate("SPY"))
    ranking = _ranking(_selected("SPY", 1), _selected("TSLA", 2))
    before = ranking.model_dump(mode="json")

    with pytest.raises(AgentOutputInvalidError):
        validate_ranking(ranking, agent_input, max_selected=10)

    assert ranking.model_dump(mode="json") == before


def test_the_agent_input_carries_no_timestamp_from_the_future() -> None:
    """A sanity check on the contract itself, not on any particular agent."""
    agent_input = _input(_candidate("SPY"))

    assert agent_input.as_of <= datetime.now(UTC)
    for candidate in agent_input.candidate_assets:
        assert candidate.source.retrieved_at <= agent_input.as_of
