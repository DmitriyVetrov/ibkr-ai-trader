"""Immutable universe snapshots and append-only history (brief sections 25-26, 36).

Every run is stored and no run replaces another. The value of a universe
history is answering "what did the system consider on 10 August, and why did it
choose what it chose" *later* — after the configuration, the data and the model
have all moved on. A store that kept only the latest universe would make every
past research decision unexplainable.

The auditability tests are the other half: an asset can be selected only if it
carries the provenance of the data behind it. That is enforced by the model, so
an unexplainable selection cannot be constructed at all.
"""

from __future__ import annotations

import json

import pytest

from trading_system.domain.enums import (
    SelectionMethod,
    UniverseRejectionReason,
    UniverseSelectionStatus,
)
from trading_system.universe.models import (
    SelectedAsset,
    UniverseSelectionResult,
    universe_snapshot_id,
)
from trading_system.universe.store import UniverseStoreError

from .conftest import UNIVERSE_NOW, FakeLLMClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 25. The snapshot carries everything needed to explain the run
# ---------------------------------------------------------------------------
def test_a_stored_run_carries_every_required_field(
    optionable_symbols, make_service, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ"])), symbols=symbols
    )

    result = service.run(as_of=UNIVERSE_NOW).result

    assert result.snapshot_id
    assert result.run_id
    assert result.as_of == UNIVERSE_NOW
    assert result.generated_at
    assert result.input_snapshot_ids
    assert result.deterministic_filter_config.max_selected_assets == 10
    assert result.selected_assets and result.rejected_assets
    assert result.agent_metadata is not None
    assert result.agent_metadata.prompt_version == "test-1.0.0"
    assert result.schema_version
    assert result.versions.application_version


def test_the_filter_config_is_echoed_into_the_run(
    optionable_symbols, make_service, ranking_text
) -> None:
    """A run that referred to "the configuration" would stop explaining itself
    the moment that configuration changed."""
    optionable_symbols(["SPY"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"])),
        symbols=["SPY"],
        min_price="7.50",
    )

    stored = service.run(as_of=UNIVERSE_NOW).result.deterministic_filter_config

    assert str(stored.min_price) == "7.50"
    assert stored.optionability_policy == "REQUIRED"


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------
def test_a_stored_run_cannot_be_rewritten_with_different_content(
    optionable_symbols, make_service, ranking_text, universe_repo
) -> None:
    optionable_symbols(["SPY", "QQQ"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY", "QQQ"])
    result = service.run(as_of=UNIVERSE_NOW).result

    tampered = result.model_copy(update={"status_detail": "edited after the fact"})

    with pytest.raises(UniverseStoreError, match="immutable"):
        universe_repo.save(tampered)


def test_saving_the_same_run_twice_is_idempotent(
    optionable_symbols, make_service, ranking_text, universe_repo
) -> None:
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY"])
    result = service.run(as_of=UNIVERSE_NOW).result

    universe_repo.save(result)

    assert len(universe_repo.history()) == 1, "a re-save records one event, not two"


def test_a_run_result_is_frozen() -> None:
    """The permanent record cannot be edited in memory either."""
    from pydantic import ValidationError

    result = _minimal_result()

    with pytest.raises(ValidationError):
        result.status = UniverseSelectionStatus.NO_CANDIDATES  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 26. Historical universes accumulate
# ---------------------------------------------------------------------------
def test_a_later_run_does_not_replace_an_earlier_one(
    data_repo, store_quote, store_chain, make_service, ranking_text, universe_repo
) -> None:
    from datetime import timedelta

    store_quote("SPY")
    store_chain("SPY")
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY"])

    first = service.run(as_of=UNIVERSE_NOW).result
    later = UNIVERSE_NOW + timedelta(hours=1)
    store_quote("SPY", as_of=later, retrieved_at=later)
    second = service.run(as_of=later).result

    history = universe_repo.history()
    assert len(history) == 2
    assert {e.run_id for e in history} == {first.run_id, second.run_id}
    assert universe_repo.get(first.run_id) is not None, "the earlier run is still readable"


def test_history_is_newest_first(
    data_repo, store_quote, store_chain, make_service, ranking_text, universe_repo
) -> None:
    from datetime import timedelta

    from trading_system.infrastructure.clock import FixedClock

    store_quote("SPY")
    store_chain("SPY")
    clock = FixedClock(UNIVERSE_NOW)
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY"])
    service._clock = clock

    service.run(as_of=UNIVERSE_NOW)
    clock.advance(seconds=3600)
    later = UNIVERSE_NOW + timedelta(hours=1)
    store_quote("SPY", as_of=later, retrieved_at=later)
    service.run(as_of=later)

    history = universe_repo.history()
    assert history[0].generated_at > history[1].generated_at


def test_a_failed_run_is_recorded_too(optionable_symbols, make_service, universe_repo) -> None:
    """A failure that left no trace could not be investigated later."""
    from .conftest import UnavailableLLMClient

    optionable_symbols(["SPY"])
    service = make_service(llm_client=UnavailableLLMClient(), symbols=["SPY"])

    service.run(as_of=UNIVERSE_NOW)

    history = universe_repo.history()
    assert len(history) == 1
    assert history[0].status == "AI_UNAVAILABLE"
    assert history[0].selected_count == 0


def test_the_latest_run_is_retrievable(
    optionable_symbols, make_service, ranking_text, universe_repo
) -> None:
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY"])
    stored = service.run(as_of=UNIVERSE_NOW).result

    assert universe_repo.latest() is not None
    assert universe_repo.latest().run_id == stored.run_id


def test_reading_back_a_run_round_trips_exactly(
    optionable_symbols, make_service, ranking_text, universe_repo
) -> None:
    optionable_symbols(["SPY", "QQQ"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ"])),
        symbols=["SPY", "QQQ"],
    )
    stored = service.run(as_of=UNIVERSE_NOW).result

    loaded = universe_repo.get(stored.run_id)

    assert loaded is not None
    assert loaded.model_dump(mode="json") == stored.model_dump(mode="json")


def test_a_run_file_is_readable_json(
    optionable_symbols, make_service, ranking_text, universe_repo
) -> None:
    """The artifact is meant to be inspectable with ``cat`` and diffed in review."""
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY"])
    service.run(as_of=UNIVERSE_NOW)

    files = list(universe_repo.runs_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["selected_assets"][0]["symbol"] == "SPY"


# ---------------------------------------------------------------------------
# 36. Auditability — nothing selected is unexplainable
# ---------------------------------------------------------------------------
def test_a_selected_asset_cannot_be_constructed_without_provenance() -> None:
    """Enforced by the model, so an unexplainable selection cannot exist."""
    from trading_system.domain.enums import (
        ConfidenceLevel,
        DataQuality,
        UniverseEligibility,
        UniverseSelectionReason,
    )
    from trading_system.universe.models import DataQualitySummary

    with pytest.raises(ValueError, match="must name the data snapshots"):
        SelectedAsset(
            symbol="SPY",
            rank=1,
            deterministic_eligibility=UniverseEligibility.ELIGIBLE,
            reasons=[UniverseSelectionReason.SUFFICIENT_DATA_QUALITY],
            data_quality=DataQualitySummary(research_usable=True, classification=DataQuality.OK),
            confidence=ConfidenceLevel.HIGH,
            source=None,
        )


def test_every_selected_asset_names_the_snapshots_behind_it(
    optionable_symbols, make_service, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(symbols)), symbols=symbols, max_selected=2
    )

    result = service.run(as_of=UNIVERSE_NOW).result

    for asset in result.selected_assets:
        assert asset.source is not None
        assert asset.source.snapshot_ids
        assert asset.source.provider
        assert asset.reasons


def test_every_rejected_asset_carries_a_machine_readable_reason(
    optionable_symbols, store_quote, make_service, ranking_text
) -> None:
    from decimal import Decimal

    symbols = optionable_symbols(["SPY", "QQQ"])
    store_quote("PENNY", last=Decimal("0.10"), close=None, bid=None, ask=None)
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ"])),
        symbols=[*symbols, "PENNY", "MISSING"],
    )

    result = service.run(as_of=UNIVERSE_NOW).result

    reasons = {a.symbol: a.reason for a in result.rejected_assets}
    assert reasons["QQQ"] is UniverseRejectionReason.NOT_SELECTED_BY_RANKING
    assert reasons["PENNY"] is UniverseRejectionReason.PRICE_BELOW_MINIMUM
    assert reasons["MISSING"] is UniverseRejectionReason.DATA_UNAVAILABLE


def test_the_run_names_every_data_snapshot_it_consumed(
    optionable_symbols, make_service, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=symbols)

    result = service.run(as_of=UNIVERSE_NOW).result

    # Two symbols, each with a quote and a chain snapshot.
    assert len(result.input_snapshot_ids) == 4
    assert result.input_snapshot_ids == sorted(result.input_snapshot_ids)


# ---------------------------------------------------------------------------
# The snapshot id is derived, not generated
# ---------------------------------------------------------------------------
def test_the_snapshot_id_is_derived_from_content() -> None:
    from trading_system.universe.models import FilterConfigSnapshot, UniverseSourceRef

    source = UniverseSourceRef(kind="STATIC", name="t", version="1")
    config = FilterConfigSnapshot(max_candidates=10, max_selected_assets=5)

    first = universe_snapshot_id(
        run_id="r", as_of=UNIVERSE_NOW, source=source, filter_config=config, selected=[]
    )
    second = universe_snapshot_id(
        run_id="r", as_of=UNIVERSE_NOW, source=source, filter_config=config, selected=[]
    )
    different = universe_snapshot_id(
        run_id="r2", as_of=UNIVERSE_NOW, source=source, filter_config=config, selected=[]
    )

    assert first == second
    assert first != different


def _minimal_result() -> UniverseSelectionResult:
    from trading_system.domain.models import SystemVersions
    from trading_system.universe.models import FilterConfigSnapshot, UniverseSourceRef

    return UniverseSelectionResult(
        snapshot_id="snap",
        run_id="run",
        as_of=UNIVERSE_NOW,
        generated_at=UNIVERSE_NOW,
        status=UniverseSelectionStatus.SUCCESS,
        selection_method=SelectionMethod.DETERMINISTIC_ONLY,
        universe_source=UniverseSourceRef(kind="STATIC", name="t", version="1"),
        deterministic_filter_config=FilterConfigSnapshot(max_candidates=10, max_selected_assets=5),
        versions=SystemVersions(application_version="0.1.0", config_version="test"),
    )
