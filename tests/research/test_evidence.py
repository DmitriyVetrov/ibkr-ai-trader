"""The evidence model and the source trust policy (brief sections 9, 10, 27, 48).

Evidence is the unit that makes a research report auditable: "why did the agent
conclude this" is only answerable if each conclusion points at a fact, and each
fact points at a source and a snapshot. These tests check the two halves of
that — the shape of an evidence item, and the single source-ranking system it
takes its tier from.
"""

from __future__ import annotations

import pytest

from trading_system.domain.enums import (
    ClaimSupport,
    ConfidenceLevel,
    EvidenceDirection,
    EvidenceKind,
    EvidenceStance,
    RelevanceLevel,
    RiskCategory,
    SourceTier,
)
from trading_system.research.models import (
    Catalyst,
    InvalidationCondition,
    RiskAssessment,
    tier_rank,
)
from trading_system.research.sources import SourceTrustPolicy

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 27. The evidence model makes an audit possible
# ---------------------------------------------------------------------------
def test_an_evidence_item_carries_everything_an_audit_needs(
    researchable_symbol, build_input
) -> None:
    researchable_symbol("NVDA")

    item = build_input("NVDA").news[0]

    assert item.evidence_id
    assert item.kind is EvidenceKind.NEWS
    assert item.summary
    assert item.source.source_name
    assert item.source.source_identifier
    assert item.source.source_tier
    assert item.source.published_at
    assert item.source.retrieved_at
    assert item.source.snapshot_id


def test_direction_and_stance_are_separate_concepts() -> None:
    """A fact can point up and still contradict a bearish thesis.

    Collapsing the two would force the agent to either mislabel the fact or
    drop it, and dropping it is what section 29 forbids.
    """
    from trading_system.research.models import AgentEvidenceAssessment

    assessment = AgentEvidenceAssessment(
        evidence_id="ev-1",
        claim="Revenue accelerated.",
        direction=EvidenceDirection.SUPPORTS_UP,
        stance=EvidenceStance.CONTRADICTS,
        relevance=RelevanceLevel.HIGH,
        confidence=ConfidenceLevel.HIGH,
    )

    assert assessment.direction is EvidenceDirection.SUPPORTS_UP
    assert assessment.stance is EvidenceStance.CONTRADICTS


def test_an_evidence_kind_knows_whether_it_is_a_dated_event() -> None:
    """Load-bearing for the A/D distinction."""
    assert EvidenceKind.CORPORATE_EVENT.is_dated_event
    assert EvidenceKind.MACRO_EVENT.is_dated_event
    assert not EvidenceKind.OPTION_MARKET.is_dated_event
    assert not EvidenceKind.NEWS.is_dated_event


# ---------------------------------------------------------------------------
# 10. A claim without evidence is labelled, not deleted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "build",
    [
        lambda ids: Catalyst(summary="A claim", evidence_ids=ids),
        lambda ids: RiskAssessment(
            category=RiskCategory.MACRO_RISK, description="A risk", evidence_ids=ids
        ),
        lambda ids: InvalidationCondition(condition="A condition", evidence_ids=ids),
    ],
)
def test_support_is_derived_from_the_citations(build) -> None:
    assert build([]).support is ClaimSupport.UNSUPPORTED
    assert build(["ev-1"]).support is ClaimSupport.SUPPORTED


@pytest.mark.parametrize(
    "build",
    [
        lambda: Catalyst(summary="A claim", evidence_ids=[], support="SUPPORTED"),
        lambda: RiskAssessment(
            category=RiskCategory.MACRO_RISK,
            description="A risk",
            evidence_ids=[],
            support="SUPPORTED",
        ),
        lambda: InvalidationCondition(
            condition="A condition", evidence_ids=[], support="SUPPORTED"
        ),
    ],
)
def test_a_claim_cannot_declare_support_it_does_not_have(build) -> None:
    """A declared value is discarded, not trusted and not merely rejected.

    Deriving rather than raising is deliberate: ``support`` is a mechanical
    function of what was cited, so recomputing it changes nothing the agent
    decided. The claim is still stored and still attributed — it is simply
    labelled honestly.
    """
    assert build().support is ClaimSupport.UNSUPPORTED


def test_a_claim_citing_evidence_is_marked_supported_even_if_it_said_otherwise() -> None:
    catalyst = Catalyst(summary="A claim", evidence_ids=["ev-1"], support="UNSUPPORTED")

    assert catalyst.support is ClaimSupport.SUPPORTED
    assert catalyst.is_supported


# ---------------------------------------------------------------------------
# 9. One source-ranking system, read from config/sources.yaml
# ---------------------------------------------------------------------------
@pytest.fixture
def policy(system_config) -> SourceTrustPolicy:
    return SourceTrustPolicy(system_config.sources)


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("https://www.sec.gov/Archives/edgar/data/1", SourceTier.TIER_1),
        ("https://www.reuters.com/technology/x", SourceTier.TIER_2),
        ("https://www.ft.com/content/x", SourceTier.TIER_2),
        ("https://www.marketwatch.com/story/x", SourceTier.TIER_3),
    ],
)
def test_a_known_domain_is_classified_from_configuration(
    policy: SourceTrustPolicy, identifier: str, expected: SourceTier
) -> None:
    assert policy.classify(identifier=identifier, name=None) is expected


def test_a_display_name_matches_its_configured_domain(policy: SourceTrustPolicy) -> None:
    assert policy.classify(identifier=None, name="Reuters") is SourceTier.TIER_2


def test_an_unknown_source_is_unclassified_rather_than_tier_four(
    policy: SourceTrustPolicy,
) -> None:
    """'The policy does not list this' and 'the policy calls this general web'
    are different statements, and only one of them is a judgement."""
    assert policy.classify(identifier="https://unknown-blog.test/x", name="Someone") is None


def test_the_configured_tier_beats_a_providers_own_claim(policy: SourceTrustPolicy) -> None:
    resolved = policy.resolve(
        declared=SourceTier.TIER_1,
        identifier="https://www.marketwatch.com/story/x",
        name="MarketWatch",
    )

    assert resolved is SourceTier.TIER_3, "a provider cannot promote itself"


def test_an_unlisted_source_keeps_the_tier_it_declared(policy: SourceTrustPolicy) -> None:
    resolved = policy.resolve(
        declared=SourceTier.TIER_2, identifier="https://unknown.test/x", name="Unknown"
    )

    assert resolved is SourceTier.TIER_2


def test_a_more_trusted_tier_wins_a_tie(policy: SourceTrustPolicy) -> None:
    """Entries are ordered by trust, so an ambiguous match resolves upward."""
    assert policy.classify(identifier="sec.gov reuters.com", name=None) is SourceTier.TIER_1


def test_the_policy_snapshot_echoes_the_configuration(
    policy: SourceTrustPolicy, system_config
) -> None:
    snapshot = policy.snapshot()

    assert snapshot.config_version == system_config.sources.config_version
    assert snapshot.min_sources_per_report == system_config.sources.min_sources_per_report
    assert snapshot.tier_1_count == len(system_config.sources.tier_1)


def test_tier_ordering_is_most_trusted_first() -> None:
    assert (
        tier_rank(SourceTier.TIER_1)
        < tier_rank(SourceTier.TIER_2)
        < tier_rank(SourceTier.TIER_3)
        < tier_rank(SourceTier.TIER_4)
    )


def test_research_does_not_define_its_own_source_tiers(repo_root) -> None:
    """Brief section 9: do not create a second source-ranking system."""
    research_yaml = (repo_root / "config" / "research.yaml").read_text(encoding="utf-8")

    for token in ("tier_1:", "tier_2:", "tier_3:", "tier_4:"):
        assert token not in research_yaml, "source tiers live in config/sources.yaml only"


# ---------------------------------------------------------------------------
# Evidence ordering prefers trust
# ---------------------------------------------------------------------------
def test_more_trusted_news_is_shown_first(
    store_quote, store_chain, store_news, build_input
) -> None:
    """Brief section 9: the agent must not treat all sources equally."""
    from datetime import timedelta

    from .conftest import RESEARCH_NOW

    store_quote("NVDA")
    store_chain("NVDA")
    store_news(
        "NVDA",
        article_id="low-tier-but-newer",
        headline="A completely different story from a lesser outlet",
        source_name="Some Aggregator",
        url="https://aggregator.test/x",
        tier=SourceTier.TIER_4,
        published_at=RESEARCH_NOW - timedelta(hours=1),
    )
    store_news(
        "NVDA",
        article_id="high-tier-but-older",
        headline="An unrelated wire report about the company",
        source_name="Reuters",
        url="https://www.reuters.com/y",
        published_at=RESEARCH_NOW - timedelta(hours=10),
    )

    news = build_input("NVDA").news

    assert news[0].source.source_tier is SourceTier.TIER_2
