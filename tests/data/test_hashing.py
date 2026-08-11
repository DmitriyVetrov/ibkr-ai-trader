"""Deterministic payload hashing.

The hash answers one question: *is this the same information?* Everything else
about it follows from that — which fields are excluded, why identical content
collapses into one snapshot, and why the hash is never the sole identifier.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.data.hashing import (
    VOLATILE_KEYS,
    canonical_json,
    payload_hash,
    snapshot_identifier,
    stable_hash,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_the_same_payload_hashes_the_same_way() -> None:
    payload = {"bid": "500.10", "ask": "500.20"}
    assert stable_hash(payload) == stable_hash(dict(payload))


def test_key_order_does_not_matter() -> None:
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})


def test_list_order_does_matter() -> None:
    """A reordered option chain is a different chain."""
    assert stable_hash([1, 2, 3]) != stable_hash([3, 2, 1])


def test_decimal_precision_is_significant() -> None:
    """``5.10`` and ``5.1`` are the same number and different quoted prices."""
    assert stable_hash(Decimal("5.10")) != stable_hash(Decimal("5.1"))


def test_hashes_are_stable_across_types() -> None:
    """A string "1" must not hash like the integer 1."""
    assert stable_hash("1") != stable_hash(1)
    assert stable_hash(Decimal("1")) != stable_hash(1)


def test_dates_and_datetimes_are_distinguished() -> None:
    assert stable_hash(date(2026, 8, 10)) != stable_hash(datetime(2026, 8, 10, tzinfo=UTC))


# ---------------------------------------------------------------------------
# What is excluded, and why
# ---------------------------------------------------------------------------
def test_retrieval_time_does_not_change_the_hash(data_now) -> None:
    """When we fetched it is a fact about us, not about the market."""
    first = {"bid": "500.10", "retrieved_at": data_now.isoformat()}
    second = {"bid": "500.10", "retrieved_at": (data_now + timedelta(hours=3)).isoformat()}

    assert stable_hash(first) == stable_hash(second)


def test_our_own_observation_time_does_not_change_the_hash(data_now) -> None:
    """Otherwise every re-collection of an unchanged chain looks like history."""
    first = {"strikes": ["500"], "as_of": data_now.isoformat()}
    second = {"strikes": ["500"], "as_of": (data_now + timedelta(minutes=5)).isoformat()}

    assert stable_hash(first) == stable_hash(second)


def test_the_quality_verdict_does_not_change_the_hash() -> None:
    """Quality is our opinion about the data, not the data."""
    first = {"bid": "500.10", "quality": {"research_usable": True}}
    second = {"bid": "500.10", "quality": {"research_usable": False}}

    assert stable_hash(first) == stable_hash(second)


def test_the_sources_observation_clock_does_not_change_the_hash() -> None:
    """A re-quote at a new exchange time with identical values is not news."""
    first = {"bid": "500.10", "source_timestamp": "2026-08-10T14:30:00+00:00"}
    second = {"bid": "500.10", "source_timestamp": "2026-08-10T14:35:00+00:00"}

    assert stable_hash(first) == stable_hash(second)


def test_event_timestamps_do_change_the_hash() -> None:
    """A filing date or a publication time is information, and is hashed."""
    first = {"headline": "x", "published_at": "2026-08-09T13:05:00+00:00"}
    second = {"headline": "x", "published_at": "2026-08-10T13:05:00+00:00"}

    assert stable_hash(first) != stable_hash(second)


def test_a_changed_value_changes_the_hash() -> None:
    assert stable_hash({"bid": "500.10"}) != stable_hash({"bid": "500.11"})


def test_excluded_keys_are_stripped_at_any_depth(data_now) -> None:
    nested = {"records": [{"source": {"retrieved_at": data_now.isoformat(), "provider": "IBKR"}}]}
    rendered = canonical_json(nested)

    assert "retrieved_at" not in rendered
    assert "IBKR" in rendered


def test_the_excluded_set_is_explicit() -> None:
    """Anything excluded from the hash is a deliberate, reviewable choice."""
    assert "retrieved_at" in VOLATILE_KEYS
    assert "as_of" in VOLATILE_KEYS
    assert "source_timestamp" in VOLATILE_KEYS
    assert "quality" in VOLATILE_KEYS
    assert "bid" not in VOLATILE_KEYS
    assert "published_at" not in VOLATILE_KEYS


# ---------------------------------------------------------------------------
# Snapshot identity
# ---------------------------------------------------------------------------
def _identifier(**overrides: object) -> str:
    fields: dict[str, object] = {
        "data_type": "MARKET_QUOTE",
        "key": "SPY",
        "provider": "IBKR",
        "schema_version": "1.0.0",
        "as_of": datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
        "content_hash": "abc123",
    }
    fields.update(overrides)
    return snapshot_identifier(**fields)  # type: ignore[arg-type]


def test_the_identifier_is_deterministic() -> None:
    assert _identifier() == _identifier()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("data_type", "OPTION_CHAIN"),
        ("key", "QQQ"),
        ("provider", "SIMULATOR"),
        ("schema_version", "2.0.0"),
        ("as_of", datetime(2026, 8, 11, 14, 30, tzinfo=UTC)),
        ("content_hash", "def456"),
    ],
)
def test_every_component_changes_the_identifier(field: str, value: object) -> None:
    """The hash alone is not the identity — all five components are."""
    assert _identifier() != _identifier(**{field: value})


def test_the_identifier_is_a_readable_length() -> None:
    """These end up in filenames and get read by people."""
    identifier = _identifier()
    assert len(identifier) == 32
    assert identifier.isalnum()


# ---------------------------------------------------------------------------
# Awkward payloads
# ---------------------------------------------------------------------------
def test_a_raw_provider_float_still_hashes_stably() -> None:
    """The models reject floats; a stored raw response may still contain one."""
    assert stable_hash({"bid": 500.1}) == stable_hash({"bid": 500.1})


def test_none_is_distinguishable_from_zero() -> None:
    """The whole point of keeping unavailable values as ``None``."""
    assert stable_hash({"iv": None}) != stable_hash({"iv": 0})


def test_payload_hash_handles_a_list_of_records(make_quote) -> None:
    records = [make_quote().model_dump(mode="json")]
    assert payload_hash(records) == payload_hash(records)
    assert payload_hash(records) != payload_hash([])
