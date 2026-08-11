"""Fixtures for the data layer suites.

Two rules hold across every test here:

* **No network, no broker.** Providers that would reach outside are given a
  recorded fetcher or a fake broker. The gateway-backed tests live in
  ``test_ibkr_paper.py`` and are marked ``ibkr``.
* **No writing into the repository's own ``data/``.** Every store is rooted at
  ``tmp_path``, so a test run leaves no artifact behind and cannot be
  influenced by one left by a previous run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.data.models import (
    DataSourceMetadata,
    MarketQuote,
    OptionChain,
    OptionContract,
    OptionQuote,
)
from trading_system.data.quality import QualityEngine
from trading_system.data.repository import FilesystemDataRepository
from trading_system.domain.enums import (
    MarketDataOrigin,
    OptionRight,
    SecurityType,
    SourceTier,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import DataConfig, SystemConfig

#: A weekday inside the calendar's covered year, during US market hours.
DATA_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


@pytest.fixture
def data_now() -> datetime:
    return DATA_NOW


@pytest.fixture
def data_clock() -> FixedClock:
    return FixedClock(DATA_NOW)


@pytest.fixture
def data_config(system_config: SystemConfig) -> DataConfig:
    """The real ``config/data.yaml``.

    Tests run against the shipped policy on purpose: a quality rule that only
    holds under test-only thresholds has not been tested.
    """
    return system_config.data


@pytest.fixture
def quality_engine(data_config: DataConfig, data_clock: FixedClock) -> QualityEngine:
    return QualityEngine(data_config, clock=data_clock)


@pytest.fixture
def repository(tmp_path: Path, data_clock: FixedClock) -> FilesystemDataRepository:
    return FilesystemDataRepository(tmp_path / "data", clock=data_clock)


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------
@pytest.fixture
def make_source() -> Callable[..., DataSourceMetadata]:
    """Build provenance, defaulting to a fresh, well-formed broker quote."""

    def _make(
        *,
        provider: str = "IBKR",
        tier: SourceTier = SourceTier.TIER_1,
        origin: MarketDataOrigin = MarketDataOrigin.BROKER_DELAYED,
        retrieved_at: datetime = DATA_NOW,
        source_timestamp: datetime | None = DATA_NOW,
        published_at: datetime | None = None,
        effective_at: datetime | None = None,
        observed_at: datetime | None = None,
        source_identifier: str | None = "ibkr:SPY",
        requested_provider: str | None = None,
        field_provenance: dict[str, str] | None = None,
    ) -> DataSourceMetadata:
        return DataSourceMetadata(
            provider=provider,
            source_name=provider,
            source_tier=tier,
            origin=origin,
            retrieved_at=retrieved_at,
            source_timestamp=source_timestamp,
            published_at=published_at,
            effective_at=effective_at,
            observed_at=observed_at or source_timestamp,
            source_identifier=source_identifier,
            requested_provider=requested_provider,
            field_provenance=field_provenance or {},
        )

    return _make


@pytest.fixture
def make_quote(make_source: Callable[..., DataSourceMetadata]) -> Callable[..., MarketQuote]:
    """Build a market quote. Every field is overridable, defaults are clean."""

    def _make(
        *,
        symbol: str = "SPY",
        as_of: datetime = DATA_NOW,
        bid: Decimal | None = Decimal("500.10"),
        ask: Decimal | None = Decimal("500.20"),
        last: Decimal | None = Decimal("500.15"),
        close: Decimal | None = Decimal("499.80"),
        volume: Decimal | None = Decimal("75000000"),
        high: Decimal | None = None,
        low: Decimal | None = None,
        security_type: SecurityType = SecurityType.STOCK,
        source: DataSourceMetadata | None = None,
        **source_kwargs: object,
    ) -> MarketQuote:
        return MarketQuote(
            as_of=as_of,
            source=source or make_source(**source_kwargs),
            symbol=symbol,
            security_type=security_type,
            currency="USD",
            contract_id=756733,
            bid=bid,
            ask=ask,
            last=last,
            close=close,
            volume=volume,
            high=high,
            low=low,
        )

    return _make


#: A real IBKR quirk, and the regression this suite exists to protect:
#: SPY *options* trade under class "2SPY" while the SPY *underlying* uses
#: "SPY". Anything that reconstructs one from the other builds a contract the
#: broker does not recognise.
SPY_OPTION_TRADING_CLASS = "2SPY"


@pytest.fixture
def spy_option_contract() -> OptionContract:
    return OptionContract(
        underlying="SPY",
        symbol="SPY",
        security_type=SecurityType.OPTION,
        expiration=date(2026, 9, 18),
        strike=Decimal("500"),
        right=OptionRight.CALL,
        contract_id=778899,
        exchange="SMART",
        primary_exchange="ARCA",
        currency="USD",
        multiplier=100,
        trading_class=SPY_OPTION_TRADING_CLASS,
        local_symbol="SPY   260918C00500000",
    )


@pytest.fixture
def make_option_quote(
    make_source: Callable[..., DataSourceMetadata],
    spy_option_contract: OptionContract,
) -> Callable[..., OptionQuote]:
    def _make(
        *,
        contract: OptionContract | None = None,
        as_of: datetime = DATA_NOW,
        bid: Decimal | None = Decimal("12.10"),
        ask: Decimal | None = Decimal("12.40"),
        last: Decimal | None = Decimal("12.25"),
        close: Decimal | None = Decimal("12.00"),
        volume: Decimal | None = Decimal("4200"),
        open_interest: Decimal | None = Decimal("15000"),
        implied_volatility: Decimal | None = Decimal("0.18"),
        delta: Decimal | None = Decimal("0.55"),
        source: DataSourceMetadata | None = None,
        **source_kwargs: object,
    ) -> OptionQuote:
        return OptionQuote(
            as_of=as_of,
            source=source or make_source(**source_kwargs),
            contract=contract or spy_option_contract,
            bid=bid,
            ask=ask,
            last=last,
            close=close,
            volume=volume,
            open_interest=open_interest,
            implied_volatility=implied_volatility,
            delta=delta,
            gamma=Decimal("0.01"),
            theta=Decimal("-0.08"),
            vega=Decimal("0.30"),
        )

    return _make


@pytest.fixture
def make_chain(
    make_source: Callable[..., DataSourceMetadata],
    spy_option_contract: OptionContract,
) -> Callable[..., OptionChain]:
    def _make(
        *,
        underlying: str = "SPY",
        as_of: datetime = DATA_NOW,
        with_contracts: bool = False,
        source: DataSourceMetadata | None = None,
        **source_kwargs: object,
    ) -> OptionChain:
        return OptionChain(
            as_of=as_of,
            source=source or make_source(**source_kwargs),
            underlying=underlying,
            underlying_contract_id=756733,
            exchange="SMART",
            trading_class=SPY_OPTION_TRADING_CLASS,
            multiplier=100,
            expirations=[date(2026, 8, 21), date(2026, 9, 18)],
            strikes=[Decimal("495"), Decimal("500"), Decimal("505")],
            rights=[OptionRight.CALL, OptionRight.PUT],
            contracts=[spy_option_contract] if with_contracts else [],
        )

    return _make


# ---------------------------------------------------------------------------
# Recorded provider payloads
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def data_fixture_dir(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures" / "data"


@pytest.fixture(scope="session")
def load_data_fixture(data_fixture_dir: Path) -> Callable[[str], str]:
    """Return the raw text of a recorded provider response.

    Text rather than parsed JSON: the provider under test is responsible for
    parsing, and handing it a pre-parsed object would skip the code that
    matters.
    """

    def _load(name: str) -> str:
        return (data_fixture_dir / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def news_fixture_dir(tmp_path: Path, data_fixture_dir: Path) -> Path:
    """A writable copy of the recorded news fixtures."""
    destination = tmp_path / "news"
    destination.mkdir(parents=True, exist_ok=True)
    for path in (data_fixture_dir / "news").glob("*.json"):
        (destination / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return destination


@pytest.fixture
def events_fixture_dir(tmp_path: Path, data_fixture_dir: Path) -> Path:
    destination = tmp_path / "events"
    destination.mkdir(parents=True, exist_ok=True)
    for path in (data_fixture_dir / "events").glob("*.json"):
        (destination / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return destination


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
