"""The research service end to end (brief sections 32, 33, 39, 40, 44, 50, 52).

The service is where every rule has to hold together at once: it consumes a
universe it did not select, researches each underlying in isolation, and turns
every possible failure into a report that states no view. These tests are
organised around the failure modes, because that is where a research pipeline
does damage — a successful run is easy, and a run that quietly converts an
outage into a hypothesis is the thing that must be impossible.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from trading_system.agents.base import AgentTimeoutError
from trading_system.domain.enums import (
    ConfidenceLevel,
    MarketHypothesis,
    ResearchStatus,
    UniverseSelectionStatus,
)

from .conftest import RESEARCH_NOW, FakeLLMClient, UnavailableLLMClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 32. The pipeline
# ---------------------------------------------------------------------------
def test_a_full_run_produces_one_report_per_underlying(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA", "AAPL"])
    researchable_symbol("NVDA")
    researchable_symbol("AAPL")
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    run = service.run(as_of=RESEARCH_NOW)

    assert run.result.status is ResearchStatus.SUCCESS
    assert run.result.symbols == ["AAPL", "NVDA"]
    assert run.result.counts.succeeded == 2
    for report in run.result.reports:
        assert report.hypothesis is MarketHypothesis.B
        assert report.evidence
        assert report.invalidation_conditions
        assert report.input_snapshot_ids


def test_each_underlying_gets_its_own_isolated_context(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """Brief section 33: never one merged answer for several assets."""
    store_universe(["NVDA", "AAPL"])
    researchable_symbol("NVDA")
    researchable_symbol("AAPL")
    client = FakeLLMClient(outlook_text)

    make_service(llm_client=client).run(as_of=RESEARCH_NOW, dry_run=True)

    assert len(client.requests) == 2, "one request per underlying"
    symbols = [json.loads(r.user_content)["symbol"] for r in client.requests]
    assert sorted(symbols) == ["AAPL", "NVDA"]
    for request in client.requests:
        payload = json.loads(request.user_content)
        others = {"AAPL", "NVDA"} - {payload["symbol"]}
        assert others.isdisjoint(json.dumps(payload["news"]))


def test_the_report_copies_facts_from_the_input_not_from_the_model(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """The agent interprets a fact; it does not get to describe it."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    report = service.run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    for item in report.evidence:
        assert item.source.snapshot_id
        assert item.source.retrieved_at <= RESEARCH_NOW
        assert item.fact, "the input's own neutral description survives next to the claim"
        assert item.fact != item.claim


# ---------------------------------------------------------------------------
# The universe is consumed, never re-selected
# ---------------------------------------------------------------------------
def test_research_without_a_universe_reports_no_data(make_service) -> None:
    run = make_service().run(as_of=RESEARCH_NOW, dry_run=True)

    assert run.result.status is ResearchStatus.NO_DATA
    assert "does not select one" in (run.result.status_detail or "")
    assert run.result.reports == []


def test_a_failed_universe_run_cannot_be_researched(make_service, store_universe) -> None:
    store_universe([], status=UniverseSelectionStatus.AI_UNAVAILABLE)

    run = make_service().run(as_of=RESEARCH_NOW, dry_run=True)

    assert run.result.status is ResearchStatus.NO_DATA
    assert "AI_UNAVAILABLE" in (run.result.status_detail or "")


def test_research_cannot_add_a_symbol_the_universe_did_not_select(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """A stage that picked its own subjects would make the pre-filter decorative."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    researchable_symbol("TSLA")
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    run = service.run(as_of=RESEARCH_NOW, dry_run=True, symbols=["TSLA"])

    assert run.result.status is ResearchStatus.CONFIGURATION_ERROR
    assert "cannot extend it" in (run.result.status_detail or "")
    assert run.result.reports == []


def test_a_subset_of_the_universe_may_be_researched(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA", "AAPL"])
    researchable_symbol("NVDA")
    researchable_symbol("AAPL")
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    run = service.run(as_of=RESEARCH_NOW, dry_run=True, symbols=["NVDA"])

    assert run.result.symbols == ["NVDA"]


def test_a_named_universe_run_can_be_researched(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"], run_id="universe-older")
    store_universe(["AAPL"], run_id="universe-newer")
    researchable_symbol("NVDA")
    researchable_symbol("AAPL")
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    run = service.run(as_of=RESEARCH_NOW, dry_run=True, universe_run_id="universe-older")

    assert run.result.symbols == ["NVDA"]
    assert run.result.universe_run_id == "universe-older"


# ---------------------------------------------------------------------------
# 39-40. Failure is never a market view
# ---------------------------------------------------------------------------
def test_an_unreachable_model_produces_no_hypothesis(
    make_service, store_universe, researchable_symbol
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(llm_client=UnavailableLLMClient())

    report = service.run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    assert report.status is ResearchStatus.AI_UNAVAILABLE
    assert report.hypothesis is None
    assert report.confidence is None
    assert report.direction is None
    assert report.bullish_catalysts == []


def test_a_timeout_is_an_unavailable_model_not_an_outlook(
    make_service, store_universe, researchable_symbol
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(llm_client=UnavailableLLMClient(AgentTimeoutError("no answer in 30s")))

    report = service.run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    assert report.status is ResearchStatus.AI_UNAVAILABLE
    assert report.hypothesis is None


def test_malformed_output_ends_as_ai_invalid_output(
    make_service, store_universe, researchable_symbol
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(llm_client=FakeLLMClient("this is not JSON"))

    report = service.run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    assert report.status is ResearchStatus.AI_INVALID_OUTPUT
    assert report.hypothesis is None


def test_a_semantically_invalid_outlook_ends_as_semantic_validation_failed(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """Distinct from AI_INVALID_OUTPUT: the answer parsed, but is not licensed."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")

    def bad(request):  # a B with no upward evidence
        payload = json.loads(outlook_text(request))
        for item in payload["evidence"]:
            item["direction"] = "NEUTRAL"
        return json.dumps(payload)

    service = make_service(llm_client=FakeLLMClient(bad))

    report = service.run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    assert report.status is ResearchStatus.SEMANTIC_VALIDATION_FAILED
    assert report.hypothesis is None
    assert "rejected in full rather than repaired" in (report.status_detail or "")


def test_no_data_at_all_is_reported_as_no_data(make_service, store_universe) -> None:
    store_universe(["NVDA"])

    report = make_service().run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    assert report.status is ResearchStatus.NO_DATA
    assert "collect data first" in (report.status_detail or "")


def test_insufficient_evidence_is_a_valid_outcome_without_a_model_call(
    make_service, store_universe, store_quote
) -> None:
    """Spending a request to be told 'not enough to say' is waste."""
    store_universe(["NVDA"])
    store_quote("NVDA", research_usable=False)
    client = FakeLLMClient("{}")
    service = make_service(llm_client=client, min_evidence_items=5)

    report = service.run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    assert report.status is ResearchStatus.INSUFFICIENT_EVIDENCE
    assert report.hypothesis is None
    assert client.requests == [], "no model was consulted"
    assert "not a directional signal" in (report.status_detail or "")


def test_a_disabled_agent_produces_no_outlook_and_no_substitute(
    make_service, store_universe, researchable_symbol
) -> None:
    """There is no deterministic fallback for a market view, by design."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(agent_enabled=False)

    report = service.run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    assert report.status is ResearchStatus.AI_UNAVAILABLE
    assert report.hypothesis is None
    assert "no deterministic substitute" in (report.status_detail or "")


def test_one_symbol_failing_does_not_stop_the_others(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA", "AAPL"])
    researchable_symbol("NVDA")
    # AAPL has no stored data at all.

    service = make_service(llm_client=FakeLLMClient(outlook_text))
    run = service.run(as_of=RESEARCH_NOW, dry_run=True)

    nvda = run.result.report("NVDA")
    aapl = run.result.report("AAPL")
    assert nvda is not None and nvda.status is ResearchStatus.SUCCESS
    assert aapl is not None and aapl.status is ResearchStatus.NO_DATA
    assert run.result.status is ResearchStatus.SUCCESS, "the run produced an outlook"


def test_a_run_where_nothing_succeeded_reports_the_dominant_failure(
    make_service, store_universe, researchable_symbol
) -> None:
    store_universe(["NVDA", "AAPL"])
    researchable_symbol("NVDA")
    researchable_symbol("AAPL")
    service = make_service(llm_client=UnavailableLLMClient())

    run = service.run(as_of=RESEARCH_NOW, dry_run=True)

    assert run.result.status is ResearchStatus.AI_UNAVAILABLE
    assert run.result.counts.succeeded == 0
    assert run.result.counts.failed == 2


# ---------------------------------------------------------------------------
# 59. Cost control
# ---------------------------------------------------------------------------
def test_assets_beyond_the_run_ceiling_are_recorded_as_skipped(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """'We did not look' is not 'we found nothing'."""
    store_universe(["NVDA", "AAPL", "MSFT"])
    for symbol in ("NVDA", "AAPL", "MSFT"):
        researchable_symbol(symbol)
    client = FakeLLMClient(outlook_text)
    service = make_service(llm_client=client, max_assets_per_run=2)

    run = service.run(as_of=RESEARCH_NOW, dry_run=True)

    assert len(client.requests) == 2
    statuses = {r.symbol: r.status for r in run.result.reports}
    assert sum(s is ResearchStatus.SKIPPED_COST_LIMIT for s in statuses.values()) == 1
    skipped = next(r for r in run.result.reports if r.status is ResearchStatus.SKIPPED_COST_LIMIT)
    assert skipped.hypothesis is None
    assert "ceiling" in (skipped.status_detail or "")


def test_an_oversized_input_fails_rather_than_being_truncated(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """An agent reasoning from a shortened input would believe it saw everything."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(llm_client=FakeLLMClient(outlook_text), max_input_characters=1000)

    report = service.run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    assert report.status is ResearchStatus.AI_INVALID_OUTPUT
    assert "not truncated" in (report.status_detail or "")


# ---------------------------------------------------------------------------
# 44. Dry run
# ---------------------------------------------------------------------------
def test_a_dry_run_persists_nothing_but_still_runs_everything(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    client = FakeLLMClient(outlook_text)
    service = make_service(llm_client=client)

    run = service.run(as_of=RESEARCH_NOW, dry_run=True)

    assert run.dry_run is True
    assert run.stored is False
    assert client.requests, "the agent was still consulted"
    assert research_repo.history() == []
    assert not research_repo.history_path.exists()


def test_a_dry_run_exposes_the_inputs_for_inspection(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    run = service.run(as_of=RESEARCH_NOW, dry_run=True)

    assert "NVDA" in run.inputs
    assert run.inputs["NVDA"].all_evidence


# ---------------------------------------------------------------------------
# 50. Reproducibility
# ---------------------------------------------------------------------------
def test_identical_inputs_and_an_identical_answer_produce_an_identical_report(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")

    first = make_service(llm_client=FakeLLMClient(outlook_text)).run(
        as_of=RESEARCH_NOW, dry_run=True
    )
    second = make_service(llm_client=FakeLLMClient(outlook_text)).run(
        as_of=RESEARCH_NOW, dry_run=True
    )

    assert first.result.run_id == second.result.run_id
    left = first.result.report("NVDA")
    right = second.result.report("NVDA")
    assert left is not None and right is not None
    assert left.report_id == right.report_id
    assert left.model_dump(mode="json") == right.model_dump(mode="json")


def test_a_measured_duration_is_not_part_of_the_record(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """A fact about the machine must not make two identical runs differ."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    run = service.run(as_of=RESEARCH_NOW, dry_run=True)

    serialised = json.dumps(run.result.model_dump(mode="json"))
    assert "duration_seconds" not in serialised
    assert run.duration_seconds >= 0.0, "it is reported on the wrapper instead"


def test_the_report_id_ignores_runtime_metadata(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    from trading_system.research.models import report_identifier

    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    report = (
        make_service(llm_client=FakeLLMClient(outlook_text))
        .run(as_of=RESEARCH_NOW, dry_run=True)
        .result.report("NVDA")
    )

    assert report is not None
    assert report.report_id == report_identifier(
        run_id=report.run_id,
        symbol=report.symbol,
        as_of=report.as_of,
        status=report.status,
        hypothesis=report.hypothesis,
        confidence=report.confidence,
        evidence_ids=[e.evidence_id for e in report.evidence],
    )


def test_a_different_universe_produces_a_different_run_id(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    researchable_symbol("NVDA")
    researchable_symbol("AAPL")

    store_universe(["NVDA"], run_id="universe-a")
    first = make_service(llm_client=FakeLLMClient(outlook_text)).run(
        as_of=RESEARCH_NOW, dry_run=True, universe_run_id="universe-a"
    )
    store_universe(["AAPL"], run_id="universe-b")
    second = make_service(llm_client=FakeLLMClient(outlook_text)).run(
        as_of=RESEARCH_NOW, dry_run=True, universe_run_id="universe-b"
    )

    assert first.result.run_id != second.result.run_id


# ---------------------------------------------------------------------------
# 52. Zero orders
# ---------------------------------------------------------------------------
def test_a_research_run_submits_no_orders(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """A whole run, with a writable broker sitting there unused."""
    from trading_system.broker.simulator import SimulatedBroker

    broker = SimulatedBroker(read_only=False)
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    run = service.run(as_of=RESEARCH_NOW)

    assert run.result.status is ResearchStatus.SUCCESS
    assert broker.orders_submitted == 0
    assert not broker.is_connected, "the run never even connected"


def test_the_service_exposes_no_broker(make_service) -> None:
    """There is nothing on the service a caller could reach an order through."""
    public = {name for name in dir(make_service()) if not name.startswith("_")}

    assert not any("broker" in name or "order" in name for name in public)


# ---------------------------------------------------------------------------
# The projection onto the Milestone 1 boundary
# ---------------------------------------------------------------------------
def test_a_successful_report_projects_onto_the_strategy_boundary(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    report = (
        make_service(llm_client=FakeLLMClient(outlook_text))
        .run(as_of=RESEARCH_NOW, dry_run=True)
        .result.report("NVDA")
    )

    assert report is not None
    boundary = report.to_research_report()

    assert boundary.ticker == "NVDA"
    assert boundary.hypothesis is MarketHypothesis.B
    assert boundary.invalidation_conditions
    assert boundary.sources
    assert boundary.expected_horizon_days == 21


def test_a_failed_report_cannot_be_projected(make_service, store_universe) -> None:
    """There is no way to hand a failed run across the boundary as a view."""
    store_universe(["NVDA"])
    report = make_service().run(as_of=RESEARCH_NOW, dry_run=True).result.report("NVDA")

    assert report is not None
    with pytest.raises(ValueError, match="nothing to hand on"):
        report.to_research_report()


def test_the_confidence_band_ordering_is_the_only_numeric_guarantee() -> None:
    """The projected float is a band representative, not a probability."""
    from trading_system.research.models import CONFIDENCE_BAND_VALUE

    assert (
        CONFIDENCE_BAND_VALUE[ConfidenceLevel.LOW]
        < CONFIDENCE_BAND_VALUE[ConfidenceLevel.MEDIUM]
        < CONFIDENCE_BAND_VALUE[ConfidenceLevel.HIGH]
    )


def test_the_canonical_report_states_no_probability() -> None:
    """Brief section 17: no percentage appears in a research report."""
    from trading_system.research.models import MarketResearchReport

    fields = " ".join(MarketResearchReport.model_fields).lower()
    for forbidden in ("probability", "percent", "odds", "likelihood_pct"):
        assert forbidden not in fields


# ---------------------------------------------------------------------------
# What the agent is actually sent
# ---------------------------------------------------------------------------
def test_the_payload_carries_no_budget_or_risk_information(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    client = FakeLLMClient(outlook_text)

    make_service(llm_client=client).run(as_of=RESEARCH_NOW, dry_run=True)

    sent = client.requests[0].user_content.lower()
    for forbidden in ("campaign_budget", "budget_eur", "max_allocation", "position_size"):
        assert forbidden not in sent


def test_the_payload_carries_no_strike_or_expiry(
    make_service, store_universe, researchable_symbol, store_option_quotes, outlook_text
) -> None:
    """Checked on the actual bytes sent, not only on the model's shape."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    store_option_quotes("NVDA")
    client = FakeLLMClient(outlook_text)

    make_service(llm_client=client).run(as_of=RESEARCH_NOW, dry_run=True)

    payload = json.loads(client.requests[0].user_content)
    option_context = json.dumps(payload["option_context"])
    assert '"strike":' not in option_context
    assert '"right":' not in option_context
    assert '"expiration":' not in option_context
    assert "days_to_expiration" in option_context


def test_the_horizon_is_stated_in_the_request(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    client = FakeLLMClient(outlook_text)

    make_service(llm_client=client, min_horizon=14, max_horizon=31).run(
        as_of=RESEARCH_NOW, dry_run=True
    )

    payload = json.loads(client.requests[0].user_content)
    assert payload["horizon"] == {"min_days": 14, "max_days": 31}


def test_a_configured_horizon_change_reaches_the_agent(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """The horizon is configuration, not a constant in code."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    client = FakeLLMClient(outlook_text)

    make_service(llm_client=client, min_horizon=7, max_horizon=45).run(
        as_of=RESEARCH_NOW, dry_run=True
    )

    payload = json.loads(client.requests[0].user_content)
    assert payload["horizon"] == {"min_days": 7, "max_days": 45}


# ---------------------------------------------------------------------------
# Historical replay
# ---------------------------------------------------------------------------
def test_a_past_instant_can_be_researched_from_stored_history(
    make_service, store_universe, store_quote, store_chain, store_news, outlook_text
) -> None:
    last_week = RESEARCH_NOW - timedelta(days=7)
    store_universe(["NVDA"], as_of=last_week)
    store_quote("NVDA", as_of=last_week, retrieved_at=last_week)
    store_chain("NVDA", as_of=last_week)
    store_news("NVDA", article_id="then", published_at=last_week - timedelta(hours=3))
    store_news("NVDA", article_id="since", published_at=RESEARCH_NOW - timedelta(hours=3))
    service = make_service(llm_client=FakeLLMClient(outlook_text))

    run = service.run(as_of=last_week, dry_run=True)

    assert run.result.as_of == last_week
    assert len(run.inputs["NVDA"].news) == 1
