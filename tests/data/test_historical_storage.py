"""Historical accumulation: the store grows, it never overwrites.

Day 1 plus day 2 plus day 3 must equal three days of history, not the third
day. That sounds obvious and is exactly what a "save the latest value" store
gets wrong — and once a day is gone it cannot be recovered, because free
historical option data does not exist to backfill from.

The counterpart property is idempotence: collecting the same unchanged
response twice must not manufacture a second day of history either.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.data.models import SNAPSHOT_CREATED, SNAPSHOT_REOBSERVED
from trading_system.data.repository import build_snapshot
from trading_system.domain.enums import DataType

pytestmark = pytest.mark.unit


def _snapshot(quote, quality):
    return build_snapshot(
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        records=[quote],
        provider=quote.source.provider,
        source_tier=quote.source.source_tier,
        origin=quote.source.origin,
        as_of=quote.as_of,
        retrieved_at=quote.source.retrieved_at,
        source_timestamp=quote.source.source_timestamp,
        quality=quality,
    )


def _daily_quotes(make_quote, data_now, prices):
    """One quote per day, each with its own observation and retrieval time."""
    quotes = []
    for offset, price in enumerate(prices):
        instant = data_now + timedelta(days=offset)
        quotes.append(
            make_quote(
                as_of=instant,
                retrieved_at=instant,
                source_timestamp=instant,
                last=price,
                bid=price - Decimal("0.05"),
                ask=price + Decimal("0.05"),
                volume=Decimal("70000000") + Decimal(offset),
            )
        )
    return quotes


# ---------------------------------------------------------------------------
# A + B + C, not C
# ---------------------------------------------------------------------------
def test_three_days_of_collection_produce_three_snapshots(
    repository, make_quote, quality_engine, data_now
) -> None:
    prices = [Decimal("500.00"), Decimal("503.50"), Decimal("498.25")]
    for quote in _daily_quotes(make_quote, data_now, prices):
        result = repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))
        assert result.created

    stored = repository.get_range(
        DataType.MARKET_QUOTE, "SPY", data_now, data_now + timedelta(days=5)
    )
    assert len(stored) == 3
    assert [s.records[0]["last"] for s in stored] == ["500.00", "503.50", "498.25"]


def test_the_ledger_records_every_day(repository, make_quote, quality_engine, data_now) -> None:
    for quote in _daily_quotes(
        make_quote, data_now, [Decimal("500"), Decimal("501"), Decimal("502")]
    ):
        repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    entries = repository.ledger(DataType.MARKET_QUOTE, "SPY")
    created = [e for e in entries if e.event == SNAPSHOT_CREATED]
    assert len(created) == 3
    assert len({e.payload_hash for e in created}) == 3


def test_an_earlier_snapshot_is_still_readable_after_later_ones(
    repository, make_quote, quality_engine, data_now
) -> None:
    """Yesterday must not be reachable only through today."""
    quotes = _daily_quotes(make_quote, data_now, [Decimal("500"), Decimal("510"), Decimal("520")])
    for quote in quotes:
        repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    first_day = repository.get_as_of(DataType.MARKET_QUOTE, "SPY", data_now)
    assert first_day is not None
    assert first_day.records[0]["last"] == "500"


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------
def test_recollecting_an_unchanged_response_creates_no_second_snapshot(
    repository, make_quote, quality_engine, data_now
) -> None:
    quote = make_quote()
    snapshot = _snapshot(quote, quality_engine.evaluate(quote))

    first = repository.save_snapshot(snapshot)
    second = repository.save_snapshot(snapshot)

    assert first.created
    assert not second.created
    assert second.reason in {"UNCHANGED", "ALREADY_STORED"}
    assert (
        len(
            repository.get_range(
                DataType.MARKET_QUOTE,
                "SPY",
                data_now - timedelta(days=1),
                data_now + timedelta(days=1),
            )
        )
        == 1
    )


def test_a_re_observation_is_still_recorded_in_the_ledger(
    repository, make_quote, quality_engine, data_now
) -> None:
    """ "We looked again and nothing had changed" is itself information."""
    quote = make_quote()
    snapshot = _snapshot(quote, quality_engine.evaluate(quote))
    repository.save_snapshot(snapshot)
    repository.save_snapshot(snapshot)

    events = [e.event for e in repository.ledger(DataType.MARKET_QUOTE, "SPY")]
    assert events == [SNAPSHOT_CREATED, SNAPSHOT_REOBSERVED]


def test_a_changed_response_is_never_deduplicated_away(
    repository, make_quote, quality_engine, data_now
) -> None:
    """Idempotence must not swallow a real change."""
    first = make_quote(last=Decimal("500.00"))
    repository.save_snapshot(_snapshot(first, quality_engine.evaluate(first)))

    later = data_now + timedelta(minutes=5)
    second = make_quote(
        as_of=later, retrieved_at=later, source_timestamp=later, last=Decimal("500.01")
    )
    result = repository.save_snapshot(_snapshot(second, quality_engine.evaluate(second)))

    assert result.created
    assert (
        len(
            repository.get_range(
                DataType.MARKET_QUOTE, "SPY", data_now, later + timedelta(minutes=1)
            )
        )
        == 2
    )


def test_a_value_that_changes_and_returns_is_stored_three_times(
    repository, make_quote, quality_engine, data_now
) -> None:
    """A round trip back to the original value is still three observations."""
    prices = [Decimal("500"), Decimal("505"), Decimal("500")]
    for offset, price in enumerate(prices):
        instant = data_now + timedelta(minutes=offset)
        quote = make_quote(
            as_of=instant, retrieved_at=instant, source_timestamp=instant, last=price
        )
        repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    created = [
        e for e in repository.ledger(DataType.MARKET_QUOTE, "SPY") if e.event == SNAPSHOT_CREATED
    ]
    assert len(created) == 3


# ---------------------------------------------------------------------------
# Failures never destroy history
# ---------------------------------------------------------------------------
def test_a_failure_leaves_earlier_snapshots_intact(
    repository, make_quote, quality_engine, data_now
) -> None:
    quote = make_quote()
    repository.save_snapshot(_snapshot(quote, quality_engine.evaluate(quote)))

    repository.record_failure(
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        provider="IBKR",
        outcome="PROVIDER_UNAVAILABLE",
        detail="gateway not running",
    )

    latest = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert latest is not None
    assert latest.records[0]["last"] == "500.15"

    entries = repository.ledger(DataType.MARKET_QUOTE, "SPY")
    assert [e.event for e in entries] == [SNAPSHOT_CREATED, "COLLECTION_FAILED"]
    assert entries[-1].outcome == "PROVIDER_UNAVAILABLE"


def test_history_starts_empty_and_that_is_a_valid_state(repository) -> None:
    """Coverage starting at zero is the expected initial condition."""
    assert repository.get_latest(DataType.OPTION_CHAIN, "SPY") is None
    assert repository.ledger(DataType.OPTION_CHAIN, "SPY") == []
    assert repository.keys(DataType.OPTION_CHAIN) == []
