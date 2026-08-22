"""Building an account snapshot from what the broker reported.

This is the *only* place broker state becomes risk input, and it is a pure
function of two domain models:

.. code-block:: text

    broker reality  ->  BrokerAccount + BrokerPosition  ->  AccountSnapshot
       (broker/)              (domain/models.py)              (risk/)

Note what is absent. This module imports no broker, opens no connection and
knows nothing about IBKR. The caller — a CLI command, which is already the
layer allowed to hold a broker — performs the read and hands the results here.
Two consequences follow, and both are load-bearing:

* ``risk/`` and ``allocation/`` contain no path to a broker at all, so "the
  risk engine never calls IBKR" is a structural fact rather than a convention.
  A test parses the import graph and asserts it.
* There is exactly **one** broker retrieval per capture, and it uses only data
  ``ib_async``'s startup handshake already caches (account summary, positions).
  Milestone 2 established that a second uncached round trip on one connection
  can go unanswered indefinitely; a design that fetched account state from
  inside a risk calculation would inherit that hazard at the worst moment.

The identity of a snapshot is derived from its *content*, so capturing an
unchanged account twice is recognisable as one observation rather than recorded
as two different balances — the same reasoning that governs data-layer snapshot
ids.
"""

from __future__ import annotations

from datetime import datetime

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import FxRateOrigin, TradingMode
from trading_system.domain.models import BrokerAccount, BrokerPosition
from trading_system.fx.models import FxRate, FxRateTable
from trading_system.risk.models import (
    AccountPosition,
    AccountSnapshot,
    account_snapshot_identifier,
    build_account_snapshot_payload,
)

__all__ = ["build_account_snapshot", "fx_rate_table"]


def fx_rate_table(account: BrokerAccount) -> FxRateTable:
    """The broker's own exchange rates, as point-in-time observations.

    IBKR quotes each currency *into* the account's base currency, so a row
    reading ``USD -> 0.855`` on a EUR account means one dollar buys 0.855 euro.
    That direction is recorded literally rather than inverted here; the
    conversion layer inverts on demand and marks the result as derived, so a
    stored artifact always says which direction the broker actually quoted.

    Rates take their instant from the account read that produced them. That is
    the whole point of building them here rather than fetching them separately:
    a balance and the rate that converts it are one observation, and there is
    no path by which the two could come from different moments.
    """
    return FxRateTable(
        rates=tuple(
            FxRate(
                base_currency=code,
                quote_currency=account.currency,
                rate=rate,
                as_of=account.as_of,
                origin=(
                    FxRateOrigin.SIMULATED
                    if account.source == "SIMULATOR"
                    else FxRateOrigin.BROKER_ACCOUNT_LEDGER
                ),
                source=account.source,
            )
            for code, rate in sorted(account.exchange_rates.items())
            if code.upper() != account.currency.upper()
        )
    )


def build_account_snapshot(
    account: BrokerAccount,
    positions: list[BrokerPosition],
    *,
    broker: str,
    trading_mode: TradingMode,
    captured_at: datetime,
    orders_submitted: int = 0,
    read_only: bool = True,
    simulated: bool = False,
) -> AccountSnapshot:
    """Reduce broker state to what deterministic risk needs, and stamp it.

    ``orders_submitted`` is taken from the broker's own counter and stored on
    the snapshot. It is structurally zero — capturing an account reads and does
    nothing else — and recording the counter rather than the constant is what
    makes the claim evidence instead of an assertion. A non-zero value is
    refused by the model.

    Every monetary field the broker omitted stays ``None``. Nothing is filled
    in with a zero: an unreported balance is unknown, and an engine that read
    "unknown" as "zero" would refuse every trade for a reason that has nothing
    to do with the market.
    """
    as_of = account.as_of
    snapshot_id = account_snapshot_identifier(
        broker=broker,
        account_id=account.account_id,
        as_of=as_of,
        payload_digest=stable_hash(build_account_snapshot_payload(account, positions)),
    )
    return AccountSnapshot(
        snapshot_id=snapshot_id,
        as_of=as_of,
        captured_at=captured_at,
        broker=broker,
        account_id=account.account_id,
        currency=account.currency,
        trading_mode=trading_mode,
        cash_by_currency=dict(account.cash_by_currency),
        fx_rates=fx_rate_table(account),
        cash=account.cash,
        net_liquidation=account.net_liquidation,
        buying_power=account.buying_power,
        available_funds=account.available_funds,
        excess_liquidity=account.excess_liquidity,
        unrealized_pnl=account.unrealized_pnl,
        realized_pnl=account.realized_pnl,
        positions=[
            AccountPosition.of(position)
            for position in sorted(
                positions, key=lambda p: (p.symbol, p.contract_id or 0, str(p.quantity))
            )
        ],
        read_only=read_only,
        orders_submitted=orders_submitted,
        simulated=simulated,
    )
