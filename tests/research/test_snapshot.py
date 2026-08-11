"""Immutable research snapshots and history (brief sections 41, 42).

A research report is only worth storing if it can still be trusted months
later. That needs three things, and each is checked here: the file is written
once and never edited, the indexes are appended to and never rewritten, and
every report names the exact universe, prompt, model and data snapshots it
rests on. Together they make the chain

    universe -> research input -> research report

walkable in both directions after the configuration, the data and the model
have all moved on.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from trading_system.domain.enums import ResearchStatus
from trading_system.research.models import ResearchRunResult
from trading_system.research.store import ResearchStoreError

from .conftest import RESEARCH_NOW, FakeLLMClient

pytestmark = pytest.mark.unit


def _run(make_service, outlook_text, **kwargs):
    return make_service(llm_client=FakeLLMClient(outlook_text)).run(as_of=RESEARCH_NOW, **kwargs)


# ---------------------------------------------------------------------------
# 41. The audit chain
# ---------------------------------------------------------------------------
def test_a_report_names_the_universe_prompt_model_and_snapshots(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    universe = store_universe(["NVDA"])
    researchable_symbol("NVDA")

    report = _run(make_service, outlook_text).result.report("NVDA")

    assert report is not None
    assert report.universe_run_id == universe.run_id
    assert report.universe_snapshot_id == universe.snapshot_id
    assert report.input_snapshot_ids
    assert report.agent_metadata is not None
    assert report.agent_metadata.model_name == "fake-model-1"
    assert report.agent_metadata.prompt_version == "test-1.0.0"
    assert report.agent_metadata.prompt_fingerprint
    assert report.versions.application_version
    assert report.versions.config_version


def test_every_cited_snapshot_is_listed_on_the_report(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """Otherwise a conclusion could rest on evidence the report does not name."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")

    report = _run(make_service, outlook_text).result.report("NVDA")

    assert report is not None
    named = set(report.input_snapshot_ids)
    for item in report.evidence:
        assert item.source.snapshot_id in named


def test_the_raw_model_response_is_kept_as_evidence(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")

    report = _run(make_service, outlook_text).result.report("NVDA")

    assert report is not None
    assert report.agent_metadata is not None
    assert report.agent_metadata.raw_response
    assert json.loads(report.agent_metadata.raw_response)["hypothesis"] == "B"


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------
def test_a_stored_run_is_written_once(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    run = _run(make_service, outlook_text)

    stored = research_repo.get(run.result.run_id)

    assert stored is not None
    assert stored.model_dump(mode="json") == run.result.model_dump(mode="json")


def test_re_running_over_unchanged_inputs_is_idempotent(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    """A re-run records the same event, not a second one."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")

    first = _run(make_service, outlook_text)
    second = _run(make_service, outlook_text)

    assert first.result.run_id == second.result.run_id
    assert len(research_repo.history()) == 1


def test_overwriting_a_run_with_different_content_raises(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    """A record that changed after the fact is worse than no record."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    run = _run(make_service, outlook_text)

    tampered = run.result.model_copy(update={"status_detail": "edited after the fact"})

    with pytest.raises(ResearchStoreError, match="immutable"):
        research_repo.save(tampered)


def test_a_later_run_never_replaces_an_earlier_one(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    """Brief section 42: what did the agent believe on each date?"""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")

    first = _run(make_service, outlook_text)
    later = make_service(llm_client=FakeLLMClient(outlook_text)).run(
        as_of=RESEARCH_NOW + timedelta(days=1)
    )

    history = research_repo.history()
    assert len(history) == 2
    assert {entry.run_id for entry in history} == {first.result.run_id, later.result.run_id}
    assert research_repo.get(first.result.run_id) is not None


# ---------------------------------------------------------------------------
# 42. History, per run and per symbol
# ---------------------------------------------------------------------------
def test_run_history_is_newest_first(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    _run(make_service, outlook_text)
    make_service(llm_client=FakeLLMClient(outlook_text)).run(as_of=RESEARCH_NOW + timedelta(days=2))

    history = research_repo.history()

    assert history[0].generated_at >= history[1].generated_at


def test_symbol_history_answers_what_we_thought_over_time(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    _run(make_service, outlook_text)
    make_service(llm_client=FakeLLMClient(outlook_text)).run(as_of=RESEARCH_NOW + timedelta(days=3))

    entries = research_repo.symbol_history("NVDA")

    assert len(entries) == 2
    assert all(entry.symbol == "NVDA" for entry in entries)
    assert all(entry.hypothesis == "B" for entry in entries)
    assert all(entry.evidence_count > 0 for entry in entries)


def test_symbol_history_is_empty_for_an_unresearched_symbol(research_repo) -> None:
    assert research_repo.symbol_history("NOTHING") == []


def test_a_failed_report_still_appears_in_the_symbol_history(
    make_service, store_universe, research_repo
) -> None:
    """'We looked and could not form a view' is worth keeping."""
    store_universe(["NVDA"])
    make_service().run(as_of=RESEARCH_NOW)

    entries = research_repo.symbol_history("NVDA")

    assert len(entries) == 1
    assert entries[0].status == ResearchStatus.NO_DATA.value
    assert entries[0].hypothesis is None


# ---------------------------------------------------------------------------
# Round-tripping and schema drift
# ---------------------------------------------------------------------------
def test_a_stored_run_round_trips_through_json(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    run = _run(make_service, outlook_text)

    reloaded = ResearchRunResult.model_validate(
        json.loads(json.dumps(run.result.model_dump(mode="json")))
    )

    assert reloaded.run_id == run.result.run_id
    assert reloaded.report("NVDA") is not None


def test_a_stored_run_that_no_longer_matches_the_model_fails_loudly(
    make_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    """Schema drift must surface rather than flow into a decision."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    run = _run(make_service, outlook_text)

    path = next(research_repo.runs_dir.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["reports"][0]["unexpected_field"] = "from a future schema"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ResearchStoreError, match="schema drift"):
        research_repo.get(run.result.run_id)


def test_a_corrupt_history_line_fails_loudly(research_repo) -> None:
    research_repo.history_path.parent.mkdir(parents=True, exist_ok=True)
    research_repo.history_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ResearchStoreError, match="corrupt"):
        research_repo.history()


# ---------------------------------------------------------------------------
# One report per underlying per run
# ---------------------------------------------------------------------------
def test_a_run_refuses_two_reports_for_one_underlying(
    make_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    run = _run(make_service, outlook_text, dry_run=True)
    report = run.result.report("NVDA")
    assert report is not None

    with pytest.raises(ValueError, match="one report per underlying"):
        run.result.model_copy(update={"reports": [report, report]}).model_validate(
            {
                **run.result.model_dump(),
                "reports": [report.model_dump(), report.model_dump()],
            }
        )
