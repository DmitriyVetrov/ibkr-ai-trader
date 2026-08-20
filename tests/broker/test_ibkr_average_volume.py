"""IBKR tick 21 (``avVolume``) and the corrupted tick 74 beside it.

The finding these tests exist for, captured at the wire level on 2026-08-15
against IB Gateway 10.45 with ``ib_async`` 2.1.0 and ``marketDataType=3``::

    <<< 2,6,3,74,31367915626456     SPY DELAYED_VOLUME  -- absurd
    <<< 2,6,3,21,52014430           SPY avVolume        -- clean

Both numbers arrive on the same message type, on the same connection, in the
same session. IBKR sends the bad one; ``ib_async`` decodes it with a bare
``float()`` and transforms nothing. So there is no library bug to work around
and no sentinel to filter — only a broker field that cannot be trusted, next
to one that can.

What must never happen here: a correction. The inflation is roughly a million
on most symbols and demonstrably something else on at least one, so any fixed
divisor would be wrong by an order of magnitude somewhere, silently, in the
direction that *passes* a liquidity floor.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.broker.ibkr.market_data import to_market_data_snapshot

from .conftest import BROKER_NOW, NAN, make_ticker

#: The exact values captured off the wire for SPY on 2026-08-15.
OBSERVED_CORRUPT_TICK_74 = 31367915626456.0
OBSERVED_TICK_21 = 52014430.0


@pytest.mark.unit
def test_average_daily_volume_comes_from_av_volume() -> None:
    """Requirement A: avVolume=52_014_430 -> average_daily_volume=52_014_430."""
    ticker = make_ticker(average_daily_volume=52_014_430.0)

    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.average_daily_volume == Decimal("52014430")


@pytest.mark.unit
def test_raw_session_volume_is_preserved_exactly() -> None:
    """Requirement B: a suspicious session volume survives byte for byte."""
    ticker = make_ticker(volume=1_763_051_657.0)

    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.volume == Decimal("1763051657")


@pytest.mark.unit
def test_the_observed_corrupt_value_is_stored_not_repaired() -> None:
    """The real capture, end to end: preserved, and not turned into anything."""
    ticker = make_ticker(volume=OBSERVED_CORRUPT_TICK_74, average_daily_volume=OBSERVED_TICK_21)

    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.volume == Decimal("31367915626456")
    assert snapshot.average_daily_volume == Decimal("52014430")


@pytest.mark.unit
def test_nothing_is_divided_by_a_million() -> None:
    """Requirement C: no rescaling, in either direction, on either field.

    Stated as an explicit inequality rather than only as an equality, because
    the failure this guards against is a *plausible-looking* helper that
    "normalises" the field on the way through. The plausible number is the
    wrong one.
    """
    ticker = make_ticker(volume=OBSERVED_CORRUPT_TICK_74, average_daily_volume=OBSERVED_TICK_21)

    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.volume != Decimal("31367915.626456")
    assert snapshot.volume != Decimal(OBSERVED_CORRUPT_TICK_74) / Decimal(1_000_000)
    assert snapshot.average_daily_volume != Decimal(OBSERVED_TICK_21) / Decimal(1_000_000)
    assert snapshot.average_daily_volume != Decimal(OBSERVED_TICK_21) * Decimal(1_000_000)


@pytest.mark.unit
def test_a_missing_average_volume_is_none_not_a_substitute() -> None:
    """Requirement D: absent means absent. It never borrows from `volume`."""
    ticker = make_ticker(volume=1500.0, average_daily_volume=NAN)

    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.average_daily_volume is None
    assert snapshot.volume == Decimal("1500")


@pytest.mark.unit
def test_an_absent_av_volume_attribute_is_none() -> None:
    """A ticker that never carried the field at all — generic tick 165 unasked."""
    ticker = make_ticker()
    del ticker.avVolume

    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.average_daily_volume is None


@pytest.mark.unit
def test_a_negative_average_volume_is_refused() -> None:
    """IBKR uses -1 for "no data" on several fields; it is not a volume."""
    snapshot = to_market_data_snapshot(make_ticker(average_daily_volume=-1.0), BROKER_NOW)

    assert snapshot.average_daily_volume is None


@pytest.mark.unit
def test_the_two_volume_fields_are_independent() -> None:
    """Neither is derived from the other, so neither moves when the other does."""
    high_session = to_market_data_snapshot(
        make_ticker(volume=999_999_999.0, average_daily_volume=1_000.0), BROKER_NOW
    )
    low_session = to_market_data_snapshot(
        make_ticker(volume=1.0, average_daily_volume=1_000.0), BROKER_NOW
    )

    assert high_session.average_daily_volume == low_session.average_daily_volume
    assert high_session.volume != low_session.volume


@pytest.mark.unit
def test_translation_is_deterministic() -> None:
    """Requirement I: the same broker payload yields the same canonical output."""
    first = to_market_data_snapshot(
        make_ticker(volume=OBSERVED_CORRUPT_TICK_74, average_daily_volume=OBSERVED_TICK_21),
        BROKER_NOW,
    )
    second = to_market_data_snapshot(
        make_ticker(volume=OBSERVED_CORRUPT_TICK_74, average_daily_volume=OBSERVED_TICK_21),
        BROKER_NOW,
    )

    assert first == second
    assert first.model_dump() == second.model_dump()


@pytest.mark.unit
def test_provenance_survives_the_new_field() -> None:
    """Requirement H: the field arrives with the broker's own provenance intact."""
    snapshot = to_market_data_snapshot(
        make_ticker(market_data_type=3, average_daily_volume=OBSERVED_TICK_21),
        BROKER_NOW,
        source="IBKR",
    )

    assert snapshot.source == "IBKR"
    assert snapshot.origin.value == "BROKER_DELAYED"
    assert snapshot.average_daily_volume == Decimal("52014430")


@pytest.mark.unit
def test_an_unavailable_quote_carries_no_average_volume_claim() -> None:
    """No prices means no snapshot; the average must not smuggle one through."""
    ticker = make_ticker(
        bid=NAN, ask=NAN, last=NAN, close=NAN, average_daily_volume=OBSERVED_TICK_21
    )

    snapshot = to_market_data_snapshot(ticker, BROKER_NOW)

    assert snapshot.origin.value == "UNAVAILABLE"
    assert snapshot.average_daily_volume is None
