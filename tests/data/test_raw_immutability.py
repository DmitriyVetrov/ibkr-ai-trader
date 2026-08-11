"""Raw evidence survives everything downstream.

The pipeline is ``raw → normalized → quality``, and the arrows only point one
way. If a provider returns something malformed or implausible, the raw record
is what proves it did. Normalisation produces a *new* artifact; quality
produces a *verdict*; neither may reach back and change what was received.

This is not paranoia about mutable objects. It is the difference between "the
feed was wrong on 10 August" being demonstrable and being a memory.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system.data.models import RawRecord
from trading_system.data.normalizers import market_quote_from_broker
from trading_system.data.providers.market import SimulatedMarketDataProvider
from trading_system.data.quality import QualityContext
from trading_system.domain.enums import DataQualityIssue, DataType, MarketDataOrigin

pytestmark = pytest.mark.unit


def _raw(payload: object, data_now) -> RawRecord:
    from trading_system.data.hashing import payload_hash

    return RawRecord(
        provider="IBKR",
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        retrieved_at=data_now,
        payload=payload,
        payload_hash=payload_hash(payload),
    )


# ---------------------------------------------------------------------------
# The model itself is frozen
# ---------------------------------------------------------------------------
def test_a_raw_record_cannot_be_edited(data_now) -> None:
    record = _raw({"bid": 500.1, "volume": 999999999999999}, data_now)

    with pytest.raises(ValidationError):
        record.payload = {"bid": 1.0}  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.retrieved_at = data_now  # type: ignore[misc]


def test_a_canonical_record_cannot_be_edited(make_quote) -> None:
    quote = make_quote()

    with pytest.raises(ValidationError):
        quote.bid = Decimal("1")


def test_a_snapshot_cannot_be_edited(make_quote, quality_engine) -> None:
    from trading_system.data.repository import build_snapshot

    quote = make_quote()
    snapshot = build_snapshot(
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        records=[quote],
        provider="IBKR",
        source_tier=quote.source.source_tier,
        origin=quote.source.origin,
        as_of=quote.as_of,
        retrieved_at=quote.source.retrieved_at,
        quality=quality_engine.evaluate(quote),
    )

    with pytest.raises(ValidationError):
        snapshot.records = []  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Normalisation does not reach back
# ---------------------------------------------------------------------------
def test_normalisation_leaves_the_broker_snapshot_untouched(make_source) -> None:
    from datetime import UTC, datetime

    from trading_system.domain.enums import DataQuality, SecurityType
    from trading_system.domain.models import MarketDataSnapshot

    broker_snapshot = MarketDataSnapshot(
        symbol="SPY",
        security_type=SecurityType.STOCK,
        as_of=datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
        source="IBKR",
        origin=MarketDataOrigin.BROKER_DELAYED,
        data_quality=DataQuality.OK,
        bid=Decimal("500.10"),
        ask=Decimal("500.20"),
        volume=Decimal("999999999999"),
    )
    before = broker_snapshot.model_dump(mode="json")

    market_quote_from_broker(broker_snapshot, source=make_source())

    assert broker_snapshot.model_dump(mode="json") == before


def test_the_provider_keeps_raw_and_normalized_side_by_side(data_clock) -> None:
    result = SimulatedMarketDataProvider(clock=data_clock).fetch_quote("SPY")

    assert result.raw is not None
    assert result.records
    # Two distinct artifacts from one retrieval.
    assert result.raw.payload is not result.records[0]


# ---------------------------------------------------------------------------
# Quality assessment does not reach back
# ---------------------------------------------------------------------------
def test_flagging_a_suspicious_value_does_not_change_it(
    quality_engine, make_quote, data_now, data_config
) -> None:
    absurd = Decimal(data_config.plausibility.max_equity_daily_volume) * 7
    quote = make_quote(volume=absurd)
    before = quote.model_dump(mode="json")

    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.SUSPICIOUS_VOLUME)
    assert quote.model_dump(mode="json") == before
    assert quote.volume == absurd


def test_attaching_quality_produces_a_copy(quality_engine, make_quote, data_now) -> None:
    quote = make_quote(bid=Decimal("-1"))
    assessed = quality_engine.attach(quote, context=QualityContext(now=data_now))

    assert assessed is not quote
    assert assessed.bid == quote.bid == Decimal("-1")
    assert not assessed.quality.plausibility_valid
    assert quote.quality.plausibility_valid


# ---------------------------------------------------------------------------
# Storage preserves the raw bytes
# ---------------------------------------------------------------------------
def test_the_stored_raw_payload_matches_what_was_received(repository, tmp_path, data_now) -> None:
    payload = {
        "bid": 500.1,
        "ask": 500.2,
        # A value that cannot be a real session volume, kept exactly as sent.
        "volume": 91234567890123,
        "note": "verbatim",
    }
    repository.save_raw(_raw(payload, data_now))

    stored = json.loads(next((tmp_path / "data" / "raw").rglob("*.json")).read_text())
    assert stored["payload"] == payload
    assert stored["payload"]["volume"] == 91234567890123


def test_saving_the_same_raw_response_twice_does_not_duplicate_it(
    repository, tmp_path, data_now
) -> None:
    record = _raw({"bid": 500.1}, data_now)
    first = repository.save_raw(record)
    second = repository.save_raw(record)

    assert first == second
    assert len(list((tmp_path / "data" / "raw").rglob("*.json"))) == 1


def test_a_normalisation_run_does_not_rewrite_the_raw_file(
    repository, make_quote, tmp_path, data_now
) -> None:
    payload = {"bid": 500.1, "volume": 91234567890123}
    repository.save_raw(_raw(payload, data_now))
    raw_path = next((tmp_path / "data" / "raw").rglob("*.json"))
    before = raw_path.read_bytes()

    repository.save_normalized(
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        provider="IBKR",
        records=[make_quote()],
        as_of=data_now,
    )

    assert raw_path.read_bytes() == before


def test_the_raw_area_and_the_snapshot_area_are_separate(
    repository, make_quote, quality_engine, tmp_path, data_now
) -> None:
    """Cache, raw, normalized and snapshots must never share a directory."""
    from trading_system.data.repository import build_snapshot

    repository.save_raw(_raw({"bid": 1}, data_now))
    quote = make_quote()
    repository.save_snapshot(
        build_snapshot(
            data_type=DataType.MARKET_QUOTE,
            key="SPY",
            records=[quote],
            provider="IBKR",
            source_tier=quote.source.source_tier,
            origin=quote.source.origin,
            as_of=quote.as_of,
            retrieved_at=quote.source.retrieved_at,
            quality=quality_engine.evaluate(quote),
        )
    )

    root = tmp_path / "data"
    assert (root / "raw").is_dir()
    assert (root / "snapshots").is_dir()
    assert not any((root / "raw").rglob("*/snapshots/*"))
    assert not any((root / "snapshots").rglob("*/raw/*"))
