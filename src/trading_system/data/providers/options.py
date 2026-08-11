"""Option data providers.

Option data is the point of this project, so it is worth being precise about
what is and is not available for free today.

**Implemented.** Chain metadata from IBKR: the expirations, strikes, exchange,
multiplier and — importantly — the option ``trading_class``, preserved exactly
as the broker reports it. For SPY that is ``2SPY``, not ``SPY``.

**Deferred, with a reason.** Per-contract quotes, open interest and Greeks from
IBKR. Not an oversight and not laziness: the Milestone 2 broker interface
quotes an instrument by symbol, and a specific option contract needs strike,
expiry and right. Extending it is the easy part — the blocker is that every
uncached contract quote needs its own connection under the
one-reliable-round-trip constraint, so a 500-strike chain would mean 500
connections. That needs a batching design against a real gateway, which
belongs with the contract selector in Milestone 6.

The interface, the canonical models, the storage path and the quality checks
for option quotes all exist and are exercised end to end by
:class:`SimulatedOptionsDataProvider`, so a later provider drops in without a
consumer change.
"""

from __future__ import annotations

from abc import abstractmethod
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
from trading_system.data.normalizers.broker import option_chain_from_broker
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
from trading_system.domain.models import OptionChainSnapshot
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
    ) -> ProviderResult[OptionQuote]:
        """Retrieve per-contract quotes.

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
    notes = "Chain metadata only; per-contract quotes and Greeks are deferred to Milestone 6."

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
        return frozenset({DataType.OPTION_CHAIN})

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


def _chain(broker: Broker, underlying: str) -> OptionChainSnapshot:
    """The single broker operation an option-chain session is allowed to run."""
    return broker.get_option_chain(underlying)


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
            for contract in self._contracts(key, snapshot, expiration=chosen)
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
    ) -> list[OptionContract]:
        from trading_system.broker.simulator.market import simulated_reference_price

        expirations = [expiration] if expiration else snapshot.expirations[:1]
        reference = simulated_reference_price(underlying)
        strikes = self._near_the_money(snapshot.strikes, reference)
        contracts: list[OptionContract] = []
        for expiry in expirations:
            for strike in strikes:
                for right in (OptionRight.CALL, OptionRight.PUT):
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
