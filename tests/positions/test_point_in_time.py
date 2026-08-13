"""Point-in-time discipline for broker observations (brief section 13).

Retrieval binds. A snapshot observed after an instant was not available at that
instant, however recent the holdings it describes — so a historical
reconstruction can never claim we knew a position before we looked at it.

The distinction this file also pins: ``broker_timestamp`` and ``observed_at``
are kept apart. What the broker says about *when* is not what we say about
*when we learned it*, and collapsing them loses the only means of telling a
slow feed from a slow fill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.positions.factories import ACCOUNT, MASKED, NOW, option_position
from trading_system.domain.enums import TradingMode
from trading_system.positions.snapshot import build_position_snapshot, to_observed_position

pytestmark = pytest.mark.unit

EARLIER = NOW - timedelta(hours=2)
LATER = NOW + timedelta(hours=2)


def _snapshot(*, as_of: datetime, observed_at: datetime, quantity: Decimal = Decimal("2")):
    return build_position_snapshot(
        [option_position(quantity=quantity, as_of=as_of)],
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=as_of,
        observed_at=observed_at,
    )


def test_a_snapshot_observed_later_was_not_known_earlier() -> None:
    snapshot = _snapshot(as_of=LATER, observed_at=LATER)
    assert snapshot.known_at(NOW) is False
    assert snapshot.known_at(LATER) is True


def test_a_snapshot_describing_a_past_instant_is_still_bound_by_when_we_looked() -> None:
    """The state is old; our sight of it is not. Retrieval is what binds."""
    snapshot = _snapshot(as_of=EARLIER, observed_at=LATER)
    assert snapshot.known_at(NOW) is False


def test_age_is_measured_against_the_instant_being_reconciled() -> None:
    snapshot = _snapshot(as_of=NOW, observed_at=NOW)
    assert snapshot.age_seconds(NOW) == 0
    assert snapshot.age_seconds(NOW + timedelta(seconds=300)) == 300


def test_the_broker_timestamp_and_our_observation_clock_stay_separate() -> None:
    position = to_observed_position(
        option_position(as_of=EARLIER), observed_at=NOW, account_reference=MASKED
    )
    assert position.broker_timestamp == EARLIER
    assert position.observed_at == NOW
    assert position.broker_timestamp != position.observed_at


def test_reconstructing_at_an_instant_returns_what_was_known_then(position_repository) -> None:
    old = _snapshot(as_of=EARLIER, observed_at=EARLIER, quantity=Decimal("1"))
    new = _snapshot(as_of=LATER, observed_at=LATER, quantity=Decimal("5"))
    position_repository.save_snapshot(old)
    position_repository.save_snapshot(new)

    reconstructed = position_repository.reconstruct(NOW)

    assert reconstructed is not None
    assert reconstructed.snapshot_id == old.snapshot_id


def test_reconstructing_before_any_observation_returns_nothing(position_repository) -> None:
    position_repository.save_snapshot(_snapshot(as_of=LATER, observed_at=LATER))
    assert position_repository.reconstruct(NOW) is None


def test_a_capture_records_both_clocks(service, make_service, clock) -> None:
    capture = service.capture()
    assert capture.snapshot.as_of == NOW
    assert capture.snapshot.observed_at == NOW


def test_every_timestamp_is_timezone_aware_and_utc() -> None:
    snapshot = _snapshot(as_of=NOW, observed_at=NOW)
    assert snapshot.as_of.tzinfo is not None
    assert snapshot.observed_at.tzinfo is not None
    assert snapshot.as_of.astimezone(UTC) == snapshot.as_of
