"""IBKR broker adapter.

This is the only module in the system that imports ``ib_async``. Everything it
returns is a domain model, so no IBKR type escapes this package.

Safety properties, in order of how much they matter:

* **LIVE is refused.** DRY_RUN and PAPER only; live readiness is Milestone 12.
* **The API connection is opened read-only by default.** ``IB.connect(readonly=True)``
  makes IBKR itself reject order placement, so a bug here cannot trade — the
  guarantee does not depend on our own code being correct. Milestone 8 can open
  a *writable* connection, but only in PAPER, only through
  ``broker.factory.build_execution_broker``, and only after
  :meth:`IBKRBroker.verify_paper_account` has shown which account answered.
* **Account identity is required.** If the account cannot be determined, or
  the configured account is not among those the connection manages, the
  connection is closed and an error raised. Unknown broker state is not a
  state to trade from.
* **Nothing is fabricated.** Every failure raises; no method fills in a
  placeholder price, position or balance.

The library is imported lazily so the package remains importable — and the
whole unit test suite runnable — without ``ib_async`` installed.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, TypeGuard

from trading_system.broker.base import (
    Broker,
    BrokerAuthenticationError,
    BrokerConfigurationError,
    BrokerConnectionError,
    BrokerResponseError,
    BrokerTimeoutError,
    MarketDataUnavailableError,
    OptionChainUnavailableError,
)
from trading_system.broker.ibkr.executions import to_broker_executions
from trading_system.broker.ibkr.market_data import (
    IBKR_SOURCE,
    to_market_data_snapshot,
    to_option_chain_snapshot,
)
from trading_system.broker.ibkr.order_translation import (
    IBKROrderRequest,
    OrderTranslationError,
    to_ibkr_order_request,
)
from trading_system.broker.ibkr.orders import (
    to_broker_order,
    to_broker_orders,
    to_execution_result,
)
from trading_system.broker.ibkr.positions import to_broker_positions
from trading_system.domain.enums import (
    BrokerConnectionState,
    SecurityType,
    TradingMode,
)
from trading_system.domain.models import (
    BrokerAccount,
    BrokerContract,
    BrokerExecution,
    BrokerHealth,
    BrokerOrder,
    BrokerPosition,
    ExecutionResult,
    MarketDataSnapshot,
    OptionChainSnapshot,
    OrderIntent,
)
from trading_system.infrastructure.clock import Clock, SystemClock

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from datetime import datetime

__all__ = ["ACCOUNT_TAGS", "IBKRBroker"]

#: IBKR account summary tags mapped onto :class:`BrokerAccount` fields.
ACCOUNT_TAGS = {
    "TotalCashValue": "cash",
    "NetLiquidation": "net_liquidation",
    "BuyingPower": "buying_power",
    "AvailableFunds": "available_funds",
    "FullInitMarginReq": "initial_margin",
    "FullMaintMarginReq": "maintenance_margin",
    "ExcessLiquidity": "excess_liquidity",
    "UnrealizedPnL": "unrealized_pnl",
    "RealizedPnL": "realized_pnl",
}

_SECURITY_TYPE_CODES = {
    SecurityType.STOCK: "STK",
    SecurityType.INDEX: "IND",
    SecurityType.CASH: "CASH",
}

#: IBKR generic tick 165 ("Miscellaneous Stats") is what makes tick 21
#: (``avVolume``) arrive at all. Without it the field is never sent and the
#: liquidity floor has nothing honest to read.
_AVERAGE_VOLUME_GENERIC_TICK = "165"

#: How much longer to wait for tick 21 once a price is already in hand. Short
#: on purpose: a feed that does not serve it should cost one grace period per
#: symbol, not a full request timeout.
_AVERAGE_VOLUME_GRACE_SECONDS = 2.0


def _reported(value: Any) -> TypeGuard[float]:
    """Did IBKR actually send a number here?

    ``ib_async`` initialises unset ticker fields to ``NaN``, which is neither
    absent nor a value — and ``NaN`` compares false against everything,
    including itself, which is exactly what ``value == value`` exploits. A
    naive truth test would read "not sent yet" as "sent".
    """
    return isinstance(value, float | int) and not isinstance(value, bool) and value == value


def _priced(ticker: Any) -> bool:
    """Whether any usable price has arrived on a streaming ticker."""
    for field in ("bid", "ask", "last", "close"):
        value = getattr(ticker, field, None)
        if _reported(value) and value > 0:
            return True
    return False


def _import_ib_async() -> Any:
    """Import ``ib_async``, turning a missing dependency into a clear error."""
    try:
        import ib_async
    except ImportError as exc:  # pragma: no cover - exercised by absence
        raise BrokerConfigurationError(
            "ib_async is not installed. Install the broker extra: uv pip install -e '.[ibkr]'"
        ) from exc
    return ib_async


class IBKRBroker(Broker):
    """Read-only IBKR adapter over IB Gateway / TWS."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        account: str | None = None,
        trading_mode: TradingMode = TradingMode.PAPER,
        read_only: bool = True,
        connect_timeout_seconds: float = 10.0,
        request_timeout_seconds: float = 30.0,
        market_data_type: int = 3,
        order_exchange: str = "SMART",
        order_currency: str = "USD",
        clock: Clock | None = None,
    ) -> None:
        super().__init__(trading_mode=trading_mode, read_only=read_only)
        self._validate_configuration(host, port, client_id, trading_mode, read_only)
        if request_timeout_seconds <= 0:
            raise BrokerConfigurationError(
                "request_timeout_seconds must be positive: an unbounded IBKR request can "
                "hang the process forever when a round trip goes unanswered"
            )
        self._host = host
        self._port = port
        self._client_id = client_id
        self._configured_account = account or None
        self._connect_timeout = connect_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._market_data_type = market_data_type
        #: Routing for submitted orders. SMART is IBKR's router; the chain the
        #: contract came from was read from the same venue, so an order routed
        #: elsewhere could reach a different book than the one that was priced.
        self._order_exchange = order_exchange
        self._order_currency = order_currency
        self._clock = clock or SystemClock()

        self._ib: Any = None
        self._account_id: str | None = None
        self._last_communication: datetime | None = None
        self._last_error: str | None = None

    @staticmethod
    def _validate_configuration(
        host: str, port: int, client_id: int, trading_mode: TradingMode, read_only: bool
    ) -> None:
        if trading_mode is TradingMode.LIVE:
            raise BrokerConfigurationError(
                "LIVE trading is not available. Milestone 2 supports DRY_RUN and "
                "PAPER only; live readiness is Milestone 12."
            )
        if not host or not host.strip():
            raise BrokerConfigurationError("IBKR_HOST is not configured")
        if not 1 <= port <= 65535:
            raise BrokerConfigurationError(f"IBKR_PORT {port} is outside the valid range")
        if client_id < 0:
            raise BrokerConfigurationError(f"IBKR_CLIENT_ID {client_id} must not be negative")
        if not read_only and trading_mode is not TradingMode.PAPER:
            # A writable connection exists for exactly one purpose: submitting
            # a paper order through the Milestone 8 execution service. Any
            # other mode asking for one is a configuration error, and DRY_RUN
            # in particular must never reach a real gateway — that is what the
            # simulator is for.
            raise BrokerConfigurationError(
                f"a writable IBKR connection requires TRADING_MODE=PAPER, not "
                f"{trading_mode.value}. Order submission is permitted against the paper "
                f"account only; live readiness is Milestone 12."
            )

    @property
    def name(self) -> str:
        return IBKR_SOURCE

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def account_id(self) -> str | None:
        return self._account_id

    @property
    def is_connected(self) -> bool:
        return bool(self._ib is not None and self._ib.isConnected())

    # --- connection --------------------------------------------------------
    def connect(self) -> BrokerHealth:
        """Open a read-only connection and establish account identity."""
        ib_async = _import_ib_async()
        ib = ib_async.IB()
        # ib_async's synchronous wrappers default to RequestTimeout = 0, which
        # means "wait forever". Against the validated TWS environment a second
        # uncached round trip on one connection can go unanswered indefinitely,
        # so an unbounded wrapper is a guaranteed hang rather than a
        # theoretical one. Bounding it turns that into a timeout we can fail
        # safe on.
        ib.RequestTimeout = self._request_timeout

        try:
            ib.connect(
                host=self._host,
                port=self._port,
                clientId=self._client_id,
                timeout=self._connect_timeout,
                # When read-only, IBKR itself blocks order placement on this
                # connection, so the guarantee does not rest on our own code
                # being correct. A writable connection is only ever opened by
                # the execution path, in PAPER, and the constructor already
                # refused every other combination.
                readonly=self._read_only,
                account=self._configured_account or "",
            )
        except TimeoutError as exc:
            self._last_error = f"timeout after {self._connect_timeout}s"
            raise BrokerTimeoutError(
                f"IBKR did not respond at {self._host}:{self._port} within "
                f"{self._connect_timeout}s. Is IB Gateway running?"
            ) from exc
        except ConnectionRefusedError as exc:
            self._last_error = "connection refused"
            raise BrokerConnectionError(
                f"IBKR refused the connection at {self._host}:{self._port}. Check the "
                f"port matches the running gateway and that the API is enabled."
            ) from exc
        except OSError as exc:
            self._last_error = str(exc)
            raise BrokerConnectionError(
                f"could not reach IBKR at {self._host}:{self._port}: {exc}"
            ) from exc
        except Exception as exc:  # library-specific failures
            self._last_error = str(exc)
            raise BrokerConnectionError(f"IBKR connection failed: {exc}") from exc

        self._ib = ib
        try:
            self._account_id = self._resolve_account(ib)
            ib.reqMarketDataType(self._market_data_type)
        except Exception:
            # Never leave a half-open connection behind.
            self.disconnect()
            raise

        self._last_communication = self._clock.now()
        self._last_error = None
        # Do not spend a live round trip on a self-check here: against some
        # TWS builds, only the first live request/response on a freshly
        # opened connection is reliably answered (confirmed by direct
        # ib_async probing — a second request on the same connection can go
        # unanswered indefinitely even though the socket is fine and the
        # first request worked). The connection handshake plus the account
        # lookup above already proves liveness without spending that budget,
        # leaving it for whatever the caller does next.
        return self.health_check(probe_latency=False)

    def _resolve_account(self, ib: Any) -> str:
        """Determine which account this connection speaks for.

        Fails closed: an ambiguous or unmatched account is an error, not a
        guess. Trading against the wrong account is exactly the failure this
        prevents.
        """
        try:
            managed = [a for a in (ib.managedAccounts() or []) if a and a.strip()]
        except Exception as exc:
            raise BrokerResponseError(f"could not read managed accounts: {exc}") from exc

        if not managed:
            raise BrokerAuthenticationError(
                "IBKR reported no managed accounts. The gateway is connected but not "
                "logged in, or the API user has no account access."
            )

        if self._configured_account:
            if self._configured_account not in managed:
                raise BrokerAuthenticationError(
                    f"configured IBKR_ACCOUNT is not managed by this connection "
                    f"(connection manages {len(managed)} account(s))"
                )
            return self._configured_account

        if len(managed) > 1:
            raise BrokerConfigurationError(
                f"this login manages {len(managed)} accounts; set IBKR_ACCOUNT "
                f"explicitly so the system cannot act on the wrong one"
            )
        return str(managed[0])

    def disconnect(self) -> None:
        # Disconnect must never raise: it runs in `finally` blocks, where an
        # exception would mask the error that caused the teardown.
        if self._ib is not None:
            with suppress(Exception):
                self._ib.disconnect()
        self._ib = None
        self._account_id = None

    def health_check(self, *, probe_latency: bool = True) -> BrokerHealth:
        """Report connection state. Never raises.

        ``probe_latency`` issues a live ``reqCurrentTime`` round trip to
        measure latency and confirm the broker is actually answering, not
        just that the socket is open. Skip it when the connection has not
        yet spent its round trip elsewhere (see ``connect``'s use of this
        with ``probe_latency=False``) — some TWS builds reliably answer only
        the first live request per connection and leave later ones pending
        forever, so this must stay opt-out, not opt-in, to avoid silently
        reintroducing a hang at every call site that checks health after
        already using the connection for something else.
        """
        now = self._clock.now()
        if self._ib is None:
            return BrokerHealth(
                broker=self.name,
                state=BrokerConnectionState.DISCONNECTED,
                as_of=now,
                trading_mode=self.trading_mode,
                host=self._host,
                port=self._port,
                read_only=self.read_only,
                last_successful_communication=self._last_communication,
                error=self._last_error,
            )

        try:
            connected = bool(self._ib.isConnected())
        except Exception as exc:
            return BrokerHealth(
                broker=self.name,
                state=BrokerConnectionState.ERROR,
                as_of=now,
                trading_mode=self.trading_mode,
                host=self._host,
                port=self._port,
                read_only=self.read_only,
                last_successful_communication=self._last_communication,
                error=str(exc),
            )

        if not connected:
            return BrokerHealth(
                broker=self.name,
                state=BrokerConnectionState.DISCONNECTED,
                as_of=now,
                trading_mode=self.trading_mode,
                host=self._host,
                port=self._port,
                read_only=self.read_only,
                last_successful_communication=self._last_communication,
                error=self._last_error,
            )

        latency_ms: float | None = None
        error: str | None = None
        state = BrokerConnectionState.CONNECTED
        if probe_latency:
            try:
                started = time.perf_counter()
                # ib_async's synchronous IB.reqCurrentTime() has no timeout
                # and can hang indefinitely on a repeat call over the same
                # connection (observed against a real TWS: the first live
                # request/response on a fresh connection returns, a later
                # one on that same connection never does). Where the async
                # form is available, route through it with an explicit
                # bound so a broken round trip surfaces as ERROR instead of
                # hanging the whole process.
                req_current_time_async = getattr(self._ib, "reqCurrentTimeAsync", None)
                if req_current_time_async is not None:
                    _import_ib_async().util.run(
                        req_current_time_async(), timeout=self._connect_timeout
                    )
                else:
                    self._ib.reqCurrentTime()
                latency_ms = (time.perf_counter() - started) * 1000
                self._last_communication = self._clock.now()
            except Exception as exc:
                state = BrokerConnectionState.ERROR
                error = str(exc)

        return BrokerHealth(
            broker=self.name,
            state=state,
            as_of=now,
            trading_mode=self.trading_mode,
            host=self._host,
            port=self._port,
            account_id=self._account_id,
            read_only=self.read_only,
            latency_ms=latency_ms,
            last_successful_communication=self._last_communication,
            server_version=self._server_version(),
            error=error,
        )

    def _server_version(self) -> int | None:
        try:
            version = self._ib.client.serverVersion()
        except Exception:
            return None
        return version if isinstance(version, int) and version > 0 else None

    def _require_connection(self) -> Any:
        if self._ib is None or not self.is_connected:
            raise BrokerConnectionError(f"{self.name} is not connected. Call connect() first.")
        return self._ib

    def _timed_out(self, what: str) -> BrokerTimeoutError:
        """Turn an exhausted request bound into a typed, actionable failure.

        Distinct from a response error on purpose: a timeout means IBKR never
        answered, which is the documented one-live-round-trip failure mode and
        calls for a fresh connection rather than for retrying on this one.
        """
        self._last_error = f"{what} timed out after {self._request_timeout}s"
        return BrokerTimeoutError(
            f"IBKR did not answer the {what} request within {self._request_timeout}s. "
            f"Only the first uncached round trip on a connection is reliably answered; "
            f"open a fresh connection for the next uncached request."
        )

    # --- read-only state ---------------------------------------------------
    def get_account_summary(self) -> dict[str, str]:
        """Raw tag/value pairs exactly as IBKR reported them."""
        ib = self._require_connection()
        try:
            values = ib.accountSummary(self._account_id or "")
        except TimeoutError as exc:
            raise self._timed_out("account summary") from exc
        except Exception as exc:
            raise BrokerResponseError(f"could not read the account summary: {exc}") from exc
        summary = {
            str(v.tag): str(v.value)
            for v in values
            if getattr(v, "tag", None) and not getattr(v, "modelCode", "")
        }
        if not summary:
            raise BrokerResponseError("IBKR returned an empty account summary")
        self._last_communication = self._clock.now()
        return summary

    def get_account(self) -> BrokerAccount:
        ib = self._require_connection()
        if self._account_id is None:
            raise BrokerAuthenticationError("account identity is unknown")

        try:
            values = ib.accountSummary(self._account_id)
        except TimeoutError as exc:
            raise self._timed_out("account summary") from exc
        except Exception as exc:
            raise BrokerResponseError(f"could not read the account summary: {exc}") from exc

        from trading_system.broker.ibkr.conversion import to_decimal

        fields: dict[str, Any] = {}
        raw_tags: dict[str, str] = {}
        currency: str | None = None

        for value in values:
            tag = str(getattr(value, "tag", "") or "")
            # A non-empty modelCode marks a what-if/model row, not real state.
            if not tag or str(getattr(value, "modelCode", "") or ""):
                continue
            raw_tags[tag] = str(getattr(value, "value", ""))
            if tag in ACCOUNT_TAGS:
                fields[ACCOUNT_TAGS[tag]] = to_decimal(getattr(value, "value", None))
                if tag == "NetLiquidation":
                    currency = str(getattr(value, "currency", "") or "") or None

        if not raw_tags:
            raise BrokerResponseError("IBKR returned an empty account summary")

        self._last_communication = self._clock.now()
        return BrokerAccount(
            account_id=self._account_id,
            currency=currency or "USD",
            as_of=self._clock.now(),
            source=self.name,
            raw_tags=raw_tags,
            **fields,
        )

    def get_positions(self) -> list[BrokerPosition]:
        """Positions, preferring the valued portfolio view.

        ``portfolio()`` carries market value and P&L; ``positions()`` does not.
        The fallback is used only when the portfolio view is empty, so missing
        valuation is visible as ``None`` rather than absent rows.
        """
        ib = self._require_connection()
        now = self._clock.now()
        try:
            items = list(ib.portfolio(self._account_id or ""))
            if not items:
                items = list(ib.positions(self._account_id or ""))
        except TimeoutError as exc:
            raise self._timed_out("positions") from exc
        except Exception as exc:
            raise BrokerResponseError(f"could not read positions: {exc}") from exc

        self._last_communication = self._clock.now()
        try:
            return to_broker_positions(items, now, account_id=self._account_id)
        except ValueError as exc:
            raise BrokerResponseError(f"malformed position from IBKR: {exc}") from exc

    def get_open_orders(self) -> list[BrokerOrder]:
        ib = self._require_connection()
        now = self._clock.now()
        try:
            trades = list(ib.openTrades())
        except TimeoutError as exc:
            raise self._timed_out("open orders") from exc
        except Exception as exc:
            raise BrokerResponseError(f"could not read open orders: {exc}") from exc

        self._last_communication = self._clock.now()
        try:
            return to_broker_orders(trades, now, account_id=self._account_id)
        except ValueError as exc:
            raise BrokerResponseError(f"malformed order from IBKR: {exc}") from exc

    def get_executions(self) -> list[BrokerExecution]:
        ib = self._require_connection()
        now = self._clock.now()
        try:
            fills = list(ib.fills())
        except TimeoutError as exc:
            raise self._timed_out("executions") from exc
        except Exception as exc:
            raise BrokerResponseError(f"could not read executions: {exc}") from exc

        self._last_communication = self._clock.now()
        try:
            return to_broker_executions(fills, now)
        except ValueError as exc:
            raise BrokerResponseError(f"malformed execution from IBKR: {exc}") from exc

    # --- contracts and market data -----------------------------------------
    def _build_contract(
        self,
        symbol: str,
        security_type: SecurityType,
        exchange: str | None,
        currency: str | None,
    ) -> Any:
        ib_async = _import_ib_async()
        code = _SECURITY_TYPE_CODES.get(security_type)
        if code is None:
            raise BrokerConfigurationError(
                f"resolving a {security_type.value} contract needs strike and expiry, "
                f"which this read-only diagnostic does not take. Option contract "
                f"selection is Milestone 6."
            )
        return ib_async.Contract(
            secType=code,
            symbol=symbol.upper(),
            exchange=exchange or "SMART",
            currency=currency or "USD",
        )

    def get_contract(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
        *,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> BrokerContract:
        ib = self._require_connection()
        contract = self._build_contract(symbol, security_type, exchange, currency)

        try:
            qualified = ib.qualifyContracts(contract)
        except TimeoutError as exc:
            raise self._timed_out(f"contract qualification for {symbol.upper()}") from exc
        except Exception as exc:
            raise BrokerResponseError(f"could not qualify {symbol}: {exc}") from exc
        if not qualified:
            raise BrokerResponseError(
                f"IBKR could not resolve {symbol.upper()} as {security_type.value}"
            )

        resolved = qualified[0]
        self._last_communication = self._clock.now()

        from trading_system.broker.ibkr.conversion import (
            to_date,
            to_decimal,
            to_option_right,
            to_security_type,
        )

        multiplier = to_decimal(getattr(resolved, "multiplier", None))
        strike = to_decimal(getattr(resolved, "strike", None))
        return BrokerContract(
            symbol=str(getattr(resolved, "symbol", symbol.upper())),
            security_type=to_security_type(getattr(resolved, "secType", None)),
            as_of=self._clock.now(),
            source=self.name,
            contract_id=getattr(resolved, "conId", None) or None,
            exchange=getattr(resolved, "exchange", None) or None,
            primary_exchange=getattr(resolved, "primaryExchange", None) or None,
            currency=getattr(resolved, "currency", None) or None,
            local_symbol=getattr(resolved, "localSymbol", None) or None,
            trading_class=getattr(resolved, "tradingClass", None) or None,
            multiplier=int(multiplier) if multiplier and multiplier > 0 else None,
            expiration=to_date(getattr(resolved, "lastTradeDateOrContractMonth", None)),
            strike=strike if strike and strike > 0 else None,
            right=to_option_right(getattr(resolved, "right", None)),
        )

    def get_market_data(
        self,
        symbol: str,
        security_type: SecurityType = SecurityType.STOCK,
    ) -> MarketDataSnapshot:
        """Fetch a snapshot quote.

        Raises :class:`MarketDataUnavailableError` when IBKR has nothing to
        give — a missing subscription or a closed market must never become an
        invented price.
        """
        ib = self._require_connection()
        contract = self._build_contract(symbol, security_type, None, None)

        try:
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                raise MarketDataUnavailableError(
                    f"MARKET_DATA_UNAVAILABLE: IBKR could not resolve {symbol.upper()}"
                )
            ticker = self._request_quote(ib, qualified[0])
        except MarketDataUnavailableError:
            raise
        except TimeoutError as exc:
            raise self._timed_out(f"market data for {symbol.upper()}") from exc
        except Exception as exc:
            raise MarketDataUnavailableError(
                f"MARKET_DATA_UNAVAILABLE: IBKR rejected the market data request for "
                f"{symbol.upper()}: {exc}"
            ) from exc

        if ticker is None:
            raise MarketDataUnavailableError(
                f"MARKET_DATA_UNAVAILABLE: IBKR returned no ticker for {symbol.upper()}"
            )

        self._last_communication = self._clock.now()
        snapshot = to_market_data_snapshot(
            ticker, self._clock.now(), fallback_symbol=symbol.upper()
        )
        if not snapshot.has_quote:
            raise MarketDataUnavailableError(
                f"MARKET_DATA_UNAVAILABLE: no usable price for {symbol.upper()}. "
                f"A market data subscription may be required."
            )
        return snapshot

    def _request_quote(self, ib: Any, contract: Any) -> Any:
        """Subscribe briefly, so generic tick 165 can be requested.

        ``reqTickers`` takes no generic tick list, which puts IBKR tick 21
        (``avVolume``) out of reach — and that is the only volume field this
        system trusts for a liquidity floor, because tick 74 arrives corrupted
        on the delayed feed. A short streaming subscription is IBKR's own way
        to ask for it.

        This is still **one** market-data operation on the connection, not a
        second uncached round trip: a subscription is opened, its ticks are
        awaited under a bound, and it is cancelled before returning. The
        Milestone 2/3 one-purpose-per-connection contract is unchanged, and
        :class:`~trading_system.data.providers.broker_session.BrokerSession`
        still gets exactly one operation per connect.
        """
        ticker = ib.reqMktData(
            contract, genericTickList=_AVERAGE_VOLUME_GENERIC_TICK, snapshot=False
        )
        try:
            self._await_quote(ib, ticker)
        finally:
            # A failed cancel must not discard a quote we already hold. The
            # connection is short-lived and closes moments later regardless.
            with suppress(Exception):
                ib.cancelMktData(contract)
        return ticker

    def _await_quote(self, ib: Any, ticker: Any) -> None:
        """Wait, bounded, for a price — and a little longer for tick 21.

        Two deadlines, deliberately. A quote that never arrives becomes the
        caller's ``MARKET_DATA_UNAVAILABLE``, so the price gets the full
        request bound. ``avVolume`` is a different matter: it is not always
        served, and spending the whole bound on every symbol to discover that
        would make collection crawl. So once a price is in hand tick 21 gets a
        short grace period, after which the quote is returned **without** it —
        honestly absent rather than waited for indefinitely, and never
        substituted from ``volume``.
        """
        deadline = time.monotonic() + self._request_timeout
        grace_deadline: float | None = None

        while time.monotonic() < deadline:
            if _reported(getattr(ticker, "avVolume", None)) and _priced(ticker):
                return
            if _priced(ticker) and grace_deadline is None:
                grace_deadline = min(time.monotonic() + _AVERAGE_VOLUME_GRACE_SECONDS, deadline)
            if grace_deadline is not None and time.monotonic() >= grace_deadline:
                return
            try:
                ib.waitOnUpdate(timeout=min(0.5, max(deadline - time.monotonic(), 0.05)))
            except Exception:
                # A wait that fails is a wait that ended; the loop's own
                # deadline bounds this, not the library's cooperation.
                return

    def get_option_chain(self, underlying: str) -> OptionChainSnapshot:
        ib = self._require_connection()
        contract = self.get_contract(underlying, SecurityType.STOCK)
        if contract.contract_id is None:
            raise OptionChainUnavailableError(
                f"cannot request a chain for {underlying.upper()}: no contract id"
            )

        try:
            chains = ib.reqSecDefOptParams(underlying.upper(), "", "STK", contract.contract_id)
        except TimeoutError as exc:
            raise self._timed_out(f"option chain for {underlying.upper()}") from exc
        except Exception as exc:
            raise OptionChainUnavailableError(
                f"IBKR rejected the option chain request for {underlying.upper()}: {exc}"
            ) from exc

        if not chains:
            raise OptionChainUnavailableError(
                f"IBKR returned no option chain for {underlying.upper()}"
            )

        self._last_communication = self._clock.now()
        try:
            return to_option_chain_snapshot(
                list(chains),
                underlying.upper(),
                self._clock.now(),
                underlying_contract_id=contract.contract_id,
            )
        except ValueError as exc:
            raise OptionChainUnavailableError(str(exc)) from exc

    # --- execution (Milestone 8) -------------------------------------------
    def verify_paper_account(self, prefixes: Sequence[str]) -> str:
        """Prove this session speaks for a paper account, or refuse.

        IBKR paper accounts are prefixed ``DU`` (``DF`` for paper advisor
        accounts) and live accounts are not. That is the only account-level
        evidence available without spending a round trip, and it is checked
        against the identity resolved during the handshake rather than by
        asking again — ``managedAccounts()`` is served from the connection's
        startup cache, so this costs nothing and cannot hang.

        Fails closed. An account this cannot recognise is refused rather than
        assumed to be paper, because the failure mode of the other choice is
        an order against real money.
        """
        account = self._account_id
        if not account:
            raise BrokerAuthenticationError(
                "the connection has no resolved account, so it cannot be shown to be a paper "
                "account. Refusing to submit."
            )
        if not any(account.upper().startswith(prefix.upper()) for prefix in prefixes):
            raise BrokerConfigurationError(
                f"the connected account does not match any configured paper account prefix "
                f"({', '.join(prefixes)}). Refusing to submit an order against an account "
                f"that cannot be shown to be a paper account."
            )
        if self.trading_mode is not TradingMode.PAPER:
            raise BrokerConfigurationError(
                f"trading mode is {self.trading_mode.value}; paper submission requires PAPER."
            )
        return account

    def _submit_order(self, intent: OrderIntent) -> ExecutionResult:
        """Send one order, and spend the connection's round trip on it.

        Deliberately does *not* run a health check, re-read the account or
        qualify the contract first. Milestone 2 established that only the first
        uncached round trip on a freshly opened connection is reliably answered
        against the validated TWS build, so anything done before the submission
        risks consuming the one exchange that matters. Every contract is
        addressed by the ``conId`` Milestone 6 resolved, which is what makes
        skipping qualification possible rather than merely optimistic.

        A submission that is not acknowledged within the request timeout raises
        :class:`BrokerTimeoutError`. That is *not* the same as "the order was
        not sent": the caller records an uncertain submission and resolves it
        by looking, never by sending a second order.
        """
        ib_async = _import_ib_async()
        ib = self._require_connection()

        try:
            request = to_ibkr_order_request(
                intent,
                exchange=self._order_exchange,
                currency=self._order_currency,
                account=self._account_id,
            )
        except OrderTranslationError as exc:
            # Nothing has been sent, and nothing will be. Distinct from a
            # broker failure on purpose: this is our own refusal.
            raise BrokerConfigurationError(str(exc)) from exc

        contract = self._build_order_contract(ib_async, request)
        order = self._build_order(ib_async, request)

        try:
            trade = ib.placeOrder(contract, order)
        except Exception as exc:
            raise BrokerResponseError(
                f"IBKR refused the order submission: {exc}. Whether it reached the broker is "
                f"unknown; do not resubmit without checking open orders."
            ) from exc

        self._await_acknowledgement(ib, trade)
        self._last_communication = self._clock.now()
        return to_execution_result(
            trade,
            intent_id=intent.intent_id,
            as_of=self._clock.now(),
            trading_mode=self.trading_mode,
            orders_submitted=1,
        )

    def _await_acknowledgement(self, ib: Any, trade: Any) -> None:
        """Wait, bounded, for IBKR to say something about the order.

        Bounded is the whole point. ``ib_async``'s waiting primitives can be
        given no deadline at all, and an unanswered submission would then hang
        the process holding a live order it cannot describe.

        Not answering is not an error here: an order that has been placed but
        not yet acknowledged is a real state, and the caller records it as
        uncertain. What must never happen is waiting forever.
        """
        deadline = time.monotonic() + self._request_timeout
        while time.monotonic() < deadline:
            status = getattr(getattr(trade, "orderStatus", None), "status", None)
            if isinstance(status, str) and status.strip() and status != "PendingSubmit":
                return
            if getattr(trade, "log", None):
                return
            try:
                ib.waitOnUpdate(timeout=min(1.0, max(deadline - time.monotonic(), 0.05)))
            except Exception:
                # A wait that fails is a wait that ended. The loop's own
                # deadline is what bounds this, not the library's cooperation.
                return

    def _build_order_contract(self, ib_async: Any, request: IBKROrderRequest) -> Any:
        """Build the ``ib_async`` contract. The only place a library object appears."""
        contract = ib_async.Contract(
            secType=request.security_type,
            symbol=request.symbol,
            exchange=request.exchange,
            currency=request.currency,
        )
        if request.contract_id is not None:
            contract.conId = request.contract_id
        if request.trading_class:
            contract.tradingClass = request.trading_class
        if request.combo_legs:
            contract.comboLegs = [
                ib_async.ComboLeg(
                    conId=leg.contract_id,
                    ratio=leg.ratio,
                    action=leg.action,
                    exchange=leg.exchange,
                )
                for leg in request.combo_legs
            ]
        return contract

    def _build_order(self, ib_async: Any, request: IBKROrderRequest) -> Any:
        order = ib_async.Order(
            action=request.action,
            orderType=request.order_type,
            totalQuantity=request.total_quantity,
            tif=request.time_in_force,
            transmit=request.transmit,
        )
        if request.limit_price is not None:
            order.lmtPrice = float(request.limit_price)
        if request.account:
            order.account = request.account
        if request.order_ref:
            order.orderRef = request.order_ref
        return order

    def _cancel_order(self, broker_order_id: str) -> BrokerOrder:
        """Cancel one order and report what IBKR then says about it.

        Cancellation is intentionally narrow (brief section 37): it exists so a
        submitted order's lifecycle can be closed, not so a strategy can manage
        exits.
        """
        ib = self._require_connection()
        now = self._clock.now()

        trade = self._find_trade(ib, broker_order_id)
        if trade is None:
            raise BrokerResponseError(
                f"IBKR reports no open order {broker_order_id}. It may have filled, been "
                f"cancelled already, or belong to another session; refusing to report a "
                f"cancellation that did not happen."
            )

        try:
            ib.cancelOrder(trade.order)
        except Exception as exc:
            raise BrokerResponseError(f"IBKR refused to cancel {broker_order_id}: {exc}") from exc

        self._last_communication = now
        return to_broker_order(trade, self._clock.now(), account_id=self._account_id)

    def _find_trade(self, ib: Any, broker_order_id: str) -> Any:
        """Locate a trade among the open orders the handshake already cached."""
        try:
            trades = ib.openTrades()
        except Exception as exc:
            raise BrokerResponseError(f"could not read open orders: {exc}") from exc
        for trade in trades or []:
            order = getattr(trade, "order", None)
            status = getattr(trade, "orderStatus", None)
            for candidate in (
                getattr(order, "permId", None),
                getattr(status, "permId", None),
                getattr(order, "orderId", None),
                getattr(status, "orderId", None),
            ):
                if candidate and str(candidate) == str(broker_order_id):
                    return trade
        return None
