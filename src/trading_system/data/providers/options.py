"""Option data providers.

Option data is the point of this project, so it is worth being precise about
what is and is not available for free today.

**Chain metadata from IBKR**: the expirations, strikes, exchange, multiplier
and — importantly — the option ``trading_class``, preserved exactly as the
broker reports it. For SPY that is ``2SPY``, not ``SPY``.

**Per-contract quotes, open interest and Greeks from IBKR**, for an
*explicitly named* strike list. The earlier deferral assumed one connection per
contract under the one-reliable-round-trip constraint, which would have meant
500 connections for a 500-strike chain. Measured against the validated paper
gateway (10.45, ``ib_async`` 2.1.0) on 2026-08-20, that assumption was wrong in
a useful direction: one batched ``qualifyContracts`` followed by one
subscription per contract answered **eight contracts in 2.4 s on a single
connection**, which is the same two-step shape ``get_market_data`` already
uses. What remains true is that the *chain* and the *underlying price* must not
be fetched on that same connection, which is why the strike list is an argument
rather than something the provider derives.

Three things the measurement settled, each of which shapes the code:

* **The delayed feed reports an unavailable option bid and ask as ``-1``.**
  Not ``NaN``, not the ``DBL_MAX`` sentinel — a plain negative number that
  ``to_decimal`` cannot drop. The broker adapter rejects it; see
  :func:`~trading_system.broker.ibkr.market_data.to_option_quote_snapshot`.
  Without that, ``price_source: ASK_DEBIT`` would read ``-1`` as what a
  contract costs.
* **A closed market keeps its two-sided quote under ``marketDataType=4``.**
  The same contract that reports ``bid=-1 ask=-1`` delayed reports
  ``bid=10.37 ask=11.14`` delayed-frozen, and ``origin`` records which
  answered. Neither is substituted for the other.
* **Open interest needs generic tick 101** and is otherwise never sent, which
  matters because ``risk.yaml`` states a ``min_open_interest`` floor.

The canonical models, the storage path and the quality checks are shared with
:class:`SimulatedOptionsDataProvider`, which remains the offline path.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from trading_system.broker.base import Broker
from trading_system.data.hashing import payload_hash
from trading_system.data.models import (
    OptionChain,
    OptionContract,
    OptionQuote,
    RawRecord,
)
from trading_system.data.normalizers.broker import (
    option_chain_from_broker,
    option_quote_from_broker,
)
from trading_system.data.providers.base import (
    DataProvider,
    ProviderAvailability,
    ProviderCost,
    ProviderResult,
    ProviderUnavailableError,
    failed_result,
    successful_result,
)
from trading_system.data.providers.broker_session import BrokerSession
from trading_system.domain.enums import (
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    OptionRight,
    SecurityType,
    SourceTier,
)
from trading_system.domain.models import OptionChainSnapshot, OptionQuoteSnapshot
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = [
    "IBKROptionsDataProvider",
    "OptionsDataProvider",
    "SimulatedOptionsDataProvider",
]


class OptionsDataProvider(DataProvider):
    """Interface for option chain and option quote retrieval."""

    @property
    def data_types(self) -> frozenset[DataType]:
        return frozenset({DataType.OPTION_CHAIN, DataType.OPTION_QUOTE})

    @abstractmethod
    def fetch_chain(self, underlying: str) -> ProviderResult[OptionChain]:
        """Retrieve chain metadata for one underlying.

        Retrieval only. Choosing a strike or an expiry is the deterministic
        contract selector's job in Milestone 6, and no provider may pre-empt
        it by returning a filtered chain.
        """

    def fetch_option_quotes(
        self,
        underlying: str,
        *,
        expiration: date | None = None,
        strikes: Sequence[Decimal] | None = None,
        rights: Sequence[OptionRight] | None = None,
        trading_class: str | None = None,
    ) -> ProviderResult[OptionQuote]:
        """Retrieve per-contract quotes.

        ``strikes`` names the contracts to quote. A broker-backed provider
        cannot work them out for itself without reading the chain and the
        underlying's price first, which is two more round trips on a connection
        that reliably answers one — so the caller, which already holds a stored
        chain, supplies them.

        The default reports ``NO_DATA``. A provider without contract-level
        quotes says so rather than deriving Greeks it did not receive.
        """
        return failed_result(
            provider_id=self.provider_id,
            data_type=DataType.OPTION_QUOTE,
            key=underlying.upper(),
            outcome=CollectionOutcome.NO_DATA,
            error=f"{self.provider_id} does not supply per-contract option quotes",
        )


class IBKROptionsDataProvider(OptionsDataProvider):
    """Option chain metadata from IBKR, through the Milestone 2 broker adapter."""

    provider_id = "IBKR"
    display_name = "Interactive Brokers"
    tier = SourceTier.TIER_1
    cost = ProviderCost.FREE_WITH_ACCOUNT
    origin = MarketDataOrigin.BROKER_REALTIME
    requires_broker = True
    requires_network = True
    notes = "Chain metadata, and per-contract quotes for an explicitly named strike list."

    def __init__(
        self,
        session: BrokerSession,
        *,
        clock: Clock | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self._session = session
        self._clock = clock or SystemClock()

    @property
    def data_types(self) -> frozenset[DataType]:
        return frozenset({DataType.OPTION_CHAIN, DataType.OPTION_QUOTE})

    def availability(self) -> ProviderAvailability:
        return (
            ProviderAvailability.AVAILABLE
            if self._session.probe()
            else ProviderAvailability.UNAVAILABLE
        )

    def fetch_chain(self, underlying: str) -> ProviderResult[OptionChain]:
        key = underlying.upper()
        try:
            snapshot = self._session.fetch(
                lambda broker: _chain(broker, key),
                description=f"option chain for {key}",
            )
        except ProviderUnavailableError as exc:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.OPTION_CHAIN,
                key=key,
                outcome=CollectionOutcome.PROVIDER_UNAVAILABLE,
                error=str(exc),
            )
        except Exception as exc:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.OPTION_CHAIN,
                key=key,
                outcome=CollectionOutcome.INVALID_DATA,
                error=str(exc),
            )

        retrieved_at = self._clock.now()
        payload = snapshot.model_dump(mode="json")
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.OPTION_CHAIN,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            source_timestamp=snapshot.as_of,
            request={"underlying": key},
            notes=["payload is the broker adapter's chain snapshot; ib_async objects never leave"],
        )
        chain = option_chain_from_broker(
            snapshot,
            source=self.metadata(
                retrieved_at=retrieved_at,
                origin=snapshot.origin,
                source_identifier=f"ibkr:chain:{key}",
                source_timestamp=snapshot.as_of,
                observed_at=snapshot.as_of,
            ),
        )
        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.OPTION_CHAIN,
            key=key,
            records=[chain],
            raw=raw,
        )

    def fetch_option_quotes(
        self,
        underlying: str,
        *,
        expiration: date | None = None,
        strikes: Sequence[Decimal] | None = None,
        rights: Sequence[OptionRight] | None = None,
        trading_class: str | None = None,
    ) -> ProviderResult[OptionQuote]:
        """Per-contract quotes and Greeks, through the Milestone 2 adapter.

        ``expiration`` and ``strikes`` are **required**, and the refusal when
        they are missing is deliberate rather than a convenience gap. Working
        them out means reading the chain and the underlying's price, and this
        session gets one connection whose second uncached round trip may never
        be answered. The caller has both stored already.

        What the operator's ``IBKR_MARKET_DATA_TYPE`` decides is visible in the
        result rather than compensated for here. On the delayed feed with the
        market closed IBKR sends the bid and ask ticks carrying ``-1``, the
        adapter drops them, and the quotes arrive one-sided — priced by
        ``last`` alone, which is not a cost. Delayed-frozen (4) serves the last
        two-sided quote of the session instead. Neither is substituted for the
        other, and ``origin`` records which answered.
        """
        key = underlying.upper()
        if expiration is None or not strikes:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.OPTION_QUOTE,
                key=key,
                outcome=CollectionOutcome.NO_DATA,
                error=(
                    f"option quotes for {key} need an explicit expiration and strike list; "
                    f"deriving them would cost two more round trips on a connection that "
                    f"reliably answers one. Collect the option chain first."
                ),
            )

        wanted_strikes = sorted({Decimal(str(s)) for s in strikes})
        wanted_rights = list(rights) if rights else [OptionRight.CALL, OptionRight.PUT]
        try:
            snapshots = self._session.fetch(
                lambda broker: _quotes(
                    broker,
                    key,
                    expiration,
                    wanted_strikes,
                    rights=wanted_rights,
                    trading_class=trading_class,
                ),
                description=f"option quotes for {key} {expiration.isoformat()}",
            )
        except ProviderUnavailableError as exc:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.OPTION_QUOTE,
                key=key,
                outcome=CollectionOutcome.PROVIDER_UNAVAILABLE,
                error=str(exc),
            )
        except Exception as exc:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.OPTION_QUOTE,
                key=key,
                outcome=CollectionOutcome.INVALID_DATA,
                error=str(exc),
            )

        if not snapshots:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.OPTION_QUOTE,
                key=key,
                outcome=CollectionOutcome.NO_DATA,
                error=(
                    f"IBKR resolved no contracts for {key} expiring "
                    f"{expiration.isoformat()} at the requested strikes"
                ),
            )

        retrieved_at = self._clock.now()
        payload = [snapshot.model_dump(mode="json") for snapshot in snapshots]
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.OPTION_QUOTE,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            source_timestamp=snapshots[0].as_of,
            request={
                "underlying": key,
                "expiration": expiration.isoformat(),
                # Flattened to strings because `request` is a str->str map, and
                # recorded in full rather than as a count: reproducing a stored
                # snapshot means knowing exactly which contracts were asked for.
                "strikes": ",".join(str(strike) for strike in wanted_strikes),
                "rights": ",".join(right.value for right in wanted_rights),
                "trading_class": trading_class or "",
            },
            notes=["payload is the broker adapter's quote snapshots; ib_async objects never leave"],
        )
        quotes = [
            option_quote_from_broker(
                snapshot,
                source=self.metadata(
                    retrieved_at=retrieved_at,
                    origin=snapshot.origin,
                    source_identifier=(
                        f"ibkr:option:{snapshot.contract.local_symbol or snapshot.contract.symbol}"
                    ),
                    source_timestamp=snapshot.as_of,
                    observed_at=snapshot.as_of,
                ),
                underlying=key,
            )
            for snapshot in snapshots
        ]
        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.OPTION_QUOTE,
            key=key,
            records=quotes,
            raw=raw,
        )


def _chain(broker: Broker, underlying: str) -> OptionChainSnapshot:
    """The single broker operation an option-chain session is allowed to run."""
    return broker.get_option_chain(underlying)


def _quotes(
    broker: Broker,
    underlying: str,
    expiration: date,
    strikes: Sequence[Decimal],
    *,
    rights: Sequence[OptionRight],
    trading_class: str | None,
) -> list[OptionQuoteSnapshot]:
    """The single broker operation an option-quote session is allowed to run."""
    return broker.get_option_quotes(
        underlying,
        expiration,
        strikes,
        rights=rights,
        trading_class=trading_class,
    )


class SimulatedOptionsDataProvider(OptionsDataProvider):
    """Deterministic offline chains and option quotes, stamped ``SIMULATED``.

    Its per-contract quotes exist so the option-quote model, the Greeks
    handling, the quality checks and the snapshot path are exercised without a
    gateway. The numbers are synthetic and labelled as such at every layer.
    """

    provider_id = "SIMULATOR"
    display_name = "Deterministic simulator"
    tier = SourceTier.TIER_4
    cost = ProviderCost.FREE
    origin = MarketDataOrigin.SIMULATED
    notes = "Synthetic. Never a substitute for market data."

    #: Strikes either side of the money to quote. Bounded so a simulated
    #: snapshot stays inspectable rather than becoming a wall of noise.
    STRIKE_WINDOW = 3

    def __init__(self, *, clock: Clock | None = None, timeout_seconds: float = 5.0) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self._clock = clock or SystemClock()

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability.AVAILABLE

    def fetch_chain(self, underlying: str) -> ProviderResult[OptionChain]:
        from trading_system.broker.simulator.market import simulated_option_chain

        key = underlying.upper()
        snapshot = simulated_option_chain(key, self._clock)
        retrieved_at = self._clock.now()
        payload = snapshot.model_dump(mode="json")
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.OPTION_CHAIN,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            request={"underlying": key},
            notes=["SIMULATED - not market data"],
        )
        chain = option_chain_from_broker(
            snapshot,
            source=self.metadata(
                retrieved_at=retrieved_at,
                source_identifier=f"simulator:chain:{key}",
                source_timestamp=snapshot.as_of,
                observed_at=snapshot.as_of,
            ),
            contracts=self._contracts(key, snapshot),
        )
        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.OPTION_CHAIN,
            key=key,
            records=[chain],
            raw=raw,
        )

    def fetch_option_quotes(
        self,
        underlying: str,
        *,
        expiration: date | None = None,
        strikes: Sequence[Decimal] | None = None,
        rights: Sequence[OptionRight] | None = None,
        trading_class: str | None = None,
    ) -> ProviderResult[OptionQuote]:
        from trading_system.broker.simulator.market import (
            simulated_option_chain,
            simulated_reference_price,
        )

        key = underlying.upper()
        snapshot = simulated_option_chain(key, self._clock)
        chosen = expiration or (snapshot.expirations[0] if snapshot.expirations else None)
        if chosen is None:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.OPTION_QUOTE,
                key=key,
                outcome=CollectionOutcome.NO_DATA,
                error=f"no expirations available for {key}",
            )

        retrieved_at = self._clock.now()
        reference = simulated_reference_price(key)
        quotes = [
            self._quote(contract, reference, retrieved_at)
            for contract in self._contracts(
                key, snapshot, expiration=chosen, strikes=strikes, rights=rights
            )
        ]
        payload = [quote.model_dump(mode="json") for quote in quotes]
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.OPTION_QUOTE,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            request={"underlying": key, "expiration": chosen.isoformat()},
            notes=["SIMULATED - not market data"],
        )
        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.OPTION_QUOTE,
            key=key,
            records=quotes,
            raw=raw,
        )

    # --- synthetic construction -------------------------------------------
    def _contracts(
        self,
        underlying: str,
        snapshot: OptionChainSnapshot,
        *,
        expiration: date | None = None,
        strikes: Sequence[Decimal] | None = None,
        rights: Sequence[OptionRight] | None = None,
    ) -> list[OptionContract]:
        from trading_system.broker.simulator.market import simulated_reference_price

        expirations = [expiration] if expiration else snapshot.expirations[:1]
        reference = simulated_reference_price(underlying)
        chosen_strikes = (
            sorted({Decimal(str(s)) for s in strikes})
            if strikes
            else self._near_the_money(snapshot.strikes, reference)
        )
        wanted_rights = list(rights) if rights else [OptionRight.CALL, OptionRight.PUT]
        contracts: list[OptionContract] = []
        for expiry in expirations:
            for strike in chosen_strikes:
                for right in wanted_rights:
                    contracts.append(
                        OptionContract(
                            underlying=underlying,
                            symbol=underlying,
                            security_type=SecurityType.OPTION,
                            expiration=expiry,
                            strike=strike,
                            right=right,
                            contract_id=_synthetic_contract_id(underlying, expiry, strike, right),
                            exchange=snapshot.exchange,
                            currency="USD",
                            multiplier=snapshot.multiplier or 100,
                            trading_class=snapshot.trading_class,
                            local_symbol=_occ_symbol(underlying, expiry, strike, right),
                        )
                    )
        return contracts

    def _near_the_money(self, strikes: list[Decimal], reference: Decimal) -> list[Decimal]:
        if not strikes:
            return []
        ordered = sorted(strikes, key=lambda s: abs(s - reference))
        return sorted(ordered[: self.STRIKE_WINDOW * 2 + 1])

    def _quote(
        self,
        contract: OptionContract,
        reference: Decimal,
        retrieved_at: datetime,
    ) -> OptionQuote:
        strike = contract.strike or reference
        intrinsic = (
            max(Decimal("0"), reference - strike)
            if contract.right is OptionRight.CALL
            else max(Decimal("0"), strike - reference)
        )
        # A flat synthetic time value. Deliberately crude: this is scaffolding
        # for the storage and quality paths, not a pricing model, and dressing
        # it up as one would invite someone to trust it.
        theoretical = (intrinsic + reference * Decimal("0.02")).quantize(Decimal("0.01"))
        half_spread = (theoretical * Decimal("0.02")).quantize(Decimal("0.01"))
        return OptionQuote(
            as_of=self._clock.now(),
            source=self.metadata(
                retrieved_at=retrieved_at,
                source_identifier=f"simulator:option:{contract.local_symbol}",
                source_timestamp=self._clock.now(),
                observed_at=self._clock.now(),
            ),
            contract=contract,
            bid=max(Decimal("0.01"), theoretical - half_spread),
            ask=theoretical + half_spread,
            last=theoretical,
            close=theoretical,
            volume=Decimal("250"),
            open_interest=Decimal("1500"),
            implied_volatility=Decimal("0.25"),
            delta=Decimal("0.50") if contract.right is OptionRight.CALL else Decimal("-0.50"),
            gamma=Decimal("0.02"),
            theta=Decimal("-0.05"),
            vega=Decimal("0.12"),
            underlying_price=reference,
        )


def _synthetic_contract_id(
    underlying: str, expiration: date, strike: Decimal, right: OptionRight
) -> int:
    """A stable fake broker id, so simulated chains deduplicate deterministically."""
    from trading_system.data.hashing import stable_hash

    digest = stable_hash([underlying, expiration.isoformat(), str(strike), right.value])
    return int(digest[:8], 16)


def _occ_symbol(underlying: str, expiration: date, strike: Decimal, right: OptionRight) -> str:
    """OCC-style local symbol, matching the shape IBKR reports."""
    strike_thousandths = int((strike * 1000).to_integral_value())
    return (
        f"{underlying:<6}{expiration.strftime('%y%m%d')}"
        f"{'C' if right is OptionRight.CALL else 'P'}{strike_thousandths:08d}"
    )
