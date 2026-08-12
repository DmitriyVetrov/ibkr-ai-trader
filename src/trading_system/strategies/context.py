"""Assembling what the strategy agent is allowed to see.

Two jobs, both deterministic and both deliberately outside the agent's import
graph:

* **Project the research report.** A :class:`ResearchSummary` is a *narrowing*
  of a :class:`~trading_system.research.models.MarketResearchReport`: the
  conclusion, the claims and the shape of the evidence, with every evidence id,
  source, URL, snapshot and option-market figure left behind. The strategy
  stage does not re-do research and must not be able to.
* **Establish data readiness.** Whether an option chain was actually visible at
  ``as_of`` is a fact about our storage, not a judgement, so it is settled here
  from the repository and never put to a model. A symbol with no chain ends as
  ``NO_TRADE`` before a request is ever sent.

The readiness probe reads through
:class:`~trading_system.data.repository.DataRepository` only — no provider, no
broker, no path, no network — and point-in-time, so a replay of a past instant
sees what was visible then rather than what exists now.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_system.data.models import MarketQuote, OptionChain, OptionQuote
from trading_system.data.point_in_time import assert_no_look_ahead
from trading_system.data.repository import DataRepository, records_of
from trading_system.domain.enums import DataType, SecurityType, SourceTier
from trading_system.research.models import MarketResearchReport, tier_rank
from trading_system.strategies.models import (
    DataReadiness,
    ResearchClaim,
    ResearchEventSummary,
    ResearchQualitySnapshot,
    ResearchSummary,
    StrategyOption,
    StrategySelectionInput,
)

__all__ = [
    "DataReadinessProbe",
    "build_selection_input",
    "research_summary",
]


def research_summary(report: MarketResearchReport) -> ResearchSummary:
    """Narrow a research report to what a strategy choice may rest on.

    Raises :class:`ValueError` for a report that produced no outlook. There is
    deliberately no way to hand a failed research run to the strategy stage as
    though it were a view — the same rule
    :meth:`~trading_system.research.models.MarketResearchReport.to_research_report`
    applies at the Milestone 1 boundary.
    """
    if not report.succeeded:
        raise ValueError(
            f"cannot build a strategy input from a {report.status.value} research report; "
            f"a run that produced no outlook has nothing to express"
        )
    assert report.hypothesis is not None  # narrowing; guaranteed by the report's validator
    assert report.confidence is not None
    assert report.direction is not None
    assert report.expected_magnitude is not None
    assert report.horizon_days is not None
    assert report.thesis is not None
    assert report.expected_behavior is not None

    best_tier = _best_tier(report)
    return ResearchSummary(
        report_id=report.report_id,
        symbol=report.symbol,
        as_of=report.as_of,
        hypothesis=report.hypothesis,
        direction=report.direction,
        expected_magnitude=report.expected_magnitude,
        confidence=report.confidence,
        horizon_days=report.horizon_days,
        thesis=report.thesis,
        expected_behavior=report.expected_behavior,
        explanation=report.explanation,
        contradiction_resolution=report.contradiction_resolution,
        bullish_catalysts=[
            ResearchClaim(summary=c.summary, support=c.support) for c in report.bullish_catalysts
        ],
        bearish_catalysts=[
            ResearchClaim(summary=c.summary, support=c.support) for c in report.bearish_catalysts
        ],
        risks=[
            ResearchClaim(summary=r.description, support=r.support, category=r.category.value)
            for r in report.risks
        ],
        invalidation_conditions=[
            ResearchClaim(summary=c.condition, support=c.support)
            for c in report.invalidation_conditions
        ],
        key_events=[
            ResearchEventSummary(
                event_type=event.event_type,
                summary=event.summary,
                days_until=event.days_until,
                within_horizon=event.within_horizon,
                expected_relevance=event.expected_relevance,
                directional_uncertainty=event.directional_uncertainty,
            )
            for event in report.key_events
        ],
        data_quality=ResearchQualitySnapshot(
            research_usable=report.data_quality.research_usable,
            classification=report.data_quality.classification,
            evidence_count=len(report.evidence),
            supporting_count=len(report.supporting_evidence),
            contradicting_count=len(report.contradicting_evidence),
            best_source_tier=best_tier,
            gaps=[gap.value for gap in report.data_quality.gaps],
        ),
    )


def _best_tier(report: MarketResearchReport) -> SourceTier | None:
    tiers = [item.source_tier for item in report.evidence]
    return min(tiers, key=tier_rank) if tiers else None


def build_selection_input(
    *,
    run_id: str,
    report: MarketResearchReport,
    eligible: list[StrategyOption],
    research_run_id: str | None = None,
    universe_run_id: str | None = None,
) -> StrategySelectionInput:
    """The agent's whole view: one research conclusion and the strategies for it.

    ``eligible`` comes from the registry, which resolved it from each
    strategy's own ``applicable_hypotheses``. The input model re-checks that
    every option answers this hypothesis, so an ineligible strategy cannot
    reach the agent even if a caller assembled the list wrongly.
    """
    return StrategySelectionInput(
        run_id=run_id,
        symbol=report.symbol,
        as_of=report.as_of,
        research=research_summary(report),
        eligible_strategies=eligible,
        research_run_id=research_run_id or report.run_id,
        universe_run_id=universe_run_id or report.universe_run_id,
    )


# ---------------------------------------------------------------------------
# Data readiness
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DataReadinessProbe:
    """Establishes, from the store, what option data existed at an instant.

    Holds a repository and nothing else. It cannot collect, cannot connect and
    cannot reach a provider: like the research input builder, it is a pure
    consumer of history already accumulated, which is what makes a replay of a
    past instant meaningful and "zero orders" structural.
    """

    repository: DataRepository

    def probe(self, symbol: str, as_of: datetime) -> DataReadiness:
        """What was visible for ``symbol`` at ``as_of``.

        Reports absence as absence. A symbol with no chain is not a symbol with
        an empty chain, and neither is a reason to invent one.
        """
        key = symbol.strip().upper()
        chain_snapshot = self.repository.get_as_of(DataType.OPTION_CHAIN, key, as_of)
        quote_snapshot = self.repository.get_as_of(DataType.OPTION_QUOTE, key, as_of)
        underlying_snapshot = self.repository.get_as_of(DataType.MARKET_QUOTE, key, as_of)

        chain: OptionChain | None = None
        if chain_snapshot is not None:
            chains = records_of(chain_snapshot, OptionChain)
            assert_no_look_ahead(chains, as_of)
            chain = max(chains, key=lambda c: c.as_of) if chains else None

        quote_count = 0
        if quote_snapshot is not None:
            quotes = records_of(quote_snapshot, OptionQuote)
            assert_no_look_ahead(quotes, as_of)
            quote_count = len(quotes)

        security_type: SecurityType | None = None
        if underlying_snapshot is not None:
            underlying_quotes = records_of(underlying_snapshot, MarketQuote)
            assert_no_look_ahead(underlying_quotes, as_of)
            security_type = underlying_quotes[0].security_type if underlying_quotes else None

        return DataReadiness(
            option_chain_available=chain is not None,
            option_quotes_available=quote_count > 0,
            underlying_quote_available=underlying_snapshot is not None,
            expirations_visible=len(chain.expirations) if chain else 0,
            strikes_visible=len(chain.strikes) if chain else 0,
            chain_snapshot_id=chain_snapshot.snapshot_id if chain_snapshot else None,
            quote_snapshot_id=(
                quote_snapshot.snapshot_id if quote_snapshot is not None and quote_count else None
            ),
            underlying_snapshot_id=(
                underlying_snapshot.snapshot_id if underlying_snapshot else None
            ),
            security_type=security_type,
            detail=_readiness_detail(key, chain is not None, quote_count),
        )


def _readiness_detail(symbol: str, has_chain: bool, quote_count: int) -> str:
    if not has_chain:
        return (
            f"no option chain for {symbol} was visible at this instant; collect one first "
            f"(data collect-options --symbol {symbol}). An underlying whose chain we have "
            f"not seen cannot become an option trade."
        )
    if quote_count == 0:
        return (
            f"an option chain for {symbol} was visible but no per-contract quotes were. "
            f"Contract selection will report REQUIRED_DATA_UNAVAILABLE rather than choosing "
            f"a contract it cannot price."
        )
    return f"option chain and {quote_count} contract quote(s) visible for {symbol}."
