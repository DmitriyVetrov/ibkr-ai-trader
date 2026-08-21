"""Which option contracts a collection run asks the broker to quote.

Resolution lives in :class:`DataService` rather than in the provider, because
working the contracts out needs the chain *and* the underlying's price — two
more round trips on a connection that reliably answers one. Both are already
in the store, so the service reads them and hands the provider an explicit
list.

What these tests pin is mostly the refusals. A collection that quietly picked
the wrong expiration would succeed, store a snapshot, and fail contract
selection later with ``REQUIRED_DATA_UNAVAILABLE`` — a diagnosis pointing at
the wrong stage entirely. The shipped default used to do exactly that: the
chain's first expiration is often a day out, while ``contract_selection.yaml``
asks for 21 and ``risk.yaml`` refuses anything under 14.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.data.models import (
    DataQualityReport,
    DataRecord,
    DataSourceMetadata,
    MarketQuote,
    OptionChain,
)
from trading_system.data.repository import FilesystemDataRepository, build_snapshot
from trading_system.data.service import DataService
from trading_system.domain.enums import (
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    SecurityType,
    SourceTier,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 20, 14, 30, tzinfo=UTC)
TODAY = NOW.date()


def _source() -> DataSourceMetadata:
    return DataSourceMetadata(
        provider="SIMULATOR",
        source_name="test",
        source_tier=SourceTier.TIER_4,
        origin=MarketDataOrigin.SIMULATED,
        source_identifier="test",
        retrieved_at=NOW,
        observed_at=NOW,
        source_timestamp=NOW,
    )


def _service(tmp_path: Path, system_config: SystemConfig) -> DataService:
    clock = FixedClock(NOW)
    return DataService(
        settings=Settings(_env_file=None),
        config=system_config,
        clock=clock,
        repository=FilesystemDataRepository(tmp_path / "data", clock=clock),
        simulated=True,
    )


def _save(service: DataService, data_type: DataType, records: list[DataRecord]) -> None:
    """Put canonical records in the store the way the collector would."""
    service.repository.save_snapshot(
        build_snapshot(
            data_type=data_type,
            key="SPY",
            records=records,
            provider="SIMULATOR",
            source_tier=SourceTier.TIER_4,
            origin=MarketDataOrigin.SIMULATED,
            as_of=NOW,
            retrieved_at=NOW,
            quality=DataQualityReport(evaluated_at=NOW),
        )
    )


def _store_chain(service: DataService, expirations: list[date], strikes: list[str]) -> None:
    _save(
        service,
        DataType.OPTION_CHAIN,
        [
            OptionChain(
                as_of=NOW,
                source=_source(),
                underlying="SPY",
                exchange="SMART",
                trading_class="SPY",
                multiplier=100,
                expirations=sorted(expirations),
                strikes=sorted(Decimal(s) for s in strikes),
            )
        ],
    )


def _store_quote(service: DataService, last: str = "200.00") -> None:
    _save(
        service,
        DataType.MARKET_QUOTE,
        [
            MarketQuote(
                as_of=NOW,
                source=_source(),
                symbol="SPY",
                security_type=SecurityType.STOCK,
                last=Decimal(last),
            )
        ],
    )


# ---------------------------------------------------------------------------
# What must be in the store first
# ---------------------------------------------------------------------------
def test_without_a_stored_chain_the_run_says_so_and_asks_for_nothing(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    """A missing chain is NO_DATA naming the fix, not a broker request."""
    service = _service(tmp_path, system_config)

    report = service.collect_option_quotes("SPY")

    assert report.outcome is CollectionOutcome.NO_DATA
    assert report.provider == "NONE"
    assert report.snapshots_created == 0
    assert "no stored option chain" in (report.error or "")


def test_without_a_stored_price_the_strike_band_is_refused_not_assumed(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    """The band is a percentage *around a price*, and there is no default price."""
    service = _service(tmp_path, system_config)
    _store_chain(service, [TODAY + timedelta(days=21)], ["195", "200", "205"])

    report = service.collect_option_quotes("SPY")

    assert report.outcome is CollectionOutcome.NO_DATA
    assert "no stored reference price" in (report.error or "")


# ---------------------------------------------------------------------------
# Expiration resolution
# ---------------------------------------------------------------------------
def test_the_nearest_expiration_to_the_target_is_chosen(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    service = _service(tmp_path, system_config)
    _store_chain(
        service,
        [TODAY + timedelta(days=d) for d in (8, 20, 22, 44)],
        ["195", "200", "205"],
    )
    _store_quote(service)

    report = service.collect_option_quotes("SPY", target_dte=21)

    assert report.succeeded
    assert f"expiration {(TODAY + timedelta(days=20)).isoformat()}" in " ".join(report.notes)


def test_an_expiration_outside_the_window_is_never_reached_for(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    """The DTE window binds even when the chain has something closer to target.

    A DTE-1 quote collected in place of a DTE-21 one would satisfy the
    collector and fail the selector, for reasons visible nowhere.
    """
    service = _service(tmp_path, system_config)
    _store_chain(service, [TODAY + timedelta(days=1), TODAY + timedelta(days=400)], ["200"])
    _store_quote(service)

    report = service.collect_option_quotes("SPY")

    assert report.outcome is CollectionOutcome.NO_DATA
    assert "DTE window" in (report.error or "")
    # The DTEs it did find are named, so the operator can act on it.
    assert "1" in (report.error or "")


def test_an_explicit_expiration_the_chain_does_not_list_is_refused(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    service = _service(tmp_path, system_config)
    _store_chain(service, [TODAY + timedelta(days=21)], ["200"])
    _store_quote(service)

    report = service.collect_option_quotes("SPY", expiration=date(2099, 1, 15))

    assert report.outcome is CollectionOutcome.NO_DATA
    assert "does not list 2099-01-15" in (report.error or "")


def test_an_explicit_expiration_overrides_the_window(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    """An operator naming a date is making a decision, not a suggestion."""
    service = _service(tmp_path, system_config)
    near = TODAY + timedelta(days=2)
    _store_chain(service, [near, TODAY + timedelta(days=21)], ["195", "200", "205"])
    _store_quote(service)

    report = service.collect_option_quotes("SPY", expiration=near)

    assert report.succeeded
    assert f"expiration {near.isoformat()}" in " ".join(report.notes)


# ---------------------------------------------------------------------------
# Strike resolution
# ---------------------------------------------------------------------------
def test_only_strikes_inside_the_configured_band_are_quoted(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    """8% of 200 is +/-16, so 180 and 220 are outside and 190/200/210 are in."""
    service = _service(tmp_path, system_config)
    _store_chain(
        service,
        [TODAY + timedelta(days=21)],
        ["180", "190", "200", "210", "220"],
    )
    _store_quote(service, last="200.00")

    report = service.collect_option_quotes("SPY", target_dte=21)

    assert report.succeeded
    assert "3 strike(s)" in " ".join(report.notes)


def test_no_strike_in_the_band_is_reported_rather_than_widened(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    """A band that catches nothing is a fact about the chain, not a prompt to relax."""
    service = _service(tmp_path, system_config)
    _store_chain(service, [TODAY + timedelta(days=21)], ["10", "20"])
    _store_quote(service, last="200.00")

    report = service.collect_option_quotes("SPY", target_dte=21)

    assert report.outcome is CollectionOutcome.NO_DATA
    assert "no strike within" in (report.error or "")


def test_a_binding_contract_cap_is_recorded_not_silently_applied(
    tmp_path: Path, system_config: SystemConfig
) -> None:
    """A cap that binds changes which contracts a later selection can see.

    Applying it quietly would leave the selector reporting NO_VALID_CONTRACT
    for a strike that was never collected, with nothing on the record to say
    why.
    """
    service = _service(tmp_path, system_config)
    cap = system_config.data.collection.option_quotes.max_contracts
    strike_cap = cap // 2
    # A quarter-point ladder inside the 8% band, deliberately denser than the
    # cap allows, so the cap is what binds rather than the band.
    ladder = [f"{190 + n * Decimal('0.25')}" for n in range(strike_cap * 2 + 8)]
    _store_chain(service, [TODAY + timedelta(days=21)], ladder)
    _store_quote(service, last="200.00")

    report = service.collect_option_quotes("SPY", target_dte=21)

    assert report.succeeded
    notes = " ".join(report.notes)
    assert "CONTRACT_LIMIT_APPLIED" in notes
    assert "nearest the money" in notes
    assert f"{strike_cap} strike(s)" in notes
