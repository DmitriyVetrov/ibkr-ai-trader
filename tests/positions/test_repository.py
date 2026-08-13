"""The position and fill stores (brief sections 39-40).

The claims under test: immutability, an append-only index, re-observation
instead of duplication, and the query surface the CLI and reconciliation
actually use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.positions.factories import (
    ACCOUNT,
    CALL_KEY,
    MASKED,
    NOW,
    broker_execution,
    option_position,
    stock_position,
)
from trading_system.domain.enums import BrokerReadStatus, TradingMode
from trading_system.domain.models import BrokerPosition
from trading_system.positions.fills import ContractTerms, to_observed_fill
from trading_system.positions.snapshot import build_position_snapshot, unavailable_snapshot
from trading_system.positions.store import PositionStoreError

pytestmark = pytest.mark.unit

LATER = datetime(2026, 8, 11, 14, 30, tzinfo=UTC)


def _snapshot(positions: list[BrokerPosition], *, as_of: datetime = NOW):
    return build_position_snapshot(
        positions,
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=as_of,
        observed_at=as_of,
    )


def _fill(*, execution_id: str = "exec-1", observed_at: datetime = NOW):
    return to_observed_fill(
        broker_execution(execution_id=execution_id),
        observed_at=observed_at,
        account_reference=MASKED,
        terms=ContractTerms(multiplier=100),
        execution_id="execution-1",
    )


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
def test_a_snapshot_round_trips(position_repository) -> None:
    snapshot = _snapshot([option_position()])
    position_repository.save_snapshot(snapshot)
    assert position_repository.get_snapshot(snapshot.snapshot_id) == snapshot


def test_the_latest_snapshot_is_the_newest_observation(position_repository) -> None:
    first = _snapshot([option_position()])
    second = _snapshot([option_position(quantity=Decimal("3"))], as_of=LATER)
    position_repository.save_snapshot(first)
    position_repository.save_snapshot(second)
    latest = position_repository.latest()
    assert latest is not None
    assert latest.snapshot_id == second.snapshot_id


def test_storing_identical_content_twice_is_a_re_observation(position_repository) -> None:
    snapshot = _snapshot([option_position()])
    position_repository.save_snapshot(snapshot)
    position_repository.save_snapshot(snapshot)
    assert len(position_repository.history()) == 2
    assert sum(entry.reobserved for entry in position_repository.history()) == 1


def test_a_stored_snapshot_is_immutable(position_repository) -> None:
    snapshot = _snapshot([option_position()])
    position_repository.save_snapshot(snapshot)
    tampered = snapshot.model_copy(update={"content_hash": "something-else"})
    with pytest.raises(PositionStoreError, match="immutable"):
        position_repository.save_snapshot(tampered)


def test_latest_usable_skips_a_failed_read(position_repository) -> None:
    """A consumer that reconciled against a failed read would compare with nothing."""
    good = _snapshot([option_position()])
    position_repository.save_snapshot(good)
    position_repository.save_snapshot(
        unavailable_snapshot(
            broker="SIMULATOR",
            account_id=ACCOUNT,
            trading_mode=TradingMode.PAPER,
            as_of=LATER,
            observed_at=LATER,
            status=BrokerReadStatus.UNAVAILABLE,
            detail="gateway down",
        )
    )
    latest = position_repository.latest()
    usable = position_repository.latest_usable()
    assert latest is not None and latest.usable is False
    assert usable is not None and usable.snapshot_id == good.snapshot_id


def test_by_contract_returns_one_instruments_history_newest_first(position_repository) -> None:
    position_repository.save_snapshot(_snapshot([option_position()]))
    position_repository.save_snapshot(
        _snapshot([option_position(quantity=Decimal("3"))], as_of=LATER)
    )
    observations = position_repository.by_contract(CALL_KEY)
    assert [observation.quantity for observation in observations] == [
        Decimal("3"),
        Decimal("2"),
    ]


def test_by_underlying_finds_every_contract_on_one_symbol(position_repository) -> None:
    position_repository.save_snapshot(_snapshot([option_position(), stock_position()]))
    assert len(position_repository.by_underlying("NVDA")) == 1
    assert len(position_repository.by_underlying("SPY")) == 1


def test_an_unknown_snapshot_id_is_none_rather_than_an_error(position_repository) -> None:
    assert position_repository.get_snapshot("positions-nope") is None
    assert position_repository.latest() is None


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------
def test_a_fill_round_trips(fill_repository) -> None:
    fill = _fill()
    _, is_new = fill_repository.save(fill)
    assert is_new is True
    assert fill_repository.get(fill.fill_id) == fill


def test_saving_the_same_fill_twice_records_one_fill(fill_repository) -> None:
    fill = _fill()
    fill_repository.save(fill)
    _, is_new = fill_repository.save(fill)
    assert is_new is False
    assert len(fill_repository.all()) == 1


def test_re_observing_a_fill_at_a_later_instant_is_still_one_fill(fill_repository) -> None:
    """Only the trade matters for identity; when we saw it does not."""
    fill_repository.save(_fill())
    stored, known = fill_repository.save_many([_fill(observed_at=LATER)])
    assert stored == []
    assert len(known) == 1


def test_a_changed_economic_field_refuses_to_overwrite_a_recorded_fill(fill_repository) -> None:
    fill = _fill()
    fill_repository.save(fill)
    with pytest.raises(PositionStoreError, match="immutable"):
        fill_repository.save(fill.model_copy(update={"price": Decimal("9.99")}))


def test_fills_can_be_found_by_execution_and_by_contract(fill_repository) -> None:
    fill_repository.save(_fill())
    assert len(fill_repository.for_execution("execution-1")) == 1
    assert len(fill_repository.for_contract(CALL_KEY)) == 1
    assert fill_repository.for_execution("execution-none") == []


def test_known_ids_is_what_makes_a_repeat_recognisable(fill_repository) -> None:
    fill = _fill()
    fill_repository.save(fill)
    assert fill.fill_id in fill_repository.known_ids()


def test_fills_are_returned_oldest_first(fill_repository) -> None:
    first = _fill(execution_id="exec-1")
    second = to_observed_fill(
        broker_execution(execution_id="exec-2", executed_at=LATER),
        observed_at=LATER,
        account_reference=MASKED,
        terms=ContractTerms(multiplier=100),
        execution_id="execution-1",
    )
    fill_repository.save(second)
    fill_repository.save(first)
    assert [fill.fill_id for fill in fill_repository.all()] == [first.fill_id, second.fill_id]
