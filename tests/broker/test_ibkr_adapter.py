"""The IBKR adapter's construction guards and state reads.

No test here opens a socket: the adapter is driven with a fake ``IB`` object,
so the translation and error handling are exercised without a gateway.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from trading_system.broker.base import (
    BrokerAuthenticationError,
    BrokerConfigurationError,
    BrokerConnectionError,
    BrokerResponseError,
)
from trading_system.broker.ibkr import IBKRBroker
from trading_system.broker.ibkr.client import ACCOUNT_TAGS
from trading_system.domain.enums import SecurityType, TradingMode
from trading_system.infrastructure.clock import FixedClock

from .conftest import make_fill, make_portfolio_item, make_trade


def make_broker(clock: FixedClock, **overrides: Any) -> IBKRBroker:
    kwargs: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 4002,
        "client_id": 1,
        "clock": clock,
    }
    kwargs.update(overrides)
    return IBKRBroker(**kwargs)


class FakeIB:
    """Minimal stand-in for ``ib_async.IB``."""

    def __init__(
        self,
        *,
        accounts: list[str] | None = None,
        summary: list[Any] | None = None,
        portfolio: list[Any] | None = None,
        positions: list[Any] | None = None,
        trades: list[Any] | None = None,
        fills: list[Any] | None = None,
        ticker: Any | None = None,
    ) -> None:
        self._accounts = accounts if accounts is not None else ["DU1234567"]
        self._summary = summary if summary is not None else default_summary()
        self._portfolio = portfolio if portfolio is not None else []
        self._positions = positions if positions is not None else []
        self._trades = trades if trades is not None else []
        self._fills = fills if fills is not None else []
        self.connected = True
        self.market_data_type: int | None = None
        self._ticker = ticker
        self.generic_tick_list: str | None = None
        self.market_data_requests: list[Any] = []
        self.cancelled_market_data: list[Any] = []

    def isConnected(self) -> bool:  # noqa: N802 - mirrors the ib_async API
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def managedAccounts(self) -> list[str]:  # noqa: N802
        return self._accounts

    def reqMarketDataType(self, market_data_type: int) -> None:  # noqa: N802
        self.market_data_type = market_data_type

    # --- streaming market data ---------------------------------------------
    # `reqTickers` cannot carry a generic tick list, so the adapter opens a
    # short streaming subscription instead: that is the only way to be sent
    # IBKR tick 21 (`avVolume`). The fake records what was asked for so a test
    # can assert generic tick 165 was actually requested, and that the
    # subscription was cancelled rather than left open.
    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803 - ib_async's own keyword name
        **kwargs: Any,
    ) -> Any:
        self.generic_tick_list = genericTickList
        self.market_data_requests.append(contract)
        return self._ticker

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled_market_data.append(contract)

    def waitOnUpdate(self, timeout: float = 0) -> None:  # noqa: N802
        """Ticks are already present on the fake, so a wait is a no-op."""
        return None

    def reqCurrentTime(self) -> None:  # noqa: N802
        return None

    def accountSummary(self, account: str = "") -> list[Any]:  # noqa: N802
        return self._summary

    def portfolio(self, account: str = "") -> list[Any]:
        return self._portfolio

    def positions(self, account: str = "") -> list[Any]:
        return self._positions

    def openTrades(self) -> list[Any]:  # noqa: N802
        return self._trades

    def fills(self) -> list[Any]:
        return self._fills

    client = SimpleNamespace(serverVersion=lambda: 176)


def account_value(tag: str, value: str, currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(
        account="DU1234567", tag=tag, value=value, currency=currency, modelCode=""
    )


def default_summary() -> list[SimpleNamespace]:
    return [
        account_value("NetLiquidation", "100000.25", "EUR"),
        account_value("TotalCashValue", "50000.10", "EUR"),
        account_value("BuyingPower", "400000.00", "EUR"),
        account_value("AvailableFunds", "98000.00", "EUR"),
        account_value("ExcessLiquidity", "96500.00", "EUR"),
        account_value("FullInitMarginReq", "2000.00", "EUR"),
        account_value("FullMaintMarginReq", "1500.00", "EUR"),
    ]


def connect_fake(broker: IBKRBroker, ib: FakeIB, account: str = "DU1234567") -> IBKRBroker:
    """Attach a fake IB without opening a socket."""
    broker._ib = ib
    broker._account_id = account
    return broker


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_live_mode_is_refused(broker_clock: FixedClock) -> None:
    """Milestone 2 supports DRY_RUN and PAPER only."""
    with pytest.raises(BrokerConfigurationError, match="LIVE"):
        make_broker(broker_clock, trading_mode=TradingMode.LIVE)


@pytest.mark.unit
def test_a_writable_connection_requires_paper(broker_clock: FixedClock) -> None:
    """Milestone 8 permits a writable connection, in PAPER and nowhere else.

    DRY_RUN in particular must never reach a real gateway — that is what the
    simulator is for — so asking for a writable one is a configuration error
    rather than a quiet downgrade.
    """
    with pytest.raises(BrokerConfigurationError, match="TRADING_MODE=PAPER"):
        make_broker(broker_clock, trading_mode=TradingMode.DRY_RUN, read_only=False)


@pytest.mark.unit
def test_a_writable_paper_connection_is_permitted(broker_clock: FixedClock) -> None:
    """Constructed, not connected: this opens no socket.

    Obtaining one of these still requires ``build_execution_broker`` plus an
    explicit ``IBKR_READ_ONLY=false``; the constructor is simply no longer the
    thing that refuses.
    """
    broker = make_broker(broker_clock, trading_mode=TradingMode.PAPER, read_only=False)

    assert broker.read_only is False
    assert broker.orders_submitted == 0


@pytest.mark.unit
def test_a_writable_live_connection_is_still_refused(broker_clock: FixedClock) -> None:
    with pytest.raises(BrokerConfigurationError, match="LIVE"):
        make_broker(broker_clock, trading_mode=TradingMode.LIVE, read_only=False)


@pytest.mark.unit
@pytest.mark.parametrize("port", [0, -1, 70000])
def test_invalid_port_is_refused(broker_clock: FixedClock, port: int) -> None:
    with pytest.raises(BrokerConfigurationError, match="PORT"):
        make_broker(broker_clock, port=port)


@pytest.mark.unit
@pytest.mark.parametrize("host", ["", "   "])
def test_missing_host_is_refused(broker_clock: FixedClock, host: str) -> None:
    with pytest.raises(BrokerConfigurationError, match="HOST"):
        make_broker(broker_clock, host=host)


@pytest.mark.unit
def test_negative_client_id_is_refused(broker_clock: FixedClock) -> None:
    with pytest.raises(BrokerConfigurationError, match="CLIENT_ID"):
        make_broker(broker_clock, client_id=-1)


@pytest.mark.unit
def test_ports_are_not_hardcoded(broker_clock: FixedClock) -> None:
    """TWS and IB Gateway use different ports; both must be configurable."""
    for port in (4001, 4002, 7496, 7497):
        assert make_broker(broker_clock, port=port).port == port


@pytest.mark.unit
def test_broker_is_read_only_and_reports_zero_orders(broker_clock: FixedClock) -> None:
    broker = make_broker(broker_clock)
    assert broker.read_only is True
    assert broker.orders_submitted == 0
    assert broker.name == "IBKR"


# ---------------------------------------------------------------------------
# Account identity
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_account_resolution_requires_a_managed_account(broker_clock: FixedClock) -> None:
    broker = make_broker(broker_clock)
    with pytest.raises(BrokerAuthenticationError, match="no managed accounts"):
        broker._resolve_account(FakeIB(accounts=[]))


@pytest.mark.unit
def test_ambiguous_account_is_refused(broker_clock: FixedClock) -> None:
    """Multiple accounts and no explicit choice must not be resolved by guessing."""
    broker = make_broker(broker_clock)
    with pytest.raises(BrokerConfigurationError, match="IBKR_ACCOUNT"):
        broker._resolve_account(FakeIB(accounts=["DU1", "DU2"]))


@pytest.mark.unit
def test_configured_account_must_be_managed(broker_clock: FixedClock) -> None:
    broker = make_broker(broker_clock, account="DU9999999")
    with pytest.raises(BrokerAuthenticationError, match="not managed"):
        broker._resolve_account(FakeIB(accounts=["DU1234567"]))


@pytest.mark.unit
def test_account_mismatch_error_does_not_leak_the_account_number(
    broker_clock: FixedClock,
) -> None:
    broker = make_broker(broker_clock, account="DU9999999")
    with pytest.raises(BrokerAuthenticationError) as excinfo:
        broker._resolve_account(FakeIB(accounts=["DU1234567"]))
    assert "DU1234567" not in str(excinfo.value)


@pytest.mark.unit
def test_single_managed_account_is_used(broker_clock: FixedClock) -> None:
    broker = make_broker(broker_clock)
    assert broker._resolve_account(FakeIB(accounts=["DU1234567"])) == "DU1234567"


# ---------------------------------------------------------------------------
# State reads
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_reads_require_a_connection(broker_clock: FixedClock) -> None:
    broker = make_broker(broker_clock)
    for read in (
        broker.get_account,
        broker.get_positions,
        broker.get_open_orders,
        broker.get_executions,
    ):
        with pytest.raises(BrokerConnectionError, match="not connected"):
            read()


@pytest.mark.unit
def test_account_is_translated_with_decimal_money(broker_clock: FixedClock) -> None:
    broker = connect_fake(make_broker(broker_clock), FakeIB())
    account = broker.get_account()

    assert account.account_id == "DU1234567"
    assert account.currency == "EUR"
    assert account.net_liquidation == Decimal("100000.25")
    assert isinstance(account.cash, Decimal)
    assert account.raw_tags["NetLiquidation"] == "100000.25"


@pytest.mark.unit
def test_unreported_account_field_stays_none(broker_clock: FixedClock) -> None:
    """A tag IBKR did not send must not read as zero."""
    summary = [account_value("NetLiquidation", "100.00", "EUR")]
    broker = connect_fake(make_broker(broker_clock), FakeIB(summary=summary))
    account = broker.get_account()

    assert account.net_liquidation == Decimal("100.00")
    assert account.buying_power is None
    assert account.excess_liquidity is None


@pytest.mark.unit
def test_every_mapped_tag_has_a_model_field(broker_clock: FixedClock) -> None:
    from trading_system.domain.models import BrokerAccount

    for field in ACCOUNT_TAGS.values():
        assert field in BrokerAccount.model_fields


@pytest.mark.unit
def test_empty_account_summary_is_an_error(broker_clock: FixedClock) -> None:
    """Better to fail than to report an account with no balances."""
    broker = connect_fake(make_broker(broker_clock), FakeIB(summary=[]))
    with pytest.raises(BrokerResponseError, match="empty account summary"):
        broker.get_account()


@pytest.mark.unit
def test_positions_prefer_the_valued_portfolio_view(broker_clock: FixedClock) -> None:
    ib = FakeIB(portfolio=[make_portfolio_item()])
    broker = connect_fake(make_broker(broker_clock), ib)
    positions = broker.get_positions()

    assert len(positions) == 1
    assert positions[0].market_value == Decimal("1250.0")


@pytest.mark.unit
def test_positions_fall_back_when_the_portfolio_is_empty(broker_clock: FixedClock) -> None:
    from .conftest import make_position

    ib = FakeIB(portfolio=[], positions=[make_position()])
    broker = connect_fake(make_broker(broker_clock), ib)
    positions = broker.get_positions()

    assert len(positions) == 1
    # The fallback shape has no valuation, and that stays visible as None.
    assert positions[0].market_value is None


@pytest.mark.unit
def test_malformed_position_becomes_a_response_error(broker_clock: FixedClock) -> None:
    broken = make_portfolio_item()
    broken.contract = None
    broker = connect_fake(make_broker(broker_clock), FakeIB(portfolio=[broken]))

    with pytest.raises(BrokerResponseError, match="malformed position"):
        broker.get_positions()


@pytest.mark.unit
def test_open_orders_are_translated(broker_clock: FixedClock) -> None:
    broker = connect_fake(make_broker(broker_clock), FakeIB(trades=[make_trade()]))
    orders = broker.get_open_orders()

    assert len(orders) == 1
    assert orders[0].broker_order_id == "900001"


@pytest.mark.unit
def test_malformed_order_becomes_a_response_error(broker_clock: FixedClock) -> None:
    broken = make_trade()
    broken.order.action = "NONSENSE"
    broker = connect_fake(make_broker(broker_clock), FakeIB(trades=[broken]))

    with pytest.raises(BrokerResponseError, match="malformed order"):
        broker.get_open_orders()


@pytest.mark.unit
def test_executions_are_translated(broker_clock: FixedClock) -> None:
    broker = connect_fake(make_broker(broker_clock), FakeIB(fills=[make_fill()]))
    executions = broker.get_executions()

    assert len(executions) == 1
    assert executions[0].price == Decimal("5.95")


@pytest.mark.unit
def test_malformed_execution_becomes_a_response_error(broker_clock: FixedClock) -> None:
    broken = make_fill()
    broken.execution.execId = ""
    broker = connect_fake(make_broker(broker_clock), FakeIB(fills=[broken]))

    with pytest.raises(BrokerResponseError, match="malformed execution"):
        broker.get_executions()


@pytest.mark.unit
def test_option_contract_resolution_is_refused_without_terms(
    broker_clock: FixedClock,
) -> None:
    """This diagnostic takes no strike or expiry, so it must not pretend to."""
    broker = connect_fake(make_broker(broker_clock), FakeIB())
    with pytest.raises(BrokerConfigurationError, match="Milestone 6"):
        broker.get_contract("NVDA", SecurityType.OPTION)


@pytest.mark.unit
def test_reads_never_submit_orders(broker_clock: FixedClock) -> None:
    ib = FakeIB(portfolio=[make_portfolio_item()], trades=[make_trade()], fills=[make_fill()])
    broker = connect_fake(make_broker(broker_clock), ib)

    broker.get_account()
    broker.get_positions()
    broker.get_open_orders()
    broker.get_executions()
    broker.health_check()

    assert broker.orders_submitted == 0


# ---------------------------------------------------------------------------
# The market-data request asks for tick 21, and cleans up after itself
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_the_quote_request_asks_for_the_average_volume_generic_tick(
    broker_clock: FixedClock,
) -> None:
    """Generic tick 165 is what makes IBKR send tick 21 at all.

    Without it the field is simply never transmitted, and the universe
    liquidity floor would have nothing to read on every symbol forever — the
    failure would look exactly like an account entitlement problem.
    """
    from .conftest import make_ticker

    class QuotingIB(FakeIB):
        def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
            return [SimpleNamespace(symbol="SPY", secType="STK", conId=756733)]

    ib = QuotingIB(ticker=make_ticker(average_daily_volume=52_014_430.0))
    broker = connect_fake(make_broker(broker_clock), ib)

    snapshot = broker.get_market_data("SPY")

    assert ib.generic_tick_list == "165"
    assert snapshot.average_daily_volume == Decimal("52014430")


@pytest.mark.unit
def test_the_streaming_subscription_is_always_cancelled(broker_clock: FixedClock) -> None:
    """A subscription left open holds a market-data line the account may need."""
    from .conftest import make_ticker

    class QuotingIB(FakeIB):
        def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
            return [SimpleNamespace(symbol="SPY", secType="STK", conId=756733)]

    ib = QuotingIB(ticker=make_ticker())
    broker = connect_fake(make_broker(broker_clock), ib)

    broker.get_market_data("SPY")

    assert len(ib.market_data_requests) == 1
    assert len(ib.cancelled_market_data) == 1


@pytest.mark.unit
def test_a_quote_still_returns_when_the_average_never_arrives(
    broker_clock: FixedClock,
) -> None:
    """Tick 21 is waited for briefly, then given up on — never indefinitely.

    An account without the entitlement must still get its prices, with the
    average honestly absent rather than the collection hanging on every symbol.
    """
    from .conftest import NAN, make_ticker

    class NoAverageIB(FakeIB):
        def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
            return [SimpleNamespace(symbol="SPY", secType="STK", conId=756733)]

    ib = NoAverageIB(ticker=make_ticker(average_daily_volume=NAN))
    broker = connect_fake(make_broker(broker_clock), ib)

    snapshot = broker.get_market_data("SPY")

    assert snapshot.has_quote
    assert snapshot.average_daily_volume is None
    assert ib.cancelled_market_data, "the subscription must be cancelled even so"


@pytest.mark.unit
def test_reading_a_quote_submits_no_orders(broker_clock: FixedClock) -> None:
    from .conftest import make_ticker

    class QuotingIB(FakeIB):
        def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
            return [SimpleNamespace(symbol="SPY", secType="STK", conId=756733)]

    broker = connect_fake(make_broker(broker_clock), QuotingIB(ticker=make_ticker()))

    broker.get_market_data("SPY")

    assert broker.orders_submitted == 0


# ---------------------------------------------------------------------------
# The per-currency ledger rows that arrive with every account summary
# ---------------------------------------------------------------------------
#
# ``ib_async`` puts ``$LEDGER:ALL`` in its account-summary tag list, so IBKR
# returns the cash tags once per currency alongside the account-level ones.
# Everything below is about not letting those two kinds of row be confused.
def ledger_summary() -> list[SimpleNamespace]:
    """A EUR-based account holding euro and no dollars, as IBKR reports it.

    ``TotalCashValue`` appears three times — once for the account (BASE) and
    once per currency — which is exactly the shape that used to leave
    ``BrokerAccount.cash`` holding whichever currency arrived last.
    """
    return [
        account_value("NetLiquidation", "5000.00", "EUR"),
        account_value("BuyingPower", "20000.00", "EUR"),
        account_value("AvailableFunds", "5000.00", "EUR"),
        account_value("TotalCashValue", "5000.00", "BASE"),
        account_value("TotalCashValue", "5000.00", "EUR"),
        account_value("TotalCashValue", "0.00", "USD"),
        account_value("CashBalance", "5000.00", "EUR"),
        account_value("CashBalance", "0.00", "USD"),
        account_value("CashBalance", "5000.00", "BASE"),
        account_value("ExchangeRate", "1.00", "EUR"),
        account_value("ExchangeRate", "0.855", "USD"),
        account_value("ExchangeRate", "1.00", "BASE"),
    ]


@pytest.mark.unit
def test_a_per_currency_cash_row_does_not_stand_in_for_the_account(
    broker_clock: FixedClock,
) -> None:
    """The bug this parsing exists to prevent, pinned as an inequality.

    ``TotalCashValue`` is a ledger tag, so it arrives once per currency. A loop
    keyed on the tag alone takes whichever came last — here USD 0.00 — and
    reports an account holding EUR 5,000 as holding nothing.
    """
    broker = connect_fake(make_broker(broker_clock), FakeIB(summary=ledger_summary()))

    account = broker.get_account()

    assert account.currency == "EUR"
    assert account.cash == Decimal("5000.00")
    assert account.cash != Decimal("0.00"), "the USD ledger row must not stand in for the base"


@pytest.mark.unit
def test_the_per_currency_cash_is_recorded_separately_and_never_summed(
    broker_clock: FixedClock,
) -> None:
    """EUR 5,000 and USD 0 are two facts. A total would need a rate."""
    broker = connect_fake(make_broker(broker_clock), FakeIB(summary=ledger_summary()))

    account = broker.get_account()

    assert account.cash_by_currency == {"EUR": Decimal("5000.00"), "USD": Decimal("0.00")}


@pytest.mark.unit
def test_the_exchange_rates_arrive_with_the_balance_they_convert(broker_clock: FixedClock) -> None:
    """No extra round trip: they are rows in a summary already being read.

    That matters twice over. Milestone 2 established that a second uncached
    request on one connection can go unanswered indefinitely, so a rate fetched
    separately would be a real hazard; and a rate read at another instant could
    convert a balance it does not belong to.
    """
    broker = connect_fake(make_broker(broker_clock), FakeIB(summary=ledger_summary()))

    account = broker.get_account()

    assert account.exchange_rates == {"USD": Decimal("0.855")}
    assert "EUR" not in account.exchange_rates, "the base against itself is not an observation"
    assert "BASE" not in account.exchange_rates


@pytest.mark.unit
def test_a_per_currency_row_keeps_its_own_key_in_the_raw_tags(broker_clock: FixedClock) -> None:
    """Audit data that overwrote itself would be worse than none."""
    broker = connect_fake(make_broker(broker_clock), FakeIB(summary=ledger_summary()))

    account = broker.get_account()

    assert account.raw_tags["TotalCashValue"] == "5000.00"
    assert account.raw_tags["TotalCashValue:USD"] == "0.00"
    assert account.raw_tags["ExchangeRate:USD"] == "0.855"


@pytest.mark.unit
def test_an_account_quoting_no_rates_reports_none_rather_than_one(broker_clock: FixedClock) -> None:
    """A broker that said nothing must be recorded as having said nothing."""
    summary = [row for row in ledger_summary() if row.tag != "ExchangeRate"]
    broker = connect_fake(make_broker(broker_clock), FakeIB(summary=summary))

    account = broker.get_account()

    assert account.exchange_rates == {}
