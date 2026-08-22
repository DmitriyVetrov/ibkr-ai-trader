"""Where a rate enters the system: the account capture, and nowhere else.

The engines hold no broker and could not fetch a rate if they wanted to, so a
conversion is only ever as good as what one capture stored. Two properties
follow, and this suite asserts both:

* **The rate and the balance it converts are one observation.** They come from
  one broker read at one instant, so there is no path by which a balance could
  be converted at a rate from a different moment.
* **A missing rate is recorded as missing.** IBKR reports no rate for a
  currency it does not know; nothing here fills that in, and the account simply
  cannot be expressed in that currency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from trading_system.broker.ibkr.client import ACCOUNT_TAGS, LEDGER_TAGS
from trading_system.domain.enums import FxRateOrigin, FxStatus, TradingMode
from trading_system.domain.models import BrokerAccount
from trading_system.fx.models import FxRateTable
from trading_system.risk.account import build_account_snapshot, fx_rate_table

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 22, 14, 30, tzinfo=UTC)


def account(**overrides: Any) -> BrokerAccount:
    fields: dict[str, Any] = {
        "account_id": "DU1234567",
        "currency": "EUR",
        "as_of": NOW,
        "source": "IBKR",
        "cash": Decimal("5000.00"),
        "available_funds": Decimal("5000.00"),
        "cash_by_currency": {"EUR": Decimal("5000.00"), "USD": Decimal("0.00")},
        "exchange_rates": {"USD": Decimal("0.855")},
    }
    fields.update(overrides)
    return BrokerAccount(**fields)


# ---------------------------------------------------------------------------
# The account the brief describes
# ---------------------------------------------------------------------------
def test_a_eur_account_holding_no_dollars_can_still_price_a_usd_campaign() -> None:
    """Case 4, at the boundary: EUR 5,000, USD 0, and a rate.

    The account's base currency stays EUR, its cash stays EUR, and none of it
    is converted anywhere. What the rate does is let a EUR balance be
    *compared* with a USD price — which is a different thing from acquiring
    dollars, and the system deliberately does neither on its own.
    """
    snapshot = build_account_snapshot(
        account(),
        [],
        broker="IBKR",
        trading_mode=TradingMode.PAPER,
        captured_at=NOW,
    )

    assert snapshot.currency == "EUR"
    assert snapshot.cash_by_currency == {"EUR": Decimal("5000.00"), "USD": Decimal("0.00")}

    conversion = snapshot.spendable_in("USD", as_of=NOW, max_rate_age_seconds=86400)
    assert conversion is not None and conversion.ok
    assert conversion.converted_amount == Decimal("5847.95"), "5000 / 0.855"


def test_the_cash_ledger_is_never_summed_across_currencies() -> None:
    """EUR 5,000 and USD 0 are two facts, and a total would hide a rate."""
    snapshot = build_account_snapshot(
        account(cash_by_currency={"EUR": Decimal("5000.00"), "USD": Decimal("250.00")}),
        [],
        broker="IBKR",
        trading_mode=TradingMode.PAPER,
        captured_at=NOW,
    )

    assert set(snapshot.cash_by_currency) == {"EUR", "USD"}
    assert snapshot.cash_by_currency["USD"] == Decimal("250.00")


def test_the_rate_takes_its_instant_from_the_read_that_produced_it() -> None:
    """A balance can never be converted at a rate from another moment.

    Not because a caller remembers to pass matching timestamps, but because
    there is no other instant's rate on the artifact to reach for.
    """
    rates = fx_rate_table(account())

    assert [r.as_of for r in rates.rates] == [NOW]


def test_the_broker_quotes_into_the_base_and_the_record_says_so() -> None:
    """IBKR reports ``USD -> 0.855`` on a EUR account: one dollar buys 0.855 euro.

    Stored in that direction rather than pre-inverted, so a stored artifact
    always states the direction the broker actually quoted. Inversion happens
    on demand and is marked as derived.
    """
    [usd] = fx_rate_table(account()).rates

    assert (usd.base_currency, usd.quote_currency) == ("USD", "EUR")
    assert usd.rate == Decimal("0.855")
    assert usd.origin is FxRateOrigin.BROKER_ACCOUNT_LEDGER


def test_no_reported_rate_means_no_conversion_at_all() -> None:
    """Case 2 and Case 6 at the boundary: absent, not one."""
    snapshot = build_account_snapshot(
        account(exchange_rates={}),
        [],
        broker="IBKR",
        trading_mode=TradingMode.PAPER,
        captured_at=NOW,
    )

    conversion = snapshot.spendable_in("USD", as_of=NOW, max_rate_age_seconds=86400)
    assert conversion is not None
    assert conversion.status is FxStatus.UNAVAILABLE
    assert conversion.converted_amount is None


def test_no_balance_at_all_is_distinct_from_no_rate() -> None:
    """ "We hold nothing", "we could not convert it" and "we could not look".

    An absent balance returns ``None`` rather than a failed conversion, because
    a missing cash figure is an account-snapshot problem and pointing an
    operator at exchange rates for it would waste their afternoon.
    """
    snapshot = build_account_snapshot(
        account(cash=None, available_funds=None),
        [],
        broker="IBKR",
        trading_mode=TradingMode.PAPER,
        captured_at=NOW,
    )

    assert snapshot.spendable is None
    assert snapshot.spendable_in("USD", as_of=NOW, max_rate_age_seconds=86400) is None


def test_a_rate_into_the_base_currency_itself_is_refused() -> None:
    """The identity belongs in code, not in an editable field."""
    with pytest.raises(ValidationError, match="base currency"):
        account(exchange_rates={"EUR": Decimal("1.0")})


def test_a_non_positive_rate_is_refused_rather_than_stored() -> None:
    """A broker that reported nothing must be recorded as having reported nothing."""
    with pytest.raises(ValidationError, match="not positive"):
        account(exchange_rates={"USD": Decimal("0")})


# ---------------------------------------------------------------------------
# Two snapshots at the same balance and different rates are different facts
# ---------------------------------------------------------------------------
def test_the_rate_is_part_of_the_snapshot_identity() -> None:
    """Otherwise the immutable store would collide two different capitals.

    Identical balances converted at different rates are different amounts of
    spendable money. An id derived from the balance alone would make the second
    capture look like a re-observation of the first — which is the mistake this
    repository has now learned four times, in four different milestones.
    """
    first = build_account_snapshot(
        account(),
        [],
        broker="IBKR",
        trading_mode=TradingMode.PAPER,
        captured_at=NOW,
    )
    second = build_account_snapshot(
        account(exchange_rates={"USD": Decimal("0.92")}),
        [],
        broker="IBKR",
        trading_mode=TradingMode.PAPER,
        captured_at=NOW,
    )

    assert first.snapshot_id != second.snapshot_id


def test_an_unchanged_account_still_records_one_observation() -> None:
    """Content-addressing is not weakened by adding the rate to it."""
    kwargs: dict[str, Any] = {
        "broker": "IBKR",
        "trading_mode": TradingMode.PAPER,
        "captured_at": NOW,
    }
    first = build_account_snapshot(account(), [], **kwargs)
    second = build_account_snapshot(account(), [], **kwargs)

    assert first.snapshot_id == second.snapshot_id


# ---------------------------------------------------------------------------
# The ledger rows the rate arrives with
# ---------------------------------------------------------------------------
def test_every_per_currency_tag_is_known_to_be_one() -> None:
    """``$LEDGER:ALL`` makes some account-summary tags arrive once per currency.

    A loop keyed on the tag alone overwrites them in arrival order and leaves
    whichever currency came last standing in for the account — which is what
    ``BrokerAccount.cash`` used to be. The fix depends on knowing which tags
    are per-currency, so the overlap is asserted rather than assumed.
    """
    overlapping = set(ACCOUNT_TAGS) & LEDGER_TAGS

    assert overlapping == {"TotalCashValue", "UnrealizedPnL", "RealizedPnL"}


def test_an_empty_rate_table_is_a_perfectly_ordinary_value() -> None:
    """Constructible, and it converts nothing. No special-casing anywhere."""
    assert FxRateTable().find("EUR", "USD") is None
