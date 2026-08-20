"""The universe selection service end to end (brief sections 16, 22-24, 41).

The pipeline's job is to reach exactly one of a small set of honest outcomes,
and never to fake one. The tests that matter most here are the failure paths:

* an empty universe is a **valid result**, not an error to be worked around;
* an unreachable model ends the run with ``AI_UNAVAILABLE`` and **no** selected
  assets — it never falls back silently;
* a deterministic ordering happens only when configuration explicitly permits
  it, and the run is stamped so nobody can later mistake it for a model's
  judgement;
* filters are never relaxed to manufacture candidates.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from trading_system.agents.base import AgentTimeoutError, AgentUnavailableError
from trading_system.domain.enums import (
    SelectionMethod,
    UniverseEligibility,
    UniverseRejectionReason,
    UniverseSelectionStatus,
)

from .conftest import UNIVERSE_NOW, FakeLLMClient, UnavailableLLMClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_successful_run_produces_a_ranked_universe(
    optionable_symbols, make_service, ranking_text
) -> None:
    optionable_symbols(["SPY", "QQQ", "NVDA"])
    client = FakeLLMClient(ranking_text(["NVDA", "SPY"], not_selected=["QQQ"]))
    service = make_service(llm_client=client, symbols=["SPY", "QQQ", "NVDA"])

    run = service.run(as_of=UNIVERSE_NOW)

    assert run.result.status is UniverseSelectionStatus.SUCCESS
    assert run.result.selection_method is SelectionMethod.AI_RANKED
    assert run.result.symbols == ["NVDA", "SPY"]
    assert run.stored is True


def test_the_run_records_which_model_ranked_it(
    optionable_symbols, make_service, ranking_text
) -> None:
    """Brief section 20: a universe whose author is unknown cannot be reproduced."""
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY"])

    meta = service.run(as_of=UNIVERSE_NOW).result.agent_metadata

    assert meta is not None
    assert meta.model_provider == "FAKE"
    assert meta.model_name == "fake-model-1"
    assert meta.model_version == "fake-model-1-20260810"
    assert meta.prompt_version == "test-1.0.0"
    assert meta.raw_response, "the exact response is retained for auditability"


def test_every_candidate_ends_up_either_selected_or_explicitly_rejected(
    optionable_symbols, make_service, ranking_text
) -> None:
    """There is no third category. Silence is not a verdict."""
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA"])
    # The model ranks only SPY and simply omits the other two.
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=symbols)

    result = service.run(as_of=UNIVERSE_NOW).result

    accounted = {a.symbol for a in result.selected_assets} | {
        a.symbol for a in result.rejected_assets
    }
    assert accounted == set(symbols)
    omitted = next(a for a in result.rejected_assets if a.symbol == "QQQ")
    assert omitted.reason is UniverseRejectionReason.NOT_SELECTED_BY_RANKING
    assert "no verdict" in (omitted.detail or "")


def test_the_agent_only_ever_sees_eligible_candidates(
    data_repo, store_quote, store_chain, optionable_symbols, make_service, ranking_text
) -> None:
    """Brief section 2: the agent is a consumer of validated data."""
    optionable_symbols(["SPY"])
    store_quote("PENNY", last=Decimal("0.40"), close=None, bid=None, ask=None)
    store_chain("PENNY")

    client = FakeLLMClient(ranking_text(["SPY"]))
    service = make_service(llm_client=client, symbols=["SPY", "PENNY"])
    service.run(as_of=UNIVERSE_NOW)

    sent = json.loads(client.requests[0].user_content)
    symbols_shown = {c["symbol"] for c in sent["candidate_assets"]}
    assert symbols_shown == {"SPY"}, "the rejected asset was never shown to the model"
    assert all(c["deterministic_eligibility"] == "ELIGIBLE" for c in sent["candidate_assets"])


def test_the_agent_payload_carries_no_repository_or_provider_internals(
    optionable_symbols, make_service, ranking_text
) -> None:
    """Brief section 18: a compact structured dataset, not the data layer."""
    optionable_symbols(["SPY"])
    client = FakeLLMClient(ranking_text(["SPY"]))
    make_service(llm_client=client, symbols=["SPY"]).run(as_of=UNIVERSE_NOW)

    sent = client.requests[0].user_content
    for forbidden in ("/tmp", "raw/", "snapshots/", "historical/", "ib_async", "payload_hash"):
        assert forbidden not in sent, f"the agent must not receive {forbidden!r}"


# ---------------------------------------------------------------------------
# 23. An empty universe is valid
# ---------------------------------------------------------------------------
def test_no_qualifying_candidate_is_a_valid_result_not_an_error(
    store_quote, store_chain, make_service
) -> None:
    store_quote("SPY", last=Decimal("1.00"), close=None, bid=None, ask=None)
    store_chain("SPY")
    service = make_service(symbols=["SPY"])

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.NO_CANDIDATES
    assert result.selected_assets == []
    assert "never relaxed automatically" in (result.status_detail or "")


def test_filters_are_never_relaxed_to_manufacture_candidates(
    store_quote, store_chain, make_service
) -> None:
    """Two runs over the same failing data give the same empty answer."""
    store_quote("SPY", last=Decimal("1.00"), close=None, bid=None, ask=None)
    store_chain("SPY")
    service = make_service(symbols=["SPY"])

    first = service.run(as_of=UNIVERSE_NOW, dry_run=True).result
    second = service.run(as_of=UNIVERSE_NOW, dry_run=True).result

    assert first.selected_assets == second.selected_assets == []
    assert first.snapshot_id == second.snapshot_id


def test_missing_data_is_reported_as_data_unavailable_not_no_candidates(
    make_service,
) -> None:
    """A fact about our plumbing must not read as a fact about the market."""
    service = make_service(symbols=["SPY", "QQQ"])

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.DATA_UNAVAILABLE
    assert "plumbing" in (result.status_detail or "")


def test_an_empty_run_still_records_every_rejection(make_service) -> None:
    service = make_service(symbols=["SPY", "QQQ"])

    result = service.run(as_of=UNIVERSE_NOW).result

    assert {a.symbol for a in result.rejected_assets} == {"SPY", "QQQ"}
    assert all(a.reason is UniverseRejectionReason.DATA_UNAVAILABLE for a in result.rejected_assets)


# ---------------------------------------------------------------------------
# 24. Failure modes — fail closed
# ---------------------------------------------------------------------------
def test_an_unavailable_model_fails_closed(optionable_symbols, make_service) -> None:
    """No universe is produced, and no ordering is invented in its place."""
    optionable_symbols(["SPY", "QQQ"])
    service = make_service(llm_client=UnavailableLLMClient(), symbols=["SPY", "QQQ"])

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.AI_UNAVAILABLE
    assert result.selected_assets == []
    assert result.agent_metadata is None
    assert result.selection_method is SelectionMethod.DETERMINISTIC_ONLY


def test_a_model_timeout_is_reported_as_unavailable(optionable_symbols, make_service) -> None:
    optionable_symbols(["SPY"])
    service = make_service(
        llm_client=UnavailableLLMClient(AgentTimeoutError("no answer in 30s")),
        symbols=["SPY"],
    )

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.AI_UNAVAILABLE
    assert "30s" in (result.status_detail or "")


def test_a_failed_run_preserves_each_candidates_eligibility(
    optionable_symbols, make_service
) -> None:
    """The failure must not read as the assets being unsuitable."""
    optionable_symbols(["SPY", "QQQ"])
    service = make_service(llm_client=UnavailableLLMClient(), symbols=["SPY", "QQQ"])

    result = service.run(as_of=UNIVERSE_NOW).result

    for asset in result.rejected_assets:
        assert asset.deterministic_eligibility is UniverseEligibility.ELIGIBLE
        assert "never ranked" in (asset.detail or "")


def test_invalid_model_output_is_rejected_not_repaired(optionable_symbols, make_service) -> None:
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient("this is not JSON"), symbols=["SPY"])

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.AI_INVALID_OUTPUT
    assert result.selected_assets == []


def test_a_model_that_invents_a_symbol_invalidates_the_whole_run(
    optionable_symbols, make_service, ranking_text
) -> None:
    """One hallucinated ticker rejects the response entirely, not just that row."""
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY", "TSLA"])), symbols=["SPY"])

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.AI_INVALID_OUTPUT
    assert result.selected_assets == [], "SPY is not kept just because it was valid"
    assert "TSLA" in (result.status_detail or "")


def test_a_refusal_is_never_parsed_as_an_empty_ranking(optionable_symbols, make_service) -> None:
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient("", stop_reason="refusal"), symbols=["SPY"])

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.AI_UNAVAILABLE
    assert "refusal" in (result.status_detail or "")


def test_a_truncated_response_is_not_treated_as_an_answer(optionable_symbols, make_service) -> None:
    optionable_symbols(["SPY"])
    service = make_service(
        llm_client=FakeLLMClient('{"run_id": "x"', stop_reason="max_tokens"), symbols=["SPY"]
    )

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.AI_UNAVAILABLE


def test_an_unresolvable_source_is_a_configuration_error(make_service, shipped_config) -> None:
    """Brief section 8: index kinds are not approximated."""
    from trading_system.domain.enums import UniverseSourceKind
    from trading_system.infrastructure.settings import UniverseSourceConfig

    config = shipped_config.model_copy(
        update={
            "universe": shipped_config.universe.model_copy(
                update={
                    "source": UniverseSourceConfig(
                        kind=UniverseSourceKind.SP500,
                        name="sp500",
                        version="1",
                        symbols=[],
                    )
                }
            )
        }
    )
    service = make_service(config=config)

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.CONFIGURATION_ERROR
    assert "not implemented" in (result.status_detail or "")
    assert "invented data" in (result.status_detail or "")


# ---------------------------------------------------------------------------
# The explicitly configured deterministic path
# ---------------------------------------------------------------------------
def test_disabling_ai_ranking_produces_a_deterministic_universe(
    optionable_symbols, make_service
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA"])
    service = make_service(symbols=symbols, ai_enabled=False, max_selected=2)

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.SUCCESS
    assert result.selection_method is SelectionMethod.DETERMINISTIC_ONLY
    assert result.agent_metadata is None, "no model was involved, and the record says so"
    assert result.symbols == ["SPY", "QQQ"], "ordered by underlying volume, descending"


def test_the_deterministic_fallback_only_runs_when_configured(
    optionable_symbols, make_service
) -> None:
    optionable_symbols(["SPY", "QQQ"])

    closed = make_service(llm_client=UnavailableLLMClient(), symbols=["SPY", "QQQ"])
    assert closed.run(as_of=UNIVERSE_NOW, dry_run=True).result.status is (
        UniverseSelectionStatus.AI_UNAVAILABLE
    )

    permitted = make_service(
        llm_client=UnavailableLLMClient(), symbols=["SPY", "QQQ"], allow_fallback=True
    )
    result = permitted.run(as_of=UNIVERSE_NOW, dry_run=True).result

    assert result.status is UniverseSelectionStatus.SUCCESS
    assert result.selection_method is SelectionMethod.DETERMINISTIC_ONLY
    assert result.agent_metadata is None
    assert "deterministic fallback after AI_UNAVAILABLE" in (result.status_detail or "")


def test_the_deterministic_ordering_claims_only_supportable_reasons(
    store_quote, store_chain, make_service
) -> None:
    """Even the fallback may not assert evidence it does not have."""
    store_quote("SPY", volume=None, average_daily_volume=None)
    store_chain("SPY")
    service = make_service(symbols=["SPY"], ai_enabled=False, min_volume=0)

    result = service.run(as_of=UNIVERSE_NOW).result

    reasons = {r.value for r in result.selected_assets[0].reasons}
    assert not any("LIQUIDITY" in r for r in reasons), (
        "no liquidity claim is made for an asset with no volume figure"
    )


# ---------------------------------------------------------------------------
# 22. Maximum universe size
# ---------------------------------------------------------------------------
def test_the_agent_cannot_exceed_the_configured_maximum(
    optionable_symbols, make_service, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(symbols)), symbols=symbols, max_selected=2
    )

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.AI_INVALID_OUTPUT
    assert "max_selected_assets is 2" in (result.status_detail or "")
    assert result.selected_assets == [], "rejected rather than truncated"


def test_selecting_fewer_than_the_maximum_is_accepted(
    optionable_symbols, make_service, ranking_text
) -> None:
    """The cap is a ceiling, not a target."""
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ", "NVDA"])),
        symbols=symbols,
        max_selected=10,
    )

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.SUCCESS
    assert result.symbols == ["SPY"]


def test_an_empty_selection_from_the_agent_is_accepted(
    optionable_symbols, make_service, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text([], not_selected=symbols)), symbols=symbols
    )

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.status is UniverseSelectionStatus.SUCCESS
    assert result.selected_assets == []


# ---------------------------------------------------------------------------
# 29. Dry run
# ---------------------------------------------------------------------------
def test_a_dry_run_produces_a_result_without_persisting_it(
    optionable_symbols, make_service, ranking_text, universe_repo
) -> None:
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY"])

    run = service.run(as_of=UNIVERSE_NOW, dry_run=True)

    assert run.dry_run is True
    assert run.stored is False
    assert run.result.symbols == ["SPY"], "the whole pipeline still ran"
    assert universe_repo.history() == [], "authoritative history is untouched"


def test_a_dry_run_still_calls_the_agent(optionable_symbols, make_service, ranking_text) -> None:
    optionable_symbols(["SPY"])
    client = FakeLLMClient(ranking_text(["SPY"]))
    make_service(llm_client=client, symbols=["SPY"]).run(as_of=UNIVERSE_NOW, dry_run=True)

    assert len(client.requests) == 1


# ---------------------------------------------------------------------------
# 41. Observability
# ---------------------------------------------------------------------------
def test_the_run_counts_every_stage(optionable_symbols, make_service, ranking_text) -> None:
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY", "QQQ"], not_selected=["NVDA"])),
        symbols=[*symbols, "MISSING"],
    )

    run = service.run(as_of=UNIVERSE_NOW)
    counts = run.result.counts

    assert run.duration_seconds >= 0, "duration is observability, not part of the record"
    assert counts.candidates == 4
    assert counts.deterministic_pass == 3
    assert counts.deterministic_reject == 1
    assert counts.ai_input == 3
    assert counts.ai_selected == 2
    assert counts.final == 2


def test_a_run_never_constructs_a_broker(monkeypatch, optionable_symbols, make_service) -> None:
    """Brief section 40: the universe workflow has no order path at all."""
    from trading_system.broker import factory

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("universe selection must never construct a broker")

    monkeypatch.setattr(factory, "build_broker", _explode)
    optionable_symbols(["SPY"])

    make_service(symbols=["SPY"], ai_enabled=False).run(as_of=UNIVERSE_NOW)


def test_the_configured_provider_must_be_implemented(optionable_symbols, make_service) -> None:
    """An unimplemented provider fails honestly rather than silently skipping AI."""
    optionable_symbols(["SPY"])
    service = make_service(symbols=["SPY"])
    service._universe_config = service._universe_config.model_copy(
        update={
            "ai_ranking": service._universe_config.ai_ranking.model_copy(
                update={"model_provider": "OPENAI"}
            )
        }
    )

    with pytest.raises(AgentUnavailableError, match="not implemented"):
        service._build_client()
