"""The collection pipeline.

One path, run by every collector, in this order:

1. retrieve from the provider;
2. assess quality (the records are described, never altered);
3. store the raw response;
4. store the normalised records;
5. offer an immutable snapshot to the store;
6. append to the append-only ledger;
7. update collection state.

Properties that hold at every step:

* **Nothing destructive.** A failed collection appends a failure entry and
  updates the state file. It never touches an existing snapshot. A bad new
  record cannot delete a good old one.
* **Idempotent.** Re-running a collection whose provider response has not
  changed produces a re-observation entry, not a second snapshot.
* **Quality-flagged, never quality-filtered.** A record with an implausible
  value is stored with its flags intact. Discarding it would destroy the
  evidence, and repairing it would fabricate data. Consumers filter on
  ``research_usable``.
* **Observable.** Every run logs provider, data type, key, duration, counts and
  issue codes. No secrets are logged — the collector never sees one.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import TypeVar

from pydantic import Field

from trading_system.data.cache import DataCache
from trading_system.data.models import (
    CollectionState,
    DataQualityReport,
    DataRecord,
    DataSnapshot,
    HistoricalGap,
)
from trading_system.data.providers.base import ProviderResult
from trading_system.data.quality import QualityContext, QualityEngine
from trading_system.data.repository import DataRepository, build_snapshot
from trading_system.domain.enums import (
    CollectionOutcome,
    DataGapStatus,
    DataQualityIssue,
    DataType,
    MarketDataOrigin,
)
from trading_system.domain.models import Identifier, ImmutableModel, UtcDatetime
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger

__all__ = ["CollectionReport", "DataCollector", "detect_gap"]

RecordT = TypeVar("RecordT", bound=DataRecord)

_logger = get_logger(__name__)


class CollectionReport(ImmutableModel):
    """What one collection run did. Returned, logged and printed by the CLI."""

    provider: Identifier
    data_type: DataType
    key: Identifier
    outcome: CollectionOutcome
    started_at: UtcDatetime
    completed_at: UtcDatetime
    duration_seconds: float = Field(ge=0)

    records_received: int = Field(default=0, ge=0)
    records_normalized: int = Field(default=0, ge=0)
    records_rejected: int = Field(default=0, ge=0)
    snapshots_created: int = Field(default=0, ge=0)
    snapshot_id: str | None = None
    data_origin: MarketDataOrigin | None = None
    research_usable: bool | None = None
    quality_issues: list[DataQualityIssue] = Field(default_factory=list)
    error: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.outcome in (
            CollectionOutcome.SUCCESS,
            CollectionOutcome.PARTIAL_SUCCESS,
            CollectionOutcome.SKIPPED_UNCHANGED,
        )

    @property
    def display_status(self) -> str:
        """How the CLI should label this run's data.

        Says what the data actually is — real, simulated, cached, historical or
        unavailable — rather than only whether the command succeeded.
        """
        if not self.succeeded or self.data_origin is None:
            return "UNAVAILABLE"
        if self.data_origin is MarketDataOrigin.SIMULATED:
            return "SIMULATED"
        if self.data_origin is MarketDataOrigin.CACHED:
            return "CACHED"
        if self.data_origin is MarketDataOrigin.HISTORICAL:
            return "HISTORICAL"
        return "REAL"


class DataCollector:
    """Runs the pipeline for one data type.

    Deliberately generic: the collector knows how to store and assess, and the
    provider knows how to retrieve. Adding a data type means adding a provider,
    not another copy of this.
    """

    def __init__(
        self,
        *,
        repository: DataRepository,
        quality_engine: QualityEngine,
        data_type: DataType,
        clock: Clock | None = None,
        cache: DataCache | None = None,
        config_version: str | None = None,
        max_records: int = 20000,
    ) -> None:
        self._repository = repository
        self._quality = quality_engine
        self._data_type = data_type
        self._clock = clock or SystemClock()
        self._cache = cache
        self._config_version = config_version
        self._max_records = max_records

    @property
    def data_type(self) -> DataType:
        return self._data_type

    def collect(
        self,
        key: str,
        fetch: Callable[[], ProviderResult[RecordT]],
        *,
        requested_provider: str,
    ) -> CollectionReport:
        """Retrieve, assess and store one dataset.

        ``fetch`` is a zero-argument call so the collector never has to know
        which provider it is talking to or how that provider is configured.
        """
        started_at = self._clock.now()
        started = time.perf_counter()

        try:
            result = fetch()
        except Exception as exc:
            # A provider that raises instead of returning a failure result is a
            # provider bug, but it must not take the collector down with it.
            return self._fail(
                key=key,
                provider=requested_provider,
                outcome=CollectionOutcome.INVALID_DATA,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
                started=started,
            )

        if not result.succeeded:
            return self._fail(
                key=key,
                provider=result.provider_id or requested_provider,
                outcome=result.outcome,
                error=result.error or result.outcome.value,
                started_at=started_at,
                started=started,
                raw_stored=self._store_raw(result),
                notes=list(result.notes),
            )

        if len(result.records) > self._max_records:
            # Refusing beats truncating: a partial option chain that looks
            # complete would silently corrupt every later analysis of it.
            return self._fail(
                key=key,
                provider=result.provider_id,
                outcome=CollectionOutcome.INVALID_DATA,
                error=(
                    f"{len(result.records)} records exceed the configured "
                    f"max_records_per_snapshot {self._max_records}; refusing to truncate"
                ),
                started_at=started_at,
                started=started,
                raw_stored=self._store_raw(result),
            )

        self._store_raw(result)

        assessed = self._quality.attach_all(
            result.records,
            context=QualityContext(now=self._clock.now(), expected_provider=result.provider_id),
        )
        snapshot = self._build_snapshot(key, result, assessed)
        self._repository.save_normalized(
            data_type=self._data_type,
            key=key,
            provider=result.provider_id,
            records=assessed,
            as_of=snapshot.as_of,
        )
        write = self._repository.save_snapshot(snapshot)

        completed_at = self._clock.now()
        outcome = (
            CollectionOutcome.SKIPPED_UNCHANGED
            if write.deduplicated and write.reason == "UNCHANGED"
            else result.outcome
        )
        report = CollectionReport(
            provider=result.provider_id,
            data_type=self._data_type,
            key=key,
            outcome=outcome,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=time.perf_counter() - started,
            records_received=len(result.records),
            records_normalized=len(assessed),
            records_rejected=0,
            snapshots_created=1 if write.created else 0,
            snapshot_id=write.snapshot_id,
            data_origin=snapshot.data_origin,
            research_usable=snapshot.data_quality.research_usable,
            quality_issues=list(snapshot.data_quality.issues),
            notes=list(result.notes),
        )
        self._record_state(report)
        self._log(report)
        return report

    # --- internals ---------------------------------------------------------
    def _store_raw(self, result: ProviderResult[RecordT]) -> bool:
        if result.raw is None:
            return False
        self._repository.save_raw(result.raw)
        return True

    def _build_snapshot(
        self, key: str, result: ProviderResult[RecordT], assessed: Sequence[RecordT]
    ) -> DataSnapshot:
        first = assessed[0]
        # The snapshot describes the newest instant any of its records
        # describes; using the oldest would make it invisible to a
        # point-in-time query that should have seen it.
        as_of = max(record.as_of for record in assessed)
        retrieved_at = min(record.source.retrieved_at for record in assessed)
        source_timestamps = [
            record.source.source_timestamp
            for record in assessed
            if record.source.source_timestamp is not None
        ]
        return build_snapshot(
            data_type=self._data_type,
            key=key,
            records=assessed,
            provider=result.provider_id,
            source_tier=first.source.source_tier,
            origin=first.source.origin,
            as_of=as_of,
            retrieved_at=retrieved_at,
            source_timestamp=max(source_timestamps) if source_timestamps else None,
            quality=_roll_up(record.quality for record in assessed),
            config_version=self._config_version,
        )

    def _fail(
        self,
        *,
        key: str,
        provider: str,
        outcome: CollectionOutcome,
        error: str,
        started_at: datetime,
        started: float,
        raw_stored: bool = False,
        notes: list[str] | None = None,
    ) -> CollectionReport:
        self._repository.record_failure(
            data_type=self._data_type,
            key=key,
            provider=provider,
            outcome=outcome.value,
            detail=error,
        )
        report = CollectionReport(
            provider=provider,
            data_type=self._data_type,
            key=key,
            outcome=outcome,
            started_at=started_at,
            completed_at=self._clock.now(),
            duration_seconds=time.perf_counter() - started,
            error=error,
            notes=notes or ([] if not raw_stored else ["raw response preserved"]),
        )
        self._record_state(report)
        self._log(report)
        return report

    def _record_state(self, report: CollectionReport) -> None:
        previous = self._repository.get_collection_state(
            provider=report.provider, data_type=self._data_type, key=report.key
        ) or CollectionState(provider=report.provider, data_type=self._data_type, key=report.key)
        self._repository.save_collection_state(
            CollectionState(
                provider=report.provider,
                data_type=self._data_type,
                key=report.key,
                last_attempt=report.completed_at,
                last_successful_collection=(
                    report.completed_at if report.succeeded else previous.last_successful_collection
                ),
                last_error=None if report.succeeded else report.error,
                last_outcome=report.outcome.value,
                records_collected=previous.records_collected + report.records_normalized,
                snapshot_count=previous.snapshot_count + report.snapshots_created,
                consecutive_failures=0 if report.succeeded else previous.consecutive_failures + 1,
            )
        )

    @staticmethod
    def _log(report: CollectionReport) -> None:
        _logger.info(
            "data.collection",
            provider=report.provider,
            data_type=report.data_type.value,
            symbol=report.key,
            outcome=report.outcome.value,
            request_started=report.started_at.isoformat(),
            request_completed=report.completed_at.isoformat(),
            duration_seconds=round(report.duration_seconds, 4),
            records_received=report.records_received,
            records_normalized=report.records_normalized,
            records_rejected=report.records_rejected,
            snapshots_created=report.snapshots_created,
            quality_issues=[issue.value for issue in report.quality_issues],
            research_usable=report.research_usable,
            error=report.error,
        )


def _roll_up(reports: Iterable[DataQualityReport]) -> DataQualityReport:
    """Combine per-record verdicts into one snapshot-level verdict.

    Pessimistic by construction: one implausible record makes the snapshot
    implausible. A snapshot that averaged its records' quality would let a bad
    record hide inside a good batch.
    """
    collected = list(reports)
    if not collected:
        return DataQualityReport(
            completeness_valid=False,
            research_usable=False,
            issues=[DataQualityIssue.EMPTY_PAYLOAD],
            details=["snapshot contains no records"],
        )
    issues: list[DataQualityIssue] = []
    details: list[str] = []
    for report in collected:
        for issue in report.issues:
            if issue not in issues:
                issues.append(issue)
        details.extend(report.details)
    return DataQualityReport(
        transport_valid=all(r.transport_valid for r in collected),
        schema_valid=all(r.schema_valid for r in collected),
        source_valid=all(r.source_valid for r in collected),
        timestamp_valid=all(r.timestamp_valid for r in collected),
        freshness_valid=all(r.freshness_valid for r in collected),
        completeness_valid=all(r.completeness_valid for r in collected),
        plausibility_valid=all(r.plausibility_valid for r in collected),
        consistency_valid=all(r.consistency_valid for r in collected),
        research_usable=all(r.research_usable for r in collected),
        issues=issues,
        details=details[:50],
        evaluated_at=collected[0].evaluated_at,
    )


def detect_gap(
    *,
    state: CollectionState | None,
    data_type: DataType,
    key: str,
    now: datetime,
    expected_interval_seconds: int,
) -> HistoricalGap:
    """Report whether collection has fallen behind its expected cadence.

    Reports; does not fill. Backfilling would mean downloading data we have
    chosen not to pay for, so the initial system accumulates forward and says
    plainly where it has nothing.
    """
    if state is None or state.last_successful_collection is None:
        return HistoricalGap(
            data_type=data_type,
            key=key,
            status=DataGapStatus.NO_COVERAGE,
            detail="nothing has ever been collected successfully for this key",
        )
    elapsed = (now - state.last_successful_collection).total_seconds()
    if elapsed <= expected_interval_seconds:
        return HistoricalGap(
            data_type=data_type,
            key=key,
            status=DataGapStatus.NO_GAP,
            last_seen=state.last_successful_collection,
            gap_seconds=max(0.0, elapsed),
        )
    return HistoricalGap(
        data_type=data_type,
        key=key,
        status=DataGapStatus.GAP_DETECTED,
        last_seen=state.last_successful_collection,
        gap_seconds=elapsed,
        detail=(
            f"last successful collection was {elapsed:.0f}s ago, beyond the expected "
            f"{expected_interval_seconds}s cadence"
        ),
    )
