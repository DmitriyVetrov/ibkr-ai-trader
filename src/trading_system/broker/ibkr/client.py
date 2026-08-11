"""IBKR broker adapter.

This is the only module in the system that imports ``ib_async``. Everything it
returns is a domain model, so no IBKR type escapes this package.

Safety properties, in order of how much they matter:

* **LIVE is refused.** Milestone 2 operates in DRY_RUN and PAPER only.
* **The API connection is opened read-only.** ``IB.connect(readonly=True)``
  makes IBKR itself reject order placement, so a bug here cannot trade — the
  guarantee does not depend on our own code being correct.
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
from typing import TYPE_CHECKING, Any

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
from trading_system.broker.ibkr.orders import to_broker_orders
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
    MarketDataSnapshot,
    OptionChainSnapshot,
)
from trading_system.infrastructure.clock import Clock, SystemClock

if TYPE_CHECKING:  # pragma: no cover - typing only
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
        market_data_type: int = 3,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(trading_mode=trading_mode, read_only=read_only)
        self._validate_configuration(host, port, client_id, trading_mode, read_only)
        self._host = host
        self._port = port
        self._client_id = client_id
        self._configured_account = account or None
        self._connect_timeout = connect_timeout_seconds
        self._market_data_type = market_data_type
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
        if not read_only:
            raise BrokerConfigurationError(
                "IBKRBroker refuses a writable connection in Milestone 2. Order "
                "execution is delivered in Milestone 8."
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

        try:
            ib.connect(
                host=self._host,
                port=self._port,
                clientId=self._client_id,
                timeout=self._connect_timeout,
                # IBKR itself blocks order placement on this connection.
                readonly=True,
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
        return self.health_check()

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

    def health_check(self) -> BrokerHealth:
        """Report connection state. Never raises."""
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
        try:
            started = time.perf_counter()
            # ib_async's synchronous IB.reqCurrentTime() has no timeout and
            # can hang indefinitely on a repeat call over the same
            # connection (observed against a real TWS/Gateway: the first
            # call on a fresh connection returns, a later call never does).
            # Where the async form is available, route through it with an
            # explicit bound so a broken round trip surfaces as ERROR
            # instead of hanging the whole process.
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

    # --- read-only state ---------------------------------------------------
    def get_account_summary(self) -> dict[str, str]:
        """Raw tag/value pairs exactly as IBKR reported them."""
        ib = self._require_connection()
        try:
            values = ib.accountSummary(self._account_id or "")
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
            tickers = ib.reqTickers(qualified[0])
        except MarketDataUnavailableError:
            raise
        except Exception as exc:
            raise MarketDataUnavailableError(
                f"MARKET_DATA_UNAVAILABLE: IBKR rejected the market data request for "
                f"{symbol.upper()}: {exc}"
            ) from exc

        if not tickers:
            raise MarketDataUnavailableError(
                f"MARKET_DATA_UNAVAILABLE: IBKR returned no ticker for {symbol.upper()}"
            )

        self._last_communication = self._clock.now()
        snapshot = to_market_data_snapshot(
            tickers[0], self._clock.now(), fallback_symbol=symbol.upper()
        )
        if not snapshot.has_quote:
            raise MarketDataUnavailableError(
                f"MARKET_DATA_UNAVAILABLE: no usable price for {symbol.upper()}. "
                f"A market data subscription may be required."
            )
        return snapshot

    def get_option_chain(self, underlying: str) -> OptionChainSnapshot:
        ib = self._require_connection()
        contract = self.get_contract(underlying, SecurityType.STOCK)
        if contract.contract_id is None:
            raise OptionChainUnavailableError(
                f"cannot request a chain for {underlying.upper()}: no contract id"
            )

        try:
            chains = ib.reqSecDefOptParams(underlying.upper(), "", "STK", contract.contract_id)
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
