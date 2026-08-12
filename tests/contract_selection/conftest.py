"""Fixtures for the contract-selection suites.

Three rules hold across every test here:

* **No network, no broker, no model.** The selector is constructed with a
  repository and configuration and nothing else. There is no seam through which
  a test could reach an API even by accident.
* **No writing into the repository's own ``data/``.** Every store is rooted at
  ``tmp_path``.
* **Chains and quotes go through the real repository.** These tests persist
  option chains and per-contract quotes the way the collector would, so the
  point-in-time rules, the hashing and the ledger are all exercised. A selector
  that only works against a hand-built object has not been tested against the
  thing it will actually see.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_system.data.models import (
    DataQualityReport,
    DataRecord,
    DataSourceMetadata,
    MarketQuote,
    OptionChain,
    OptionContract,
    OptionQuote,
)
from trading_system.data.repository import FilesystemDataRepository, build_snapshot
from trading_system.domain.enums import (
    ConfidenceLevel,
    DataType,
    DecisionMethod,
    MarketDataOrigin,
    MarketHypothesis,
    OptionRight,
    SecurityType,
    SourceTier,
    StrategyAction,
    StrategySelectionReason,
    StrategySelectionStatus,
    StrategyType,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import SystemConfig
from trading_system.strategies.contract_selector import ContractSelector, SelectionContext
from trading_system.strategies.models import StrategyDecisionRecord
from trading_system.strategies.registry import StrategyRegistry, StrategySpecification

#: A weekday inside the market calendar's covered years, during US hours.
SELECTION_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

#: Expirations either side of the 14-30 day window, all weekdays:
#: 11, 18, 25 and 39 days out respectively.
TOO_NEAR = date(2026, 8, 21)
NEAR_TARGET = date(2026, 8, 28)
FURTHER = date(2026, 9, 4)
TOO_FAR = date(2026, 9, 18)

#: A strike ladder around a reference price of 180.
STRIKES = [Decimal(str(value)) for value in (165, 170, 175, 180, 185, 190, 195)]

REFERENCE = Decimal("180.00")


@pytest.fixture
def selection_now() -> datetime:
    return SELECTION_NOW


@pytest.fixture
def selection_clock() -> FixedClock:
    return FixedClock(SELECTION_NOW)


@pytest.fixture
def data_repo(tmp_path: Path, selection_clock: FixedClock) -> FilesystemDataRepository:
    return FilesystemDataRepository(tmp_path / "data", clock=selection_clock)


@pytest.fixture
def registry(system_config: SystemConfig) -> StrategyRegistry:
    return StrategyRegistry.from_config(system_config)


# ---------------------------------------------------------------------------
# Storing option data through the real repository
# ---------------------------------------------------------------------------
def _metadata(
    *,
    retrieved_at: datetime,
    as_of: datetime,
    identifier: str,
    origin: MarketDataOrigin = MarketDataOrigin.BROKER_DELAYED,
) -> DataSourceMetadata:
    return DataSourceMetadata(
        provider="IBKR",
        source_name="IBKR",
        source_tier=SourceTier.TIER_1,
        origin=origin,
        retrieved_at=retrieved_at,
        source_timestamp=as_of,
        observed_at=as_of,
        source_identifier=identifier,
    )


def _store(
    repo: FilesystemDataRepository,
    *,
    data_type: DataType,
    key: str,
    records: Sequence[DataRecord],
    as_of: datetime,
    retrieved_at: datetime,
) -> str:
    snapshot = build_snapshot(
        data_type=data_type,
        key=key.upper(),
        records=records,
        provider="IBKR",
        source_tier=SourceTier.TIER_1,
        origin=MarketDataOrigin.BROKER_DELAYED,
        as_of=as_of,
        retrieved_at=retrieved_at,
        quality=DataQualityReport(evaluated_at=retrieved_at),
    )
    repo.save_snapshot(snapshot)
    return snapshot.snapshot_id


def _contract_id(symbol: str, expiration: date, strike: Decimal, right: OptionRight) -> int:
    from trading_system.data.hashing import stable_hash

    digest = stable_hash([symbol, expiration.isoformat(), str(strike), right.value])
    return int(digest[:8], 16)


@pytest.fixture
def store_underlying_quote(data_repo: FilesystemDataRepository) -> Callable[..., MarketQuote]:
    """The underlying quote every strike policy measures against."""

    def _make(
        symbol: str = "NVDA",
        *,
        as_of: datetime = SELECTION_NOW,
        retrieved_at: datetime | None = None,
        last: Decimal | None = REFERENCE,
        close: Decimal | None = Decimal("179.10"),
    ) -> MarketQuote:
        retrieved = retrieved_at or as_of
        quote = MarketQuote(
            as_of=as_of,
            source=_metadata(retrieved_at=retrieved, as_of=as_of, identifier=f"ibkr:{symbol}"),
            symbol=symbol.upper(),
            security_type=SecurityType.STOCK,
            currency="USD",
            last=last,
            close=close,
            volume=Decimal("240000000"),
            quality=DataQualityReport(evaluated_at=retrieved),
        )
        _store(
            data_repo,
            data_type=DataType.MARKET_QUOTE,
            key=symbol,
            records=[quote],
            as_of=as_of,
            retrieved_at=retrieved,
        )
        return quote

    return _make


@pytest.fixture
def store_chain(data_repo: FilesystemDataRepository) -> Callable[..., OptionChain]:
    """An option chain, with or without enumerated contracts."""

    def _make(
        symbol: str = "NVDA",
        *,
        as_of: datetime = SELECTION_NOW,
        retrieved_at: datetime | None = None,
        expirations: Sequence[date] | None = None,
        strikes: Sequence[Decimal] | None = None,
        trading_class: str | None = "NVDA",
        multiplier: int | None = 100,
        with_contracts: bool = False,
    ) -> OptionChain:
        retrieved = retrieved_at or as_of
        # `is None`, not falsiness: an explicitly empty chain is a case worth
        # testing, and `[] or default` would silently turn it into a full one.
        chosen_expirations = list(
            [TOO_NEAR, NEAR_TARGET, FURTHER, TOO_FAR] if expirations is None else expirations
        )
        chosen_strikes = list(STRIKES if strikes is None else strikes)
        contracts: list[OptionContract] = []
        if with_contracts:
            contracts = [
                OptionContract(
                    underlying=symbol.upper(),
                    symbol=symbol.upper(),
                    expiration=expiration,
                    strike=strike,
                    right=right,
                    contract_id=_contract_id(symbol.upper(), expiration, strike, right),
                    exchange="SMART",
                    currency="USD",
                    multiplier=multiplier,
                    trading_class=trading_class,
                )
                for expiration in chosen_expirations
                for strike in chosen_strikes
                for right in (OptionRight.CALL, OptionRight.PUT)
            ]
        chain = OptionChain(
            as_of=as_of,
            source=_metadata(
                retrieved_at=retrieved, as_of=as_of, identifier=f"ibkr:chain:{symbol}"
            ),
            underlying=symbol.upper(),
            exchange="SMART",
            trading_class=trading_class,
            multiplier=multiplier,
            expirations=sorted(chosen_expirations),
            strikes=sorted(chosen_strikes),
            rights=[OptionRight.CALL, OptionRight.PUT],
            contracts=contracts,
        )
        _store(
            data_repo,
            data_type=DataType.OPTION_CHAIN,
            key=symbol,
            records=[chain],
            as_of=as_of,
            retrieved_at=retrieved,
        )
        return chain

    return _make


def _delta(strike: Decimal, right: OptionRight, reference: Decimal) -> Decimal:
    """A monotone, deterministic stand-in for a real delta.

    Not a pricing model and not presented as one: the tests need a delta that
    moves the right way with moneyness so a target-delta policy has something
    to choose between, and a fixture that pretended to be Black-Scholes would
    invite someone to trust it.
    """
    moneyness = (reference - strike) / reference
    magnitude = min(Decimal("0.95"), max(Decimal("0.05"), Decimal("0.5") + moneyness * 4))
    return magnitude if right is OptionRight.CALL else -(Decimal(1) - magnitude)


@pytest.fixture
def store_quote_records(
    data_repo: FilesystemDataRepository,
) -> Callable[..., list[OptionQuote]]:
    """Persist a hand-assembled list of quotes as one snapshot.

    One snapshot per key per instant is what the collector actually writes, so
    a test that needs several groups of quotes visible together has to combine
    them rather than storing twice — the second store would simply not be the
    one a point-in-time read returns.
    """

    def _make(
        records: Sequence[OptionQuote],
        *,
        symbol: str = "NVDA",
        as_of: datetime = SELECTION_NOW,
        retrieved_at: datetime | None = None,
    ) -> list[OptionQuote]:
        _store(
            data_repo,
            data_type=DataType.OPTION_QUOTE,
            key=symbol,
            records=records,
            as_of=as_of,
            retrieved_at=retrieved_at or as_of,
        )
        return list(records)

    return _make


def _build_quotes(
    symbol: str = "NVDA",
    *,
    as_of: datetime = SELECTION_NOW,
    retrieved_at: datetime | None = None,
    expirations: Sequence[date] | None = None,
    strikes: Sequence[Decimal] | None = None,
    rights: Sequence[OptionRight] = (OptionRight.CALL, OptionRight.PUT),
    reference: Decimal = REFERENCE,
    trading_class: str | None = "NVDA",
    multiplier: int | None = 100,
    contract_id: int | object | None = ...,
    bid: Decimal | object | None = ...,
    ask: Decimal | object | None = ...,
    delta: Decimal | object | None = ...,
    implied_volatility: Decimal | None = Decimal("0.35"),
    volume: Decimal | None = Decimal("1200"),
    open_interest: Decimal | None = Decimal("8400"),
    research_usable: bool = True,
    underlying_price: Decimal | None = None,
) -> list[OptionQuote]:
    """Per-contract quotes, with every field a strategy might require.

    ``...`` means "derive a plausible value"; ``None`` means the data does not
    carry the field, which is a different and equally important case.
    """
    retrieved = retrieved_at or as_of
    quotes: list[OptionQuote] = []
    for expiration in [NEAR_TARGET, FURTHER] if expirations is None else expirations:
        for strike in STRIKES if strikes is None else strikes:
            for right in rights:
                intrinsic = (
                    max(Decimal(0), reference - strike)
                    if right is OptionRight.CALL
                    else max(Decimal(0), strike - reference)
                )
                theoretical = (intrinsic + Decimal("4.00")).quantize(Decimal("0.01"))
                resolved_bid = theoretical - Decimal("0.05") if bid is ... else bid
                resolved_ask = theoretical + Decimal("0.05") if ask is ... else ask
                resolved_delta = _delta(strike, right, reference) if delta is ... else delta
                resolved_id = (
                    _contract_id(symbol.upper(), expiration, strike, right)
                    if contract_id is ...
                    else contract_id
                )
                quotes.append(
                    OptionQuote(
                        as_of=as_of,
                        source=_metadata(
                            retrieved_at=retrieved,
                            as_of=as_of,
                            identifier=f"ibkr:option:{symbol}:{expiration}:{strike}:{right}",
                        ),
                        contract=OptionContract(
                            underlying=symbol.upper(),
                            symbol=symbol.upper(),
                            expiration=expiration,
                            strike=strike,
                            right=right,
                            contract_id=resolved_id,
                            exchange="SMART",
                            currency="USD",
                            multiplier=multiplier,
                            trading_class=trading_class,
                            local_symbol=f"{symbol}{expiration:%y%m%d}"
                            f"{'C' if right is OptionRight.CALL else 'P'}{strike}",
                        ),
                        bid=resolved_bid,
                        ask=resolved_ask,
                        last=theoretical,
                        volume=volume,
                        open_interest=open_interest,
                        implied_volatility=implied_volatility,
                        delta=resolved_delta,
                        underlying_price=underlying_price,
                        quality=DataQualityReport(
                            research_usable=research_usable, evaluated_at=retrieved
                        ),
                    )
                )
    return quotes


@pytest.fixture
def build_option_quotes() -> Callable[..., list[OptionQuote]]:
    """Assemble per-contract quotes without storing them."""
    return _build_quotes


@pytest.fixture
def store_option_quotes(data_repo: FilesystemDataRepository) -> Callable[..., list[OptionQuote]]:
    """Build per-contract quotes and persist them as one snapshot."""

    def _make(symbol: str = "NVDA", **overrides: Any) -> list[OptionQuote]:
        quotes = _build_quotes(symbol, **overrides)
        as_of: datetime = overrides.get("as_of") or SELECTION_NOW
        retrieved: datetime = overrides.get("retrieved_at") or as_of
        _store(
            data_repo,
            data_type=DataType.OPTION_QUOTE,
            key=symbol,
            records=quotes,
            as_of=as_of,
            retrieved_at=retrieved,
        )
        return quotes

    return _make


@pytest.fixture
def priced_chain(
    store_underlying_quote: Callable[..., MarketQuote],
    store_chain: Callable[..., OptionChain],
    store_option_quotes: Callable[..., list[OptionQuote]],
) -> Callable[..., None]:
    """A complete, selectable option market for one underlying."""

    def _make(symbol: str = "NVDA", **overrides: Any) -> None:
        store_underlying_quote(symbol)
        store_chain(symbol)
        store_option_quotes(symbol, **overrides)

    return _make


# ---------------------------------------------------------------------------
# Decisions and the selector
# ---------------------------------------------------------------------------
@pytest.fixture
def make_decision() -> Callable[..., StrategyDecisionRecord]:
    """A stored strategy decision, as the strategy stage would have written it."""

    def _make(
        symbol: str = "NVDA",
        *,
        strategy: StrategyType = StrategyType.LONG_CALL,
        as_of: datetime = SELECTION_NOW,
        action: StrategyAction = StrategyAction.BUY,
        hypothesis: MarketHypothesis = MarketHypothesis.B,
        run_id: str = "strategy-run-test",
    ) -> StrategyDecisionRecord:
        return StrategyDecisionRecord(
            decision_id=f"strategy-{symbol}-test",
            run_id=run_id,
            symbol=symbol.upper(),
            as_of=as_of,
            generated_at=as_of,
            status=StrategySelectionStatus.SUCCESS,
            action=action,
            selected_strategy=strategy if action is StrategyAction.BUY else None,
            strategy_version="1.0.0" if action is StrategyAction.BUY else None,
            decision_method=DecisionMethod.AI_SELECTED,
            confidence=ConfidenceLevel.MEDIUM,
            reasons=[StrategySelectionReason.HYPOTHESIS_MATCH],
            rationale="the hypothesis matches this strategy"
            if action is StrategyAction.BUY
            else None,
            hypothesis=hypothesis,
            research_confidence=ConfidenceLevel.MEDIUM,
            research_horizon_days=21,
            research_report_id="research-001",
            research_run_id="research-run-test",
            eligible_strategies=[strategy],
            versions=SystemVersions(application_version="0.1.0", config_version="test"),
        )

    return _make


@pytest.fixture
def make_selector(
    system_config: SystemConfig,
    data_repo: FilesystemDataRepository,
    selection_clock: FixedClock,
) -> Callable[..., ContractSelector]:
    """A selector wired to the temporary store, with policy overrides."""

    def _make(**policy: object) -> ContractSelector:
        config = system_config
        if policy:
            updated = system_config.contract_selection.model_copy(update=policy)
            config = system_config.model_copy(update={"contract_selection": updated})
        return ContractSelector(data_repo, config=config, clock=selection_clock)

    return _make


@pytest.fixture
def select(
    make_selector: Callable[..., ContractSelector],
    make_decision: Callable[..., StrategyDecisionRecord],
    registry: StrategyRegistry,
) -> Callable[..., object]:
    """Run one selection end to end, with sensible defaults."""

    def _run(
        *,
        strategy: StrategyType = StrategyType.LONG_CALL,
        symbol: str = "NVDA",
        as_of: datetime = SELECTION_NOW,
        event_time: datetime | None = None,
        specification: StrategySpecification | None = None,
        selector: ContractSelector | None = None,
        run_id: str = "contract-run-test",
    ):
        decision = make_decision(symbol, strategy=strategy, as_of=as_of)
        chosen = selector or make_selector()
        return chosen.select(
            SelectionContext(
                run_id=run_id,
                decision=decision,
                specification=specification or registry.require(strategy),
                as_of=as_of,
                event_time=event_time,
                strategy_run_id=decision.run_id,
            )
        )

    return _run


@pytest.fixture
def days_later() -> Callable[[int], datetime]:
    def _make(days: int) -> datetime:
        return SELECTION_NOW + timedelta(days=days)

    return _make
