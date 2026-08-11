"""Reproducibility of a universe run (brief section 35).

Given the same stored data, the same configuration and the same model response,
the canonical output must be identical — byte for byte, not merely equivalent.
That is what turns "this system is auditable" from a claim into something a
test can fail on.

The ranking model is probabilistic; the *contract* is not. Everything
deterministic about a run — the identifier, the ordering, the reasons, the
provenance, the counts — is derived from inputs, so replaying a stored response
reconstructs the run exactly.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings
from trading_system.universe.service import UniverseSelectionService
from trading_system.universe.store import FilesystemUniverseRepository

from .conftest import UNIVERSE_NOW, FakeLLMClient

pytestmark = pytest.mark.unit


def test_identical_inputs_produce_byte_identical_output(
    optionable_symbols, make_service, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA"])
    text = ranking_text(["NVDA", "SPY"], not_selected=["QQQ"])

    first = make_service(llm_client=FakeLLMClient(text), symbols=symbols).run(
        as_of=UNIVERSE_NOW, dry_run=True
    )
    second = make_service(llm_client=FakeLLMClient(text), symbols=symbols).run(
        as_of=UNIVERSE_NOW, dry_run=True
    )

    assert first.result.model_dump(mode="json") == second.result.model_dump(mode="json")


def test_the_run_id_is_derived_and_therefore_stable(
    optionable_symbols, make_service, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY"])
    text = ranking_text(["SPY"])

    ids = {
        make_service(llm_client=FakeLLMClient(text), symbols=symbols)
        .run(as_of=UNIVERSE_NOW, dry_run=True)
        .result.run_id
        for _ in range(3)
    }

    assert len(ids) == 1, "a run over unchanged inputs is the same run"


def test_new_data_at_the_same_instant_is_a_different_run(
    data_repo, store_quote, store_chain, make_service, ranking_text
) -> None:
    """Idempotence must not collapse two genuinely different observations.

    Both quotes are visible at the reconstruction instant; the later one wins.
    The run identity must move with the evidence, or a fresh collection would be
    silently recorded as a repeat of the previous run.
    """
    earlier = UNIVERSE_NOW - timedelta(minutes=10)
    store_quote("SPY", as_of=earlier, retrieved_at=earlier)
    store_chain("SPY", as_of=earlier, retrieved_at=earlier)
    text = ranking_text(["SPY"])

    before = make_service(llm_client=FakeLLMClient(text), symbols=["SPY"]).run(
        as_of=UNIVERSE_NOW, dry_run=True
    )

    later = UNIVERSE_NOW - timedelta(minutes=1)
    store_quote("SPY", last=Decimal("512.34"), as_of=later, retrieved_at=later)
    after = make_service(llm_client=FakeLLMClient(text), symbols=["SPY"]).run(
        as_of=UNIVERSE_NOW, dry_run=True
    )

    assert after.result.selected_assets[0].reference_price == Decimal("512.34")
    assert before.result.run_id != after.result.run_id


def test_a_changed_filter_threshold_changes_the_run_id(
    optionable_symbols, make_service, ranking_text
) -> None:
    """Two runs made under different policies must not share an identity."""
    symbols = optionable_symbols(["SPY"])
    text = ranking_text(["SPY"])

    strict = make_service(
        llm_client=FakeLLMClient(text), symbols=symbols, min_price=Decimal("5.00")
    ).run(as_of=UNIVERSE_NOW, dry_run=True)
    lenient = make_service(
        llm_client=FakeLLMClient(text), symbols=symbols, min_price=Decimal("1.00")
    ).run(as_of=UNIVERSE_NOW, dry_run=True)

    assert strict.result.run_id != lenient.result.run_id


def test_a_different_ranking_changes_the_snapshot_but_not_the_run_id(
    optionable_symbols, make_service, ranking_text
) -> None:
    """The run is what was asked; the snapshot is what was answered."""
    symbols = optionable_symbols(["SPY", "QQQ"])

    one = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ"])), symbols=symbols
    ).run(as_of=UNIVERSE_NOW, dry_run=True)
    other = make_service(
        llm_client=FakeLLMClient(ranking_text(["QQQ"], not_selected=["SPY"])), symbols=symbols
    ).run(as_of=UNIVERSE_NOW, dry_run=True)

    assert one.result.run_id == other.result.run_id
    assert one.result.snapshot_id != other.result.snapshot_id


def test_replaying_a_stored_response_reconstructs_the_run(
    optionable_symbols, make_universe_config, data_repo, tmp_path, ranking_text
) -> None:
    """The retained raw response is enough to rebuild the whole result.

    This is the point of storing it: months later, with the model long since
    updated, the stored universe can still be re-derived from evidence rather
    than trusted.
    """
    symbols = optionable_symbols(["SPY", "QQQ"])
    text = ranking_text(["SPY"], not_selected=["QQQ"])
    config = make_universe_config(symbols=symbols)

    def _service(store_name: str) -> UniverseSelectionService:
        return UniverseSelectionService(
            settings=Settings(_env_file=None),
            config=config,
            clock=FixedClock(UNIVERSE_NOW),
            data_repository=data_repo,
            universe_repository=FilesystemUniverseRepository(tmp_path / store_name),
            llm_client=FakeLLMClient(text),
        )

    original = _service("first").run(as_of=UNIVERSE_NOW).result
    raw = original.agent_metadata.raw_response  # type: ignore[union-attr]
    assert raw is not None

    replayed = (
        UniverseSelectionService(
            settings=Settings(_env_file=None),
            config=config,
            clock=FixedClock(UNIVERSE_NOW),
            data_repository=data_repo,
            universe_repository=FilesystemUniverseRepository(tmp_path / "replay"),
            llm_client=FakeLLMClient(raw),
        )
        .run(as_of=UNIVERSE_NOW)
        .result
    )

    assert replayed.model_dump(mode="json") == original.model_dump(mode="json")


def test_generated_at_comes_from_the_clock_not_wall_time(
    optionable_symbols, make_service, ranking_text
) -> None:
    """Otherwise nothing downstream of it could ever be reproducible."""
    optionable_symbols(["SPY"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(["SPY"])), symbols=["SPY"])

    result = service.run(as_of=UNIVERSE_NOW, dry_run=True).result

    assert result.generated_at == UNIVERSE_NOW


def test_the_deterministic_ordering_is_also_reproducible(optionable_symbols, make_service) -> None:
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA"])

    runs = [
        make_service(symbols=symbols, ai_enabled=False, max_selected=2)
        .run(as_of=UNIVERSE_NOW, dry_run=True)
        .result.model_dump(mode="json")
        for _ in range(3)
    ]

    assert runs[0] == runs[1] == runs[2]


def test_rejections_are_stored_in_a_stable_order(
    optionable_symbols, make_service, ranking_text
) -> None:
    """An unordered list would make two identical runs diff against each other."""
    symbols = optionable_symbols(["SPY", "QQQ", "NVDA"])
    service = make_service(
        llm_client=FakeLLMClient(ranking_text(["SPY"], not_selected=["NVDA", "QQQ"])),
        symbols=symbols,
    )

    rejected = service.run(as_of=UNIVERSE_NOW, dry_run=True).result.rejected_assets

    assert [a.symbol for a in rejected] == sorted(a.symbol for a in rejected)


def test_a_universe_run_is_projectable_onto_the_research_boundary(
    optionable_symbols, make_service, ranking_text
) -> None:
    """The next stage consumes ``schemas/universe_selection.json``, not the audit record."""
    symbols = optionable_symbols(["SPY", "QQQ"])
    service = make_service(llm_client=FakeLLMClient(ranking_text(symbols)), symbols=symbols)

    selection = service.run(as_of=UNIVERSE_NOW, dry_run=True).result.to_universe_selection()

    assert [c.ticker for c in selection.candidates] == symbols
    assert [c.rank for c in selection.candidates] == [1, 2]
    assert selection.as_of == UNIVERSE_NOW


def test_the_projection_supplies_a_score_when_the_ranking_gave_none(
    optionable_symbols, make_service, ranking_text
) -> None:
    """No stage invents a confidence, but the boundary still needs a comparable number."""
    import json

    symbols = optionable_symbols(["SPY", "QQQ"])
    payload = json.loads(ranking_text(symbols))
    for entry in payload["rankings"]:
        entry["selection_score"] = None
    service = make_service(llm_client=FakeLLMClient(json.dumps(payload)), symbols=symbols)

    selection = service.run(as_of=UNIVERSE_NOW, dry_run=True).result.to_universe_selection()

    scores = [c.selection_score for c in selection.candidates]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 100.0 for s in scores)


def test_a_later_instant_is_a_different_run(
    data_repo, store_quote, store_chain, make_service, ranking_text
) -> None:
    store_quote("SPY")
    store_chain("SPY")
    text = ranking_text(["SPY"])
    service = make_service(llm_client=FakeLLMClient(text), symbols=["SPY"])

    first = service.run(as_of=UNIVERSE_NOW, dry_run=True).result
    second = service.run(as_of=UNIVERSE_NOW + timedelta(minutes=5), dry_run=True).result

    assert first.run_id != second.run_id
