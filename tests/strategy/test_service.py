"""The strategy service: the pipeline from research to a stored decision.

What matters here is not that a happy path works — it is that every way the
stage can fail produces a record that says so and proposes nothing. An
unreachable model, an unusable answer, an inadequate outlook and an absent
option chain are four different problems, and a stage that collapsed them into
"no trade" would hide which one occurred.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_system.domain.enums import (
    ConfidenceLevel,
    DecisionMethod,
    Direction,
    ExpectedMagnitude,
    MarketHypothesis,
    ResearchStatus,
    StrategyAction,
    StrategySelectionReason,
    StrategySelectionStatus,
    StrategyType,
)

from .conftest import RESEARCH_RUN_ID, FakeLLMClient, UnavailableLLMClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_matching_hypothesis_produces_a_proposal(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    store_research([make_report(hypothesis=MarketHypothesis.B)])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    run = service.run()

    decision = run.result.decision("NVDA")
    assert run.result.status is StrategySelectionStatus.SUCCESS
    assert decision is not None
    assert decision.action is StrategyAction.BUY
    assert decision.selected_strategy is StrategyType.LONG_CALL
    assert decision.strategy_version == "1.0.0"
    assert decision.decision_method is DecisionMethod.AI_SELECTED


def test_the_decision_records_everything_needed_to_explain_it(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    decision = service.run().result.decision("NVDA")

    assert decision is not None
    assert decision.research_report_id == "research-NVDA-001"
    assert decision.research_run_id == RESEARCH_RUN_ID
    assert decision.hypothesis is MarketHypothesis.B
    assert decision.eligible_strategies == [StrategyType.LONG_CALL]
    assert decision.rationale
    assert decision.agent_metadata is not None
    assert decision.agent_metadata.prompt_version == "test-1.0.0"
    assert decision.data_readiness.option_chain_available
    assert decision.input_snapshot_ids


def test_a_straddle_is_selectable_for_an_event_hypothesis(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    researchable("SPY")
    store_research(
        [
            make_report(
                "SPY",
                hypothesis=MarketHypothesis.D,
                direction=Direction.UNCERTAIN,
                magnitude=ExpectedMagnitude.LARGE,
                event_days=17,
            )
        ]
    )
    service = make_service(llm_client=FakeLLMClient(decision_text))

    decision = service.run().result.decision("SPY")

    assert decision is not None
    assert decision.selected_strategy in {
        StrategyType.LONG_STRADDLE,
        StrategyType.LONG_STRANGLE,
    }
    assert set(decision.eligible_strategies) == {
        StrategyType.LONG_STRADDLE,
        StrategyType.LONG_STRANGLE,
    }


def test_a_no_trade_decision_is_a_success(
    make_service, make_report, store_research, researchable, no_trade_text
) -> None:
    """Declining is a decision, not a failure of the stage."""
    researchable("NVDA")
    store_research([make_report()])
    service = make_service(llm_client=FakeLLMClient(no_trade_text))

    run = service.run()
    decision = run.result.decision("NVDA")

    assert run.result.status is StrategySelectionStatus.SUCCESS
    assert decision is not None and decision.succeeded
    assert decision.action is StrategyAction.NO_TRADE
    assert decision.selected_strategy is None
    assert run.result.counts.no_trade == 1
    assert run.result.counts.proposed == 0


# ---------------------------------------------------------------------------
# Deterministic gates, before any model call
# ---------------------------------------------------------------------------
def test_hypothesis_e_is_no_trade_without_a_model_call(
    make_service, make_report, store_research, researchable
) -> None:
    researchable("NVDA")
    store_research([make_report(hypothesis=MarketHypothesis.E, direction=Direction.NEUTRAL)])
    client = FakeLLMClient("{}")
    service = make_service(llm_client=client)

    decision = service.run().result.decision("NVDA")

    assert client.requests == [], "no model is consulted when nothing is eligible"
    assert decision is not None and decision.succeeded
    assert decision.action is StrategyAction.NO_TRADE
    assert StrategySelectionReason.NO_ELIGIBLE_STRATEGY in decision.reasons


def test_unusable_research_data_is_no_trade(
    make_service, make_report, store_research, researchable
) -> None:
    researchable("NVDA")
    store_research([make_report(research_usable=False)])
    client = FakeLLMClient("{}")

    decision = make_service(llm_client=client).run().result.decision("NVDA")

    assert client.requests == []
    assert decision is not None
    assert decision.action is StrategyAction.NO_TRADE
    assert StrategySelectionReason.DATA_QUALITY_INSUFFICIENT in decision.reasons


def test_thin_evidence_is_no_trade(make_service, make_report, store_research, researchable) -> None:
    researchable("NVDA")
    store_research([make_report(evidence_count=0)])
    client = FakeLLMClient("{}")

    decision = make_service(llm_client=client).run().result.decision("NVDA")

    assert client.requests == []
    assert decision is not None
    assert StrategySelectionReason.EVIDENCE_INSUFFICIENT in decision.reasons


def test_confidence_below_the_configured_floor_is_no_trade(
    make_service, make_report, store_research, researchable
) -> None:
    researchable("NVDA")
    store_research([make_report(confidence=ConfidenceLevel.LOW)])
    client = FakeLLMClient("{}")

    decision = (
        make_service(llm_client=client, min_confidence=ConfidenceLevel.MEDIUM)
        .run()
        .result.decision("NVDA")
    )

    assert client.requests == []
    assert decision is not None
    assert StrategySelectionReason.CONFIDENCE_INSUFFICIENT in decision.reasons


def test_no_option_chain_is_a_named_data_failure(make_service, make_report, store_research) -> None:
    """An underlying whose chain we have never seen cannot become an option trade."""
    store_research([make_report()])
    client = FakeLLMClient("{}")

    decision = make_service(llm_client=client).run().result.decision("NVDA")

    assert client.requests == []
    assert decision is not None
    assert decision.status is StrategySelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert decision.action is StrategyAction.NO_TRADE
    assert decision.selected_strategy is None
    assert "collect one first" in (decision.status_detail or "")


# ---------------------------------------------------------------------------
# The stage fails closed
# ---------------------------------------------------------------------------
def test_an_unreachable_model_proposes_nothing(
    make_service, make_report, store_research, researchable
) -> None:
    researchable("NVDA")
    store_research([make_report()])

    run = make_service(llm_client=UnavailableLLMClient()).run()
    decision = run.result.decision("NVDA")

    assert decision is not None
    assert decision.status is StrategySelectionStatus.AI_UNAVAILABLE
    assert decision.selected_strategy is None
    assert decision.action is StrategyAction.NO_TRADE
    assert run.result.status is StrategySelectionStatus.AI_UNAVAILABLE


def test_a_disabled_agent_proposes_nothing(
    make_service, make_report, store_research, researchable
) -> None:
    """There is no deterministic substitute for a strategy choice, by design."""
    researchable("NVDA")
    store_research([make_report()])

    decision = make_service(agent_enabled=False).run().result.decision("NVDA")

    assert decision is not None
    assert decision.status is StrategySelectionStatus.AI_UNAVAILABLE
    assert "no deterministic substitute" in (decision.status_detail or "")


def test_malformed_output_proposes_nothing(
    make_service, make_report, store_research, researchable
) -> None:
    researchable("NVDA")
    store_research([make_report()])

    decision = (
        make_service(llm_client=FakeLLMClient("not json at all")).run().result.decision("NVDA")
    )

    assert decision is not None
    assert decision.status is StrategySelectionStatus.AI_INVALID_OUTPUT
    assert decision.selected_strategy is None


def test_a_violating_decision_is_rejected_in_full(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    store_research([make_report(confidence=ConfidenceLevel.LOW)])
    over_confident = FakeLLMClient(lambda request: decision_text(request, confidence="HIGH"))

    decision = make_service(llm_client=over_confident).run().result.decision("NVDA")

    assert decision is not None
    assert decision.status is StrategySelectionStatus.SEMANTIC_VALIDATION_FAILED
    assert decision.selected_strategy is None
    assert "rejected in full rather than repaired" in (decision.status_detail or "")


def test_one_failing_symbol_does_not_stop_the_others(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    store_research([make_report("NVDA"), make_report("AAPL")])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    run = service.run()

    assert run.result.decision("NVDA") is not None
    assert run.result.decision("NVDA").proposes_a_trade
    aapl = run.result.decision("AAPL")
    assert aapl is not None
    assert aapl.status is StrategySelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert run.result.status is StrategySelectionStatus.SUCCESS


# ---------------------------------------------------------------------------
# The stage consumes research; it never extends it
# ---------------------------------------------------------------------------
def test_no_research_run_is_reported_honestly(make_service) -> None:
    run = make_service().run()

    assert run.result.status is StrategySelectionStatus.NO_RESEARCH
    assert run.result.decisions == []
    assert "run 'research run' first" in (run.result.status_detail or "")


def test_a_research_run_with_no_outlook_produces_no_decision(
    make_service, make_report, store_research
) -> None:
    store_research(
        [make_report(status=ResearchStatus.AI_UNAVAILABLE)],
        status=ResearchStatus.AI_UNAVAILABLE,
    )

    run = make_service().run()

    assert run.result.status is StrategySelectionStatus.NO_RESEARCH
    assert run.result.decisions == []


def test_a_symbol_research_did_not_cover_is_refused(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    store_research([make_report("NVDA")])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    run = service.run(symbols=["TSLA"])

    assert run.result.status is StrategySelectionStatus.CONFIGURATION_ERROR
    assert "cannot extend it" in (run.result.status_detail or "")


def test_the_run_can_be_narrowed_to_a_subset(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    researchable("AAPL")
    store_research([make_report("NVDA"), make_report("AAPL")])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    run = service.run(symbols=["NVDA"])

    assert run.result.symbols == ["NVDA"]


def test_the_cost_ceiling_records_what_was_not_considered(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    """ "We did not look" is not "we found nothing"."""
    researchable("NVDA")
    researchable("AAPL")
    store_research([make_report("AAPL"), make_report("NVDA")])
    service = make_service(llm_client=FakeLLMClient(decision_text), max_symbols_per_run=1)

    run = service.run()

    skipped = [
        decision
        for decision in run.result.decisions
        if decision.status is StrategySelectionStatus.SKIPPED_COST_LIMIT
    ]
    assert len(skipped) == 1
    assert run.result.counts.skipped == 1
    assert "ceiling of 1" in (skipped[0].status_detail or "")


# ---------------------------------------------------------------------------
# Persistence and reproducibility
# ---------------------------------------------------------------------------
def test_a_run_is_stored_and_readable(
    make_service, make_report, store_research, researchable, decision_text, strategy_repo
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    run = service.run()

    assert run.stored
    stored = strategy_repo.get(run.result.run_id)
    assert stored is not None
    assert stored.model_dump(mode="json") == run.result.model_dump(mode="json")
    assert [entry.run_id for entry in strategy_repo.history()] == [run.result.run_id]


def test_a_dry_run_persists_nothing(
    make_service, make_report, store_research, researchable, decision_text, strategy_repo
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    run = service.run(dry_run=True)

    assert not run.stored
    assert strategy_repo.history() == []
    assert run.inputs["NVDA"].symbol == "NVDA"


def test_a_re_run_over_unchanged_inputs_is_idempotent(
    make_service, make_report, store_research, researchable, decision_text, strategy_repo
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    first = service.run()
    second = service.run()

    assert first.result.run_id == second.result.run_id
    assert first.result.decisions[0].decision_id == second.result.decisions[0].decision_id
    assert len(strategy_repo.history()) == 1, "the same run is recognised, not duplicated"


def test_a_symbols_decision_history_accumulates(
    make_service, make_report, store_research, researchable, decision_text, strategy_repo
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    service = make_service(llm_client=FakeLLMClient(decision_text))
    service.run()

    tomorrow = make_report().as_of + timedelta(days=1)
    later = make_report(as_of=tomorrow, run_id="research-run-2")
    store_research([later], as_of=tomorrow, run_id="research-run-2")
    make_service(llm_client=FakeLLMClient(decision_text)).run(research_run_id="research-run-2")

    history = strategy_repo.symbol_history("NVDA")

    assert len(history) == 2
    assert history[0].generated_at >= history[1].generated_at


# ---------------------------------------------------------------------------
# The Milestone 1 boundary
# ---------------------------------------------------------------------------
def test_a_decision_projects_onto_the_workflow_boundary(
    make_service, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    service = make_service(llm_client=FakeLLMClient(decision_text))

    decision = service.run().result.decision("NVDA")
    assert decision is not None
    projected = decision.to_strategy_decision()

    assert projected.ticker == "NVDA"
    assert projected.action is StrategyAction.BUY
    assert projected.strategy_type is StrategyType.LONG_CALL
    assert projected.research_report_id == "research-NVDA-001"


def test_a_failed_decision_cannot_cross_the_boundary(
    make_service, make_report, store_research, researchable
) -> None:
    researchable("NVDA")
    store_research([make_report()])

    decision = make_service(llm_client=UnavailableLLMClient()).run().result.decision("NVDA")

    assert decision is not None
    with pytest.raises(ValueError, match="reached no decision"):
        decision.to_strategy_decision()
