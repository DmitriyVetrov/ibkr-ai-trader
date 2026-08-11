"""The collection pipeline end to end, and its failure handling.

A collector runs repeatedly and unattended. The properties that matter are
therefore about what happens on the bad days: a provider outage must not
destroy yesterday's data, a re-run must not manufacture history, and a record
carrying an implausible number must still be stored — flagged — rather than
dropped.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.data.collectors import CollectionReport, DataCollector, RegistryCollector
from trading_system.data.collectors.base import detect_gap
from trading_system.data.models import SNAPSHOT_CREATED, SNAPSHOT_REOBSERVED
from trading_system.data.providers.base import (
    ProviderAvailability,
    failed_result,
)
from trading_system.data.providers.market import SimulatedMarketDataProvider
from trading_system.data.providers.options import SimulatedOptionsDataProvider
from trading_system.data.registry import ProviderRegistry
from trading_system.domain.enums import (
    CollectionOutcome,
    DataGapStatus,
    DataQualityIssue,
    DataType,
    MarketDataOrigin,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def market_registry(data_clock) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(SimulatedMarketDataProvider(clock=data_clock))
    registry.register(SimulatedOptionsDataProvider(clock=data_clock))
    return registry


def _collector(
    repository, quality_engine, data_clock, data_type=DataType.MARKET_QUOTE, **kwargs
) -> DataCollector:
    return DataCollector(
        repository=repository,
        quality_engine=quality_engine,
        data_type=data_type,
        clock=data_clock,
        config_version="2026.08.11-1",
        **kwargs,
    )


def _quote_collector(repository, quality_engine, data_clock, market_registry):
    return RegistryCollector(
        registry=market_registry,
        collector=_collector(repository, quality_engine, data_clock),
        operation=lambda provider, key: provider.fetch_quote(key),  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# The happy path stores everything
# ---------------------------------------------------------------------------
def test_a_collection_writes_raw_normalized_snapshot_and_ledger(
    repository, quality_engine, data_clock, market_registry, tmp_path
) -> None:
    report = _quote_collector(repository, quality_engine, data_clock, market_registry).collect(
        "SPY"
    )

    assert report.outcome is CollectionOutcome.SUCCESS
    assert report.snapshots_created == 1
    assert report.snapshot_id

    root = tmp_path / "data"
    assert list((root / "raw").rglob("*.json"))
    assert list((root / "normalized").rglob("*.json"))
    assert list((root / "snapshots").rglob("*.json"))
    assert [e.event for e in repository.ledger(DataType.MARKET_QUOTE, "SPY")] == [SNAPSHOT_CREATED]


def test_the_report_says_what_the_data_actually_is(
    repository, quality_engine, data_clock, market_registry
) -> None:
    """ "Succeeded" is not the same claim as "this is real market data"."""
    report = _quote_collector(repository, quality_engine, data_clock, market_registry).collect(
        "SPY"
    )

    assert report.data_origin is MarketDataOrigin.SIMULATED
    assert report.display_status == "SIMULATED"


def test_collection_state_is_updated(
    repository, quality_engine, data_clock, market_registry
) -> None:
    _quote_collector(repository, quality_engine, data_clock, market_registry).collect("SPY")

    state = repository.get_collection_state(
        provider="SIMULATOR", data_type=DataType.MARKET_QUOTE, key="SPY"
    )
    assert state is not None
    assert state.last_successful_collection == data_clock.now()
    assert state.snapshot_count == 1
    assert state.records_collected == 1
    assert state.consecutive_failures == 0
    assert state.last_error is None


def test_the_symbol_is_normalised_to_upper_case(
    repository, quality_engine, data_clock, market_registry
) -> None:
    report = _quote_collector(repository, quality_engine, data_clock, market_registry).collect(
        "spy"
    )
    assert report.key == "SPY"


def test_an_option_chain_collection_stores_the_whole_chain(
    repository, quality_engine, data_clock, market_registry
) -> None:
    collector = RegistryCollector(
        registry=market_registry,
        collector=_collector(repository, quality_engine, data_clock, DataType.OPTION_CHAIN),
        operation=lambda provider, key: provider.fetch_chain(key),  # type: ignore[attr-defined]
    )
    report = collector.collect("SPY")

    assert report.succeeded
    stored = repository.get_latest(DataType.OPTION_CHAIN, "SPY")
    assert stored is not None
    assert len(stored.records[0]["strikes"]) > 5


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------
def test_running_the_same_collection_twice_creates_one_snapshot(
    repository, quality_engine, data_clock, market_registry
) -> None:
    collector = _quote_collector(repository, quality_engine, data_clock, market_registry)

    first = collector.collect("SPY")
    second = collector.collect("SPY")

    assert first.snapshots_created == 1
    assert second.snapshots_created == 0
    assert second.outcome is CollectionOutcome.SKIPPED_UNCHANGED
    assert second.succeeded


def test_the_second_run_is_still_recorded(
    repository, quality_engine, data_clock, market_registry
) -> None:
    collector = _quote_collector(repository, quality_engine, data_clock, market_registry)
    collector.collect("SPY")
    collector.collect("SPY")

    events = [e.event for e in repository.ledger(DataType.MARKET_QUOTE, "SPY")]
    assert events == [SNAPSHOT_CREATED, SNAPSHOT_REOBSERVED]


def test_a_changed_response_creates_a_new_snapshot(repository, quality_engine, data_clock) -> None:
    """Idempotence must not swallow a genuine price change."""

    class _Moving(SimulatedMarketDataProvider):
        provider_id = "MOVING"

        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.price = Decimal("500.00")

        def fetch_quote(self, symbol, security_type=None):
            from trading_system.data.providers.base import successful_result

            result = super().fetch_quote(symbol)
            quote = result.records[0].model_copy(
                update={
                    "last": self.price,
                    "source": result.records[0].source.model_copy(
                        update={"provider": self.provider_id}
                    ),
                }
            )
            return successful_result(
                provider_id=self.provider_id,
                data_type=DataType.MARKET_QUOTE,
                key=symbol.upper(),
                records=[quote],
                raw=result.raw,
            )

    provider = _Moving(clock=data_clock)
    registry = ProviderRegistry()
    registry.register(provider)
    collector = RegistryCollector(
        registry=registry,
        collector=_collector(repository, quality_engine, data_clock),
        operation=lambda p, key: p.fetch_quote(key),  # type: ignore[attr-defined]
    )

    first = collector.collect("SPY")
    provider.price = Decimal("512.75")
    data_clock.advance(days=1)
    second = collector.collect("SPY")

    assert first.snapshots_created == 1
    assert second.snapshots_created == 1


# ---------------------------------------------------------------------------
# Failures never destroy history
# ---------------------------------------------------------------------------
class _BrokenProvider(SimulatedMarketDataProvider):
    provider_id = "BROKEN"

    def fetch_quote(self, symbol, security_type=None):
        return failed_result(
            provider_id=self.provider_id,
            data_type=DataType.MARKET_QUOTE,
            key=symbol.upper(),
            outcome=CollectionOutcome.PROVIDER_UNAVAILABLE,
            error="gateway is down",
        )


def test_a_provider_failure_leaves_previous_snapshots_intact(
    repository, quality_engine, data_clock, market_registry
) -> None:
    _quote_collector(repository, quality_engine, data_clock, market_registry).collect("SPY")
    before = repository.get_latest(DataType.MARKET_QUOTE, "SPY")

    broken_registry = ProviderRegistry()
    broken_registry.register(_BrokenProvider(clock=data_clock))
    report = RegistryCollector(
        registry=broken_registry,
        collector=_collector(repository, quality_engine, data_clock),
        operation=lambda provider, key: provider.fetch_quote(key),  # type: ignore[attr-defined]
    ).collect("SPY")

    after = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert not report.succeeded
    assert report.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert before is not None and after is not None
    assert after.snapshot_id == before.snapshot_id


def test_a_failure_is_recorded_in_the_ledger_and_the_state(
    repository, quality_engine, data_clock
) -> None:
    registry = ProviderRegistry()
    registry.register(_BrokenProvider(clock=data_clock))
    RegistryCollector(
        registry=registry,
        collector=_collector(repository, quality_engine, data_clock),
        operation=lambda provider, key: provider.fetch_quote(key),  # type: ignore[attr-defined]
    ).collect("SPY")

    entries = repository.ledger(DataType.MARKET_QUOTE, "SPY")
    assert entries[-1].event == "COLLECTION_FAILED"
    assert entries[-1].outcome == "PROVIDER_UNAVAILABLE"

    state = repository.get_collection_state(
        provider="BROKEN", data_type=DataType.MARKET_QUOTE, key="SPY"
    )
    assert state is not None
    assert state.consecutive_failures == 1
    assert state.last_successful_collection is None
    assert "gateway is down" in (state.last_error or "")


def test_a_provider_that_raises_does_not_crash_the_collector(
    repository, quality_engine, data_clock
) -> None:
    class _Exploding(SimulatedMarketDataProvider):
        provider_id = "EXPLODING"

        def fetch_quote(self, symbol, security_type=None):
            raise RuntimeError("unexpected")

    registry = ProviderRegistry()
    registry.register(_Exploding(clock=data_clock))
    report = RegistryCollector(
        registry=registry,
        collector=_collector(repository, quality_engine, data_clock),
        operation=lambda provider, key: provider.fetch_quote(key),  # type: ignore[attr-defined]
    ).collect("SPY")

    assert not report.succeeded
    assert report.records_normalized == 0


def test_no_provider_available_is_reported_not_raised(
    repository, quality_engine, data_clock
) -> None:
    class _Down(SimulatedMarketDataProvider):
        provider_id = "DOWN"

        def availability(self) -> ProviderAvailability:
            return ProviderAvailability.UNAVAILABLE

    registry = ProviderRegistry()
    registry.register(_Down(clock=data_clock))
    report = RegistryCollector(
        registry=registry,
        collector=_collector(repository, quality_engine, data_clock),
        operation=lambda provider, key: provider.fetch_quote(key),  # type: ignore[attr-defined]
    ).collect("SPY")

    assert report.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert report.display_status == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Quality-flagged records are stored, not discarded
# ---------------------------------------------------------------------------
class _SuspiciousProvider(SimulatedMarketDataProvider):
    """Returns a quote whose volume cannot be a real session volume."""

    provider_id = "SUSPICIOUS"

    def fetch_quote(self, symbol, security_type=None):
        result = super().fetch_quote(symbol)
        quote = result.records[0].model_copy(update={"volume": Decimal("99000000000000")})
        from trading_system.data.providers.base import successful_result

        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.MARKET_QUOTE,
            key=symbol.upper(),
            records=[
                quote.model_copy(
                    update={
                        "source": quote.source.model_copy(update={"provider": self.provider_id})
                    }
                )
            ],
            raw=result.raw,
        )


def test_a_suspicious_record_is_stored_flagged_and_marked_unusable(
    repository, quality_engine, data_clock
) -> None:
    """Preserve the evidence, flag the problem, exclude it from research."""
    registry = ProviderRegistry()
    registry.register(_SuspiciousProvider(clock=data_clock))
    report = RegistryCollector(
        registry=registry,
        collector=_collector(repository, quality_engine, data_clock),
        operation=lambda provider, key: provider.fetch_quote(key),  # type: ignore[attr-defined]
    ).collect("SPY")

    assert report.succeeded
    assert report.snapshots_created == 1
    assert DataQualityIssue.SUSPICIOUS_VOLUME in report.quality_issues
    assert report.research_usable is False

    stored = repository.get_latest(DataType.MARKET_QUOTE, "SPY")
    assert stored is not None
    assert stored.records[0]["volume"] == "99000000000000"
    assert not stored.data_quality.research_usable


def test_an_oversized_payload_is_refused_rather_than_truncated(
    repository, quality_engine, data_clock, market_registry
) -> None:
    """A truncated chain that looks complete is worse than no chain."""
    collector = RegistryCollector(
        registry=market_registry,
        collector=_collector(
            repository, quality_engine, data_clock, DataType.OPTION_QUOTE, max_records=2
        ),
        operation=lambda provider, key: provider.fetch_option_quotes(key),  # type: ignore[attr-defined]
    )
    report = collector.collect("SPY")

    assert report.outcome is CollectionOutcome.INVALID_DATA
    assert "refusing to truncate" in (report.error or "")
    assert repository.get_latest(DataType.OPTION_QUOTE, "SPY") is None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_the_report_carries_the_observability_fields(
    repository, quality_engine, data_clock, market_registry
) -> None:
    report = _quote_collector(repository, quality_engine, data_clock, market_registry).collect(
        "SPY"
    )
    payload = report.model_dump(mode="json")

    for field in (
        "provider",
        "data_type",
        "key",
        "started_at",
        "completed_at",
        "duration_seconds",
        "records_received",
        "records_normalized",
        "records_rejected",
        "snapshots_created",
        "quality_issues",
    ):
        assert field in payload

    assert isinstance(report, CollectionReport)
    assert report.duration_seconds >= 0


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------
def test_never_collected_is_no_coverage_not_a_gap(data_now) -> None:
    """Starting from zero is the expected initial condition, not a fault."""
    gap = detect_gap(
        state=None,
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        now=data_now,
        expected_interval_seconds=3600,
    )
    assert gap.status is DataGapStatus.NO_COVERAGE
    assert gap.blocks_history_dependent_analysis


def test_a_recent_collection_has_no_gap(
    repository, quality_engine, data_clock, market_registry, data_now
) -> None:
    _quote_collector(repository, quality_engine, data_clock, market_registry).collect("SPY")
    state = repository.get_collection_state(
        provider="SIMULATOR", data_type=DataType.MARKET_QUOTE, key="SPY"
    )

    gap = detect_gap(
        state=state,
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        now=data_now,
        expected_interval_seconds=3600,
    )
    assert gap.status is DataGapStatus.NO_GAP
    assert not gap.blocks_history_dependent_analysis


def test_a_long_silence_is_a_detected_gap(
    repository, quality_engine, data_clock, market_registry, data_now
) -> None:
    _quote_collector(repository, quality_engine, data_clock, market_registry).collect("SPY")
    state = repository.get_collection_state(
        provider="SIMULATOR", data_type=DataType.MARKET_QUOTE, key="SPY"
    )

    gap = detect_gap(
        state=state,
        data_type=DataType.MARKET_QUOTE,
        key="SPY",
        now=data_now + timedelta(days=3),
        expected_interval_seconds=3600,
    )
    assert gap.status is DataGapStatus.GAP_DETECTED
    assert gap.gap_seconds is not None and gap.gap_seconds > 3600
    assert gap.last_seen is not None


def test_a_gap_is_reported_never_filled(data_now) -> None:
    """The initial system accumulates forward; it does not backfill."""
    gap = detect_gap(
        state=None,
        data_type=DataType.OPTION_CHAIN,
        key="SPY",
        now=data_now,
        expected_interval_seconds=3600,
    )
    assert not hasattr(gap, "fill")
    assert gap.detail
