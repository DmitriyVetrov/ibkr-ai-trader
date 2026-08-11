"""Repository behaviour: immutability, integrity, and a filesystem-free API.

Two things are being protected here. First, that a snapshot once written is
evidence and cannot be edited — including by the code that wrote it. Second,
that consumers never learn what the storage looks like, so SQLite or PostgreSQL
stays a later implementation choice rather than a migration project.
"""

from __future__ import annotations

import inspect
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.data.models import MarketQuote, RawRecord
from trading_system.data.repository import (
    DataRepository,
    SnapshotIntegrityError,
    build_snapshot,
    records_of,
)
from trading_system.domain.enums import DataType

pytestmark = pytest.mark.unit


def _snapshot(quote, quality, *, key="SPY"):
    return build_snapshot(
        data_type=DataType.MARKET_QUOTE,
        key=key,
        records=[quote],
        provider=quote.source.provider,
        source_tier=quote.source.source_tier,
        origin=quote.source.origin,
        as_of=quote.as_of,
        retrieved_at=quote.source.retrieved_at,
        source_timestamp=quote.source.source_timestamp,
        quality=quality,
        config_version="2026.08.11-1",
    )


# ---------------------------------------------------------------------------
# Interface hygiene
# ---------------------------------------------------------------------------
def test_the_interface_exposes_no_storage_details() -> None:
    """A signature mentioning a Path would leak the filesystem to consumers."""
    for name, member in inspect.getmembers(DataRepository, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        signature = inspect.signature(member)
        rendered = str(signature)
        assert "Path" not in rendered, f"{name} leaks a filesystem type"
        assert "sql" not in rendered.lower(), f"{name} leaks a SQL detail"


def test_the_repository_supports_the_required_operations() -> None:
    required = {
        "save_raw",
        "save_normalized",
        "save_snapshot",
        "get_latest",
        "get_as_of",
        "get_range",
        "exists",
        "list_snapshots",
    }
    assert required <= set(dir(DataRepository))


# ---------------------------------------------------------------------------
# Snapshot identity and integrity
# ---------------------------------------------------------------------------
def test_snapshot_ids_are_deterministic(make_quote, quality_engine) -> None:
    """Identical inputs must yield the same id, or dedup is impossible."""
    quote = make_quote()
    quality = quality_engine.evaluate(quote)

    assert _snapshot(quote, quality).snapshot_id == _snapshot(quote, quality).snapshot_id


def test_snapshot_ids_differ_across_symbols(make_quote, quality_engine) -> None:
    quote = make_quote()
    quality = quality_engine.evaluate(quote)

    spy = _snapshot(quote, quality, key="SPY")
    qqq = _snapshot(quote, quality, key="QQQ")
    assert spy.snapshot_id != qqq.snapshot_id


def test_a_snapshot_carries_its_version_stamps(make_quote, quality_engine) -> None:
    from trading_system import __version__

    snapshot = _snapshot(make_quote(), quality_engine.evaluate(make_quote()))

    assert snapshot.schema_version
    assert snapshot.application_version == __version__
    assert snapshot.config_version == "2026.08.11-1"


def test_a_tampered_snapshot_file_is_detected(
    repository, make_quote, quality_engine, tmp_path
) -> None:
    """A snapshot that changed on disk is worse than a missing one."""
    quote = make_quote()
    repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    stored = next((tmp_path / "data" / "snapshots").rglob("*.json"))
    payload = json.loads(stored.read_text(encoding="utf-8"))
    payload["records"][0]["last"] = "999.99"
    stored.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError, match="payload hash"):
        repository.get_latest(DataType.MARKET_QUOTE, "SPY")


def test_a_snapshot_whose_hash_does_not_describe_its_records_is_refused(
    repository, make_quote, quality_engine
) -> None:
    """The hash is checked on the way in, not only on the way out.

    Deduplication compares hashes, so a snapshot carrying a hash that does not
    match its own payload could otherwise masquerade as an unchanged
    re-observation and suppress a real one.
    """
    quote = make_quote()
    original = _snapshot(quote, quality_engine.evaluate(quote))
    forged = original.model_copy(update={"records": [{"tampered": True}], "record_count": 1})

    with pytest.raises(SnapshotIntegrityError, match="declares payload_hash"):
        repository.save_snapshot(forged)


def test_a_snapshot_id_is_never_reused_for_different_content(
    repository, make_quote, quality_engine
) -> None:
    """Same id and instant, genuinely different payload: refused, not merged."""
    from trading_system.data.hashing import payload_hash

    quote = make_quote()
    original = _snapshot(quote, quality_engine.evaluate(quote))
    repository.save_snapshot(original)

    different_records = [{"tampered": True}]
    collision = original.model_copy(
        update={
            "records": different_records,
            "record_count": 1,
            "payload_hash": payload_hash(different_records),
        }
    )
    with pytest.raises(SnapshotIntegrityError, match="immutable"):
        repository.save_snapshot(collision)


def test_exists_reports_stored_snapshots(repository, make_quote, quality_engine) -> None:
    quote = make_quote()
    snapshot = _snapshot(quote, quality_engine.evaluate(quote))
    repository.save_snapshot(snapshot)

    assert repository.exists(DataType.MARKET_QUOTE, "SPY", snapshot.snapshot_id)
    assert not repository.exists(DataType.MARKET_QUOTE, "SPY", "nonexistent")


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------
def test_records_round_trip_through_storage(repository, make_quote, quality_engine) -> None:
    quote = make_quote()
    repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None
    restored = records_of(stored, MarketQuote)

    assert len(restored) == 1
    assert restored[0].bid == quote.bid
    assert restored[0].as_of == quote.as_of
    assert restored[0].source.provider == quote.source.provider


def test_reading_the_wrong_record_type_fails_loudly(repository, make_quote, quality_engine) -> None:
    from trading_system.data.models import RegulatoryEvent

    quote = make_quote()
    repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))
    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None

    with pytest.raises(SnapshotIntegrityError):
        records_of(stored, RegulatoryEvent)


def test_decimals_survive_the_round_trip_exactly(repository, make_quote, quality_engine) -> None:
    """A price that came back as a float would be a silent accounting bug."""
    quote = make_quote(bid=Decimal("500.10"), ask=Decimal("500.20"))
    repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None
    restored = records_of(stored, MarketQuote)[0]

    assert isinstance(restored.bid, Decimal)
    assert restored.bid == Decimal("500.10")
    assert str(restored.ask) == "500.20"


def test_list_snapshots_returns_metadata_newest_first(
    repository, make_quote, quality_engine, data_now
) -> None:
    for offset in range(3):
        instant = data_now + timedelta(hours=offset)
        quote = make_quote(
            as_of=instant,
            retrieved_at=instant,
            source_timestamp=instant,
            last=Decimal(f"50{offset}"),
        )
        repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    entries = repository.list_snapshots(DataType.MARKET_QUOTE, "SPY")
    assert len(entries) == 3
    assert entries[0].as_of > entries[-1].as_of
    # Metadata only: no payload was loaded to answer this.
    assert all(entry.snapshot_id for entry in entries)


def test_get_range_is_inclusive_of_its_bounds(
    repository, make_quote, quality_engine, data_now
) -> None:
    instants = [data_now, data_now + timedelta(hours=1), data_now + timedelta(hours=2)]
    for index, instant in enumerate(instants):
        quote = make_quote(
            as_of=instant,
            retrieved_at=instant,
            source_timestamp=instant,
            last=Decimal(f"50{index}"),
        )
        repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    ranged = repository.get_range(DataType.MARKET_QUOTE, "SPY", instants[0], instants[1])
    assert [s.as_of for s in ranged] == instants[:2]


# ---------------------------------------------------------------------------
# Raw and normalized areas
# ---------------------------------------------------------------------------
def test_raw_and_normalized_are_stored_separately(
    repository, make_quote, quality_engine, tmp_path, data_now
) -> None:
    payload = {"bid": 500.1, "ask": 500.2, "volume": "impossible"}
    repository.save_raw(
        RawRecord(
            provider="IBKR",
            data_type=DataType.MARKET_QUOTE,
            key="SPY",
            retrieved_at=data_now,
            payload=payload,
            payload_hash="abcdef0123456789",
        )
    )
    quote = make_quote()
    repository.save_normalized(
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        provider="IBKR",
        records=[quote],
        as_of=data_now,
    )

    raw_files = list((tmp_path / "data" / "raw").rglob("*.json"))
    normalized_files = list((tmp_path / "data" / "normalized").rglob("*.json"))
    assert len(raw_files) == 1
    assert len(normalized_files) == 1
    assert raw_files[0].parent != normalized_files[0].parent


def test_collection_state_is_stored_and_read_back(repository, data_now) -> None:
    from trading_system.data.models import CollectionState

    state = CollectionState(
        provider="IBKR",
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        last_attempt=data_now,
        last_successful_collection=data_now,
        records_collected=4,
        snapshot_count=2,
    )
    repository.save_collection_state(state)

    loaded = repository.get_collection_state(
        provider="IBKR", data_type=DataType.MARKET_QUOTE, key="SPY"
    )
    assert loaded == state
    assert repository.list_collection_states() == [state]


def test_an_awkward_key_cannot_collide_with_a_sanitised_one(repository, data_now) -> None:
    """``BRK/B`` and ``BRK_B`` are different instruments, not one directory."""
    for key in ("BRK/B", "BRK_B"):
        repository.record_failure(
            data_type=DataType.MARKET_QUOTE,
            key=key,
            provider="IBKR",
            outcome="NO_DATA",
        )

    assert repository.ledger(DataType.MARKET_QUOTE, "BRK/B")[0].key == "BRK/B"
    assert repository.ledger(DataType.MARKET_QUOTE, "BRK_B")[0].key == "BRK_B"
    assert sorted(repository.keys(DataType.MARKET_QUOTE)) == ["BRK/B", "BRK_B"]
