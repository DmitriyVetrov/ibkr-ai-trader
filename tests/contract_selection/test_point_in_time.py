"""Point-in-time contract selection (brief sections 23, 39).

One distinction does all the work here, and getting it backwards ruins every
historical evaluation the system will ever run:

* a **quote** retrieved after ``as_of`` was not information we had at ``as_of``,
  and must be invisible;
* an **expiration** after ``as_of`` is the entire point of an option, and is
  not look-ahead at all.

Retrieval binds, not publication. A chain downloaded this morning did not
inform a decision made last week, however accurately it describes last week's
market.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import ContractSelectionStatus, StrategyType

from .conftest import FURTHER, NEAR_TARGET, REFERENCE

pytestmark = pytest.mark.unit


def test_a_quote_retrieved_after_the_instant_is_invisible(
    store_underlying_quote, store_chain, store_option_quotes, select, selection_now
) -> None:
    later = selection_now + timedelta(hours=2)
    store_underlying_quote()
    store_chain()
    store_option_quotes(as_of=later, retrieved_at=later)

    result = select(as_of=selection_now)

    assert result.selection_status is ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
    assert not result.legs


def test_a_chain_retrieved_after_the_instant_is_invisible(
    store_underlying_quote, store_chain, store_option_quotes, select, selection_now
) -> None:
    later = selection_now + timedelta(days=1)
    store_underlying_quote()
    store_chain(as_of=later, retrieved_at=later)
    store_option_quotes(as_of=later, retrieved_at=later)

    result = select(as_of=selection_now)

    assert result.selection_status is ContractSelectionStatus.OPTION_CHAIN_UNAVAILABLE


def test_an_expiration_after_the_instant_is_perfectly_normal(priced_chain, select) -> None:
    """Every option expires in the future. That is not look-ahead."""
    result = (priced_chain(), select())[1]

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.expiration is not None
    assert result.expiration > result.as_of.date()
    assert result.dte is not None and result.dte > 0


def test_a_replay_of_an_earlier_instant_sees_the_earlier_chain(
    store_underlying_quote, store_chain, store_option_quotes, select, selection_now
) -> None:
    """The whole point: what was decidable then, not what is knowable now."""
    earlier = selection_now - timedelta(days=2)
    store_underlying_quote(as_of=earlier, retrieved_at=earlier)
    store_chain(as_of=earlier, retrieved_at=earlier, expirations=[NEAR_TARGET, FURTHER])
    store_option_quotes(as_of=earlier, retrieved_at=earlier)
    # Data collected since, describing a different market.
    store_underlying_quote(last=Decimal("250.00"))
    store_chain(strikes=[Decimal("240"), Decimal("250"), Decimal("260")])
    store_option_quotes(
        strikes=[Decimal("240"), Decimal("250"), Decimal("260")], reference=Decimal("250.00")
    )

    result = select(as_of=earlier)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.reference_price == REFERENCE
    assert result.legs[0].strike in {
        Decimal(str(value)) for value in (165, 170, 175, 180, 185, 190, 195)
    }


def test_a_later_instant_sees_the_later_chain(
    store_underlying_quote, store_chain, store_option_quotes, select, selection_now
) -> None:
    """The newer collection wins — when it actually carries newer content.

    The later quotes differ in a real field, not only in their clocks. The data
    layer records an unchanged payload as a re-observation rather than a second
    snapshot, so "collected again" and "changed" are deliberately different
    events, and a test that ignored the distinction would be testing a
    behaviour the system does not have.
    """
    earlier = selection_now - timedelta(days=2)
    store_underlying_quote(as_of=earlier, retrieved_at=earlier, last=Decimal("175.00"))
    store_chain(as_of=earlier, retrieved_at=earlier)
    store_option_quotes(as_of=earlier, retrieved_at=earlier, reference=Decimal("175.00"))
    store_underlying_quote()
    store_chain()
    store_option_quotes()

    result = select(as_of=selection_now)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.reference_price == REFERENCE
    assert result.legs[0].quote_as_of == selection_now


def test_a_look_ahead_leak_raises_rather_than_being_filtered(
    store_underlying_quote, store_chain, data_repo, select, selection_now
) -> None:
    """A leak is a storage bug. A quietly shorter candidate list would look
    exactly like an ordinary thin chain, so the selector refuses instead."""
    from trading_system.data.models import DataQualityReport
    from trading_system.data.repository import build_snapshot
    from trading_system.domain.enums import DataType, MarketDataOrigin, SourceTier

    from .conftest import _build_quotes

    store_underlying_quote()
    store_chain()
    future = selection_now + timedelta(days=1)
    # A snapshot whose own timestamps say it was knowable, carrying records
    # that were not: exactly the shape a storage bug produces.
    leaking = build_snapshot(
        data_type=DataType.OPTION_QUOTE,
        key="NVDA",
        records=_build_quotes(as_of=future, retrieved_at=future),
        provider="IBKR",
        source_tier=SourceTier.TIER_1,
        origin=MarketDataOrigin.BROKER_DELAYED,
        as_of=selection_now,
        retrieved_at=selection_now,
        quality=DataQualityReport(evaluated_at=selection_now),
    )
    data_repo.save_snapshot(leaking)

    result = select(as_of=selection_now)

    assert result.selection_status is ContractSelectionStatus.POINT_IN_TIME_ERROR
    assert "correctness bug in storage" in (result.status_detail or "")
    assert not result.legs


def test_the_dte_is_counted_from_the_exchange_local_date(
    store_underlying_quote, store_chain, store_option_quotes, select, selection_now
) -> None:
    """Counting from a UTC date is wrong by one for most of the evening."""
    # 01:00 UTC on 11 August is still 21:00 on 10 August in New York.
    evening = selection_now.replace(day=11, hour=1, minute=0)
    store_underlying_quote(as_of=evening, retrieved_at=evening)
    store_chain(as_of=evening, retrieved_at=evening)
    store_option_quotes(as_of=evening, retrieved_at=evening)

    result = select(as_of=evening, strategy=StrategyType.LONG_CALL)

    assert result.selection_status is ContractSelectionStatus.SUCCESS
    assert result.expiration == NEAR_TARGET
    assert result.dte == 18, "18 days from 10 August in New York, not 17 from 11 August in UTC"
