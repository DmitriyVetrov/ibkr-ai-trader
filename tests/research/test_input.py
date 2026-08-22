"""The research input contract (brief sections 5, 6, 21-25, 55, 58, 59).

The input is the agent's entire view of the world, so what it *cannot* carry
matters as much as what it does. These tests assert the shape: every fact has a
citable id, no fact loses its provenance, an unavailable value stays
unavailable, and there is nowhere to put a contract.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import EvidenceKind, ResearchDataGap, SourceTier
from trading_system.research.models import ResearchInput

from .conftest import RESEARCH_NOW

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 6. The formal contract
# ---------------------------------------------------------------------------
def test_the_input_carries_every_required_section(
    researchable_symbol, store_filing, store_fundamentals, build_input
) -> None:
    researchable_symbol("NVDA")
    store_filing("NVDA")
    store_fundamentals("NVDA")

    research_input = build_input("NVDA")

    assert research_input.run_id
    assert research_input.symbol == "NVDA"
    assert research_input.as_of == RESEARCH_NOW
    assert research_input.horizon.min_days == 14
    assert research_input.market_snapshot is not None
    assert research_input.news
    assert research_input.events
    assert research_input.regulatory_events
    assert research_input.fundamentals
    assert research_input.data_snapshot_ids
    assert research_input.data_quality_summary is not None
    assert research_input.window.news_lookback_days == 14
    assert research_input.source_policy.config_version


def test_every_evidence_item_carries_provenance(researchable_symbol, build_input) -> None:
    """A claim whose source cannot be named later is not evidence."""
    researchable_symbol("NVDA")

    research_input = build_input("NVDA")

    assert research_input.all_evidence
    for item in research_input.all_evidence:
        assert item.source.provider
        assert item.source.snapshot_id
        assert item.source.retrieved_at
        assert item.source.source_tier in set(SourceTier)


def test_evidence_ids_are_unique_and_derived(researchable_symbol, build_input) -> None:
    """Two facts sharing an id would make a citation ambiguous."""
    researchable_symbol("NVDA")

    first = build_input("NVDA")
    second = build_input("NVDA")

    ids = [item.evidence_id for item in first.all_evidence]
    assert len(set(ids)) == len(ids)
    assert ids == [item.evidence_id for item in second.all_evidence], "derived, not generated"


def test_the_input_refuses_duplicate_evidence_ids(researchable_symbol, build_input) -> None:
    research_input = build_input("NVDA") if researchable_symbol("NVDA") else None
    assert research_input is not None
    payload = research_input.model_dump()
    payload["observations"] = payload["observations"] + payload["observations"]

    with pytest.raises(ValueError, match="duplicate evidence_id"):
        ResearchInput.model_validate(payload)


# ---------------------------------------------------------------------------
# 23. No contract is expressible
# ---------------------------------------------------------------------------
def test_the_option_context_names_no_contract(
    researchable_symbol, store_option_quotes, build_input
) -> None:
    """Days to expiration and implied volatility. No strike, no right, no date."""
    researchable_symbol("NVDA")
    store_option_quotes("NVDA")

    context = build_input("NVDA").option_context

    assert context is not None
    assert context.expiration_count == 2
    assert context.term_structure
    for point in context.term_structure:
        fields = set(type(point).model_fields)
        assert fields.isdisjoint(_CONTRACT_IDENTIFYING_FIELDS)
        assert "days_to_expiration" in fields, "the term structure is keyed by DTE, not by a date"


#: Field names that would let a consumer *name* one specific option contract.
#: ``strike_count`` and ``expiration_count`` are deliberately absent: they are
#: aggregates describing the chain's shape, and saying "this chain has 491
#: strikes" identifies no instrument.
_CONTRACT_IDENTIFYING_FIELDS = frozenset(
    {
        "strike",
        "strikes",
        "expiration",
        "expirations",
        "right",
        "rights",
        "contract_id",
        "occ_symbol",
        "local_symbol",
        "delta",
        "legs",
        "strategy_type",
    }
)

#: Fields that would let research size or fund a position. Matched exactly
#: rather than by substring, because ``market_capitalization`` is an ordinary
#: fundamental and a substring ban on "capital" would reject it — a test that
#: fires on legitimate data teaches people to delete the test.
_CAPITAL_FIELDS = frozenset(
    {
        "budget",
        "budget_currency",
        "campaign_budget",
        "allocated",
        "allocation",
        "requested_allocation",
        "quantity",
        "position_size",
        "risk_limits",
        "max_allocation_per_trade",
        # The campaign's own currency machinery. Research must not be able to
        # express which unit of account a trade settles in, or at what rate:
        # both reach the money, and both are the campaign's decision.
        #
        # A bare ``currency`` is deliberately NOT banned - a fundamental's
        # reporting currency is an ordinary fact about a filing, and a test
        # that fires on legitimate data teaches people to delete the test. Same
        # reasoning as ``market_capitalization`` above.
        "target_currency",
        "fx_rate",
    }
)


def test_no_input_model_can_identify_a_contract_or_size_a_position() -> None:
    """Brief sections 23 and 45: research must not be able to express either."""
    from trading_system.research.models import (
        EventItem,
        EvidenceItem,
        FundamentalItem,
        HistoricalContext,
        MarketContext,
        MarketSnapshot,
        OptionMarketContext,
        OptionTermPoint,
    )

    for model in (
        ResearchInput,
        EvidenceItem,
        EventItem,
        FundamentalItem,
        HistoricalContext,
        MarketContext,
        MarketSnapshot,
        OptionMarketContext,
        OptionTermPoint,
    ):
        fields = set(model.model_fields)
        offending = fields & _CONTRACT_IDENTIFYING_FIELDS
        assert not offending, f"{model.__name__} could identify a contract via {offending}"

        funding = fields & _CAPITAL_FIELDS
        assert not funding, f"{model.__name__} could size a position via {funding}"


# ---------------------------------------------------------------------------
# 24-25. Unavailable is not zero
# ---------------------------------------------------------------------------
def test_missing_implied_volatility_is_none_and_recorded_as_a_gap(
    researchable_symbol, build_input
) -> None:
    """IV_UNAVAILABLE, never IV = 0."""
    researchable_symbol("NVDA")  # a chain, but no option quotes

    research_input = build_input("NVDA")

    assert research_input.option_context is not None
    assert research_input.option_context.atm_implied_volatility is None
    assert research_input.data_quality_summary.has_gap(
        ResearchDataGap.IMPLIED_VOLATILITY_UNAVAILABLE
    )


def test_missing_volume_is_none_rather_than_zero(store_quote, store_chain, build_input) -> None:
    store_quote("NVDA", volume=None)
    store_chain("NVDA")

    snapshot = build_input("NVDA").market_snapshot

    assert snapshot is not None
    assert snapshot.volume is None


def test_an_unusable_record_is_flagged_rather_than_dropped(
    store_quote, store_chain, build_input
) -> None:
    """The data layer's verdict travels; the value is never corrected."""
    store_quote("NVDA", research_usable=False, plausibility_valid=False)
    store_chain("NVDA")

    research_input = build_input("NVDA")

    assert research_input.market_snapshot is not None, "still present, still visible"
    assert research_input.data_quality_summary.research_usable is False
    assert research_input.data_quality_summary.has_gap(
        ResearchDataGap.MARKET_DATA_NOT_RESEARCH_USABLE
    )
    assert research_input.data_quality_summary.has_gap(ResearchDataGap.SUSPICIOUS_VALUES_PRESENT)


def test_no_data_at_all_produces_gaps_rather_than_an_empty_success(build_input) -> None:
    research_input = build_input("NOTHING")

    assert research_input.market_snapshot is None
    assert research_input.all_evidence == []
    assert ResearchDataGap.MARKET_DATA_UNAVAILABLE in research_input.data_quality_summary.gaps


# ---------------------------------------------------------------------------
# 21. Market context
# ---------------------------------------------------------------------------
def test_unavailable_market_context_says_so_rather_than_being_absent(
    researchable_symbol, build_input
) -> None:
    researchable_symbol("NVDA")

    context = build_input("NVDA", broad_index_symbol="SPY").market_context

    assert context is not None
    assert context.available is False
    assert context.unavailable_reason
    assert "never retrieves it itself" in context.unavailable_reason


def test_market_context_is_used_when_the_store_has_it(
    researchable_symbol, store_quote, build_input
) -> None:
    researchable_symbol("NVDA")
    store_quote("SPY", last=Decimal("500.15"))

    context = build_input("NVDA", broad_index_symbol="SPY").market_context

    assert context is not None
    assert context.available is True
    assert context.broad_index is not None
    assert context.broad_index.symbol == "SPY"


# ---------------------------------------------------------------------------
# 22 and 24. Historical context and realized volatility
# ---------------------------------------------------------------------------
def test_historical_context_is_computed_from_stored_observations(
    store_price_history, store_chain, build_input
) -> None:
    store_price_history("NVDA", days=30, start_price=Decimal("150.00"), step=Decimal("1.00"))
    store_chain("NVDA")

    history = build_input("NVDA").historical_context

    assert history is not None
    assert history.observation_count == 30
    assert history.first_close == Decimal("150.00")
    assert history.last_close == Decimal("179.00")
    assert history.change_pct is not None and history.change_pct > 0
    assert history.high == Decimal("179.00")
    assert history.low == Decimal("150.00")


def test_realized_volatility_is_unavailable_below_the_observation_floor(
    store_price_history, store_chain, build_input
) -> None:
    """A volatility from four prices is a number, not a measurement."""
    store_price_history("NVDA", days=5)
    store_chain("NVDA")

    research_input = build_input("NVDA", min_observations_for_volatility=20)

    assert research_input.historical_context is not None
    assert research_input.historical_context.realized_volatility is None
    assert research_input.data_quality_summary.has_gap(
        ResearchDataGap.REALIZED_VOLATILITY_UNAVAILABLE
    )


def test_realized_volatility_carries_its_measurement_window(
    store_price_history, store_chain, build_input
) -> None:
    """Brief section 24: a horizon mismatch must be visible, not implicit."""
    store_price_history("NVDA", days=40)
    store_chain("NVDA")

    history = build_input("NVDA", min_observations_for_volatility=20).historical_context

    assert history is not None
    assert history.realized_volatility is not None
    assert history.realized_volatility_window_days == 90
    assert history.realized_volatility_annualization_days == 252


def test_a_technical_indicator_has_no_representation() -> None:
    """Brief section 22: research is not yet a technical-indicator strategy."""
    from trading_system.research.models import HistoricalContext

    fields = " ".join(HistoricalContext.model_fields).lower()
    for indicator in ("moving_average", "rsi", "macd", "bollinger", "signal", "crossover"):
        assert indicator not in fields


# ---------------------------------------------------------------------------
# 58-59. Windows and cost control
# ---------------------------------------------------------------------------
def test_news_outside_the_lookback_window_is_not_shown(
    store_quote, store_chain, store_news, build_input
) -> None:
    store_quote("NVDA")
    store_chain("NVDA")
    store_news("NVDA", article_id="recent", published_at=RESEARCH_NOW - timedelta(days=2))
    store_news("NVDA", article_id="ancient", published_at=RESEARCH_NOW - timedelta(days=60))

    research_input = build_input("NVDA", news_lookback_days=14)

    summaries = " ".join(item.summary for item in research_input.news)
    assert "recent" not in summaries or len(research_input.news) == 1
    assert len(research_input.news) == 1


def test_the_news_limit_truncates_visibly(
    store_quote, store_chain, store_news, build_input
) -> None:
    """A thin input must never be mistaken for a quiet market."""
    store_quote("NVDA")
    store_chain("NVDA")
    for index in range(6):
        store_news(
            "NVDA",
            article_id=f"story-{index}",
            headline=f"Distinct story number {index} about the company",
            published_at=RESEARCH_NOW - timedelta(days=1, hours=index * 12),
        )

    research_input = build_input("NVDA", max_news_items=3)

    assert len(research_input.news) == 3
    assert "news" in research_input.limits.truncated


def test_the_evidence_budget_applies_across_sections(
    store_quote, store_chain, store_news, build_input
) -> None:
    store_quote("NVDA")
    store_chain("NVDA")
    for index in range(5):
        store_news(
            "NVDA",
            article_id=f"story-{index}",
            headline=f"Distinct story number {index} about the company",
            published_at=RESEARCH_NOW - timedelta(days=1, hours=index * 12),
        )

    research_input = build_input("NVDA", max_evidence_items=3)

    assert len(research_input.all_evidence) == 3
    assert research_input.limits.truncated


def test_observations_survive_the_budget_before_news(
    store_quote, store_chain, store_news, build_input
) -> None:
    """The market facts every report needs are kept first."""
    store_quote("NVDA")
    store_chain("NVDA")
    for index in range(5):
        store_news(
            "NVDA",
            article_id=f"story-{index}",
            headline=f"Distinct story number {index} about the company",
            published_at=RESEARCH_NOW - timedelta(days=1, hours=index * 12),
        )

    research_input = build_input("NVDA", max_evidence_items=2)

    kinds = {item.kind for item in research_input.all_evidence}
    assert EvidenceKind.MARKET_DATA in kinds


# ---------------------------------------------------------------------------
# 9. Source tiers are preserved
# ---------------------------------------------------------------------------
def test_the_configured_tier_wins_over_a_providers_own_claim(
    store_quote, store_chain, store_news, build_input
) -> None:
    """A provider cannot promote itself by stamping TIER_1 on its own output."""
    store_quote("NVDA")
    store_chain("NVDA")
    store_news(
        "NVDA",
        article_id="overclaimed",
        source_name="Reuters",
        url="https://www.reuters.com/technology/x/",
        tier=SourceTier.TIER_1,
    )

    news = build_input("NVDA").news

    assert news[0].source.source_tier is SourceTier.TIER_2, "sources.yaml lists reuters.com as 2"


def test_an_unlisted_source_keeps_its_declared_tier(
    store_quote, store_chain, store_news, build_input
) -> None:
    """'The policy does not list this' is not 'the policy calls this tier 4'."""
    store_quote("NVDA")
    store_chain("NVDA")
    store_news(
        "NVDA",
        article_id="unlisted",
        source_name="Some Trade Journal",
        url="https://example-trade-journal.test/story",
        tier=SourceTier.TIER_3,
    )

    news = build_input("NVDA").news

    assert news[0].source.source_tier is SourceTier.TIER_3


def test_provider_supplied_relevance_is_carried_not_computed(
    store_quote, store_chain, store_news, build_input
) -> None:
    store_quote("NVDA")
    store_chain("NVDA")
    store_news("NVDA", relevance=0.7)

    news = build_input("NVDA").news

    assert news[0].source.provider_relevance == 0.7
