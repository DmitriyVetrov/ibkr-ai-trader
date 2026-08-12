"""Account snapshots: the one boundary between broker reality and risk.

Brief sections 13, 22, 30 and 37.6. Three properties, and the third is why this
design exists at all:

* a snapshot is **immutable** and content-addressed, so capturing an unchanged
  account twice is one observation rather than two conflicting balances;
* a missing figure stays ``None`` — "no buying power reported" and "zero buying
  power" are different facts about an account;
* a capture is **read-only**, and the order counter it records comes off the
  broker rather than from a constant, so the zero is evidence.

The whole point of storing this rather than fetching it is Milestone 2's
one-reliable-round-trip-per-connection constraint: a risk calculation that
fetched its own account state could hang the process at exactly the moment it
matters. These tests build snapshots from plain domain models, never from a
broker.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_system.domain.enums import OptionRight, SecurityType, TradingMode
from trading_system.domain.models import BrokerAccount, BrokerPosition
from trading_system.risk.account import build_account_snapshot
from trading_system.risk.models import AccountSnapshot
from trading_system.risk.store import FilesystemAccountSnapshotRepository, RiskStoreError

from .conftest import NOW

pytestmark = pytest.mark.unit


@pytest.fixture
def broker_account() -> BrokerAccount:
    return BrokerAccount(
        account_id="DU0000000",
        currency="EUR",
        as_of=NOW,
        source="SIMULATOR",
        cash=Decimal("100000.00"),
        net_liquidation=Decimal("100000.00"),
        buying_power=Decimal("400000.00"),
        available_funds=Decimal("98000.00"),
    )


@pytest.fixture
def broker_positions() -> list[BrokerPosition]:
    return [
        BrokerPosition(
            account_id="DU0000000",
            symbol="SPY",
            security_type=SecurityType.STOCK,
            as_of=NOW,
            source="SIMULATOR",
            quantity=Decimal("100"),
            average_cost=Decimal("450.00"),
            currency="USD",
        ),
        BrokerPosition(
            account_id="DU0000000",
            symbol="AAPL",
            security_type=SecurityType.OPTION,
            as_of=NOW,
            source="SIMULATOR",
            quantity=Decimal("-2"),
            expiration=NOW.date(),
            strike=Decimal("200.00"),
            right=OptionRight.CALL,
            currency="USD",
        ),
    ]


def _build(account, positions, **overrides: Any) -> AccountSnapshot:
    fields: dict[str, Any] = {
        "broker": "SIMULATOR",
        "trading_mode": TradingMode.PAPER,
        "captured_at": NOW,
        "simulated": True,
    }
    fields.update(overrides)
    return build_account_snapshot(account, positions, **fields)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def test_a_snapshot_carries_what_the_broker_reported(broker_account, broker_positions):
    snapshot = _build(broker_account, broker_positions)

    assert snapshot.currency == "EUR"
    assert snapshot.cash == Decimal("100000.00")
    assert snapshot.available_funds == Decimal("98000.00")
    assert len(snapshot.positions) == 2


def test_an_unreported_figure_stays_none(broker_positions):
    """'No margin data' and 'zero margin' are different facts."""
    account = BrokerAccount(account_id="DU0000000", currency="EUR", as_of=NOW, source="SIMULATOR")

    snapshot = _build(account, broker_positions)

    assert snapshot.cash is None
    assert snapshot.buying_power is None
    assert snapshot.spendable is None, "an unknown balance is not a large one"


def test_spendable_is_the_most_conservative_reported_figure(broker_account, broker_positions):
    snapshot = _build(broker_account, broker_positions)

    assert snapshot.spendable == Decimal("98000.00")


def test_a_capture_records_the_broker_order_counter(broker_account, broker_positions):
    snapshot = _build(broker_account, broker_positions, orders_submitted=0)

    assert snapshot.orders_submitted == 0


def test_a_snapshot_claiming_a_submitted_order_is_refused(broker_account, broker_positions):
    with pytest.raises(ValueError, match="read-only capture"):
        _build(broker_account, broker_positions, orders_submitted=1)


def test_positions_are_ordered_deterministically(broker_account, broker_positions):
    forwards = _build(broker_account, broker_positions)
    backwards = _build(broker_account, list(reversed(broker_positions)))

    assert [p.symbol for p in forwards.positions] == [p.symbol for p in backwards.positions]
    assert forwards.snapshot_id == backwards.snapshot_id


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_the_same_account_state_produces_the_same_id(broker_account, broker_positions):
    first = _build(broker_account, broker_positions)
    second = _build(broker_account, broker_positions, captured_at=NOW + timedelta(hours=1))

    assert first.snapshot_id == second.snapshot_id, "when we looked is a fact about us"


def test_a_changed_balance_produces_a_different_id(broker_account, broker_positions):
    first = _build(broker_account, broker_positions)
    moved = broker_account.model_copy(update={"cash": Decimal("99999.00")})

    assert _build(moved, broker_positions).snapshot_id != first.snapshot_id


def test_a_changed_position_produces_a_different_id(broker_account, broker_positions):
    first = _build(broker_account, broker_positions)

    assert _build(broker_account, broker_positions[:1]).snapshot_id != first.snapshot_id


# ---------------------------------------------------------------------------
# Point in time
# ---------------------------------------------------------------------------
def test_a_snapshot_is_invisible_before_it_was_captured(broker_account, broker_positions):
    """Retrieval binds, not the instant described."""
    snapshot = _build(broker_account, broker_positions, captured_at=NOW + timedelta(hours=2))

    assert not snapshot.known_at(NOW)
    assert snapshot.known_at(NOW + timedelta(hours=2))


def test_a_capture_cannot_predate_the_state_it_describes(broker_account, broker_positions):
    with pytest.raises(ValueError, match="captured earlier"):
        _build(broker_account, broker_positions, captured_at=NOW - timedelta(hours=1))


def test_the_exact_timestamp_boundary_is_visible(broker_account, broker_positions):
    snapshot = _build(broker_account, broker_positions, captured_at=NOW)

    assert snapshot.known_at(NOW), "captured at exactly T was available at T"


def test_age_is_measured_against_the_decision_instant(broker_account, broker_positions):
    snapshot = _build(broker_account, broker_positions)

    assert snapshot.age_seconds(NOW + timedelta(seconds=90)) == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def test_a_snapshot_survives_a_round_trip(tmp_path, broker_account, broker_positions):
    repository = FilesystemAccountSnapshotRepository(tmp_path / "accounts")
    snapshot = _build(broker_account, broker_positions)

    repository.save(snapshot)
    loaded = repository.get(snapshot.snapshot_id)

    assert loaded is not None
    assert loaded.model_dump() == snapshot.model_dump()


def test_storing_the_same_snapshot_twice_is_idempotent(tmp_path, broker_account, broker_positions):
    repository = FilesystemAccountSnapshotRepository(tmp_path / "accounts")
    snapshot = _build(broker_account, broker_positions)

    repository.save(snapshot)
    repository.save(snapshot)

    assert len(repository.history()) == 1


def test_a_stored_snapshot_is_immutable(tmp_path, broker_account, broker_positions):
    repository = FilesystemAccountSnapshotRepository(tmp_path / "accounts")
    snapshot = _build(broker_account, broker_positions)
    repository.save(snapshot)

    forged = snapshot.model_copy(update={"cash": Decimal("999999.00")})

    with pytest.raises(RiskStoreError, match="immutable"):
        repository.save(forged)


def test_latest_as_of_never_returns_a_future_capture(tmp_path, broker_account, broker_positions):
    """A historical replay cannot see a balance nobody had yet observed."""
    repository = FilesystemAccountSnapshotRepository(tmp_path / "accounts")
    past = _build(broker_account, broker_positions)
    later_account = broker_account.model_copy(
        update={"as_of": NOW + timedelta(hours=2), "cash": Decimal("1.00")}
    )
    future = _build(later_account, broker_positions, captured_at=NOW + timedelta(hours=2))
    repository.save(past)
    repository.save(future)

    visible = repository.latest_as_of(NOW)

    assert visible is not None
    assert visible.snapshot_id == past.snapshot_id


def test_an_empty_store_answers_none(tmp_path):
    repository = FilesystemAccountSnapshotRepository(tmp_path / "accounts")

    assert repository.latest() is None
    assert repository.latest_as_of(NOW) is None
    assert repository.history() == []
